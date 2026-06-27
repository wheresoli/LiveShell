from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Generator, List

from .runtime import AsyncPreloadableSession, PreloadableSession
from .utils import Environment


@dataclass(frozen=True)
class PowerShellResult:
    command: str
    output: list[Any]
    errors: list[str]
    had_errors: bool
    exit_code: int
    last_exit_code: int | None = None

    @property
    def stdout(self) -> str:
        return "\n".join(str(item) for item in self.output)

    @property
    def stderr(self) -> str:
        return "\n".join(self.errors)

    def check_returncode(self) -> None:
        if self.exit_code != 0:
            raise RuntimeError(self.stderr or "PowerShell command failed.")


class PowerShell(PreloadableSession):
    @dataclass(frozen=True)
    class Installation:
        home: Path
        assembly: Path
        edition: str
        runtime_config: Path | None = None

        @property
        def uses_coreclr(self) -> bool:
            return self.runtime_config is not None

    _CLR_LOADED = False
    _DLL_DIRECTORIES = []
    _LOAD_LOCK = RLock()
    _LOADED_RUNTIME: str | None = None
    runtime = None
    
    def __init__(
        self,
        pshome: str | os.PathLike[str] | None = None,
        *,
        cwd: str | os.PathLike[str] | None = None,
    ):
        self.installations = list(self.find_installations(pshome))
        self.installation = self.installations[0] if self.installations else None
        if self.installation is None:
            raise RuntimeError("Could not find a PowerShell installation.")
        self.pshome = self.installation.home
        self.cwd = Path(cwd) if cwd is not None else None
        self.preload(self.pshome)

        from System.Management.Automation import PowerShell as DotNetPowerShell
        from System.Management.Automation.Runspaces import (
            InitialSessionState,
            RunspaceFactory,
        )

        self._PowerShell = DotNetPowerShell
        self._lock = RLock()
        self._closed = False

        initial_state = InitialSessionState.CreateDefault()
        self.runspace = RunspaceFactory.CreateRunspace(initial_state)
        self.runspace.Open()
        if self.cwd is not None:
            self.run(f"Set-Location -LiteralPath {self._quote_string(str(self.cwd))}")

    @classmethod
    def is_preloaded(cls) -> bool:
        return cls._CLR_LOADED and cls.runtime is not None

    @classmethod
    def preload(cls, path: str | os.PathLike[str] | None = None) -> None:
        with cls._LOAD_LOCK:
            if "clr" in sys.modules and not cls._CLR_LOADED:
                raise RuntimeError("Load pythonnet before importing clr.")

            installation = cls.find_installation(path)
            if installation is None:
                raise RuntimeError("Could not find a PowerShell installation.")
            home = installation.home
            dll_directory = None

            if installation.uses_coreclr and Environment.is_windows():
                home_str = str(home)
                if not any(d.path == home_str for d in cls._DLL_DIRECTORIES):
                    dll_directory = os.add_dll_directory(home_str)

            if str(home) not in sys.path:
                sys.path.insert(0, str(home))
            os.environ.setdefault("PSHOME", str(home))

            if dll_directory is not None:
                cls._DLL_DIRECTORIES.append(dll_directory)

            if not cls._CLR_LOADED:
                try:
                    from pythonnet import load
                except ImportError as exc:
                    raise RuntimeError("pythonnet is required to host PowerShell.") from exc

                if installation.uses_coreclr:
                    load("coreclr", runtime_config=str(installation.runtime_config))
                    cls._LOADED_RUNTIME = "coreclr"
                else:
                    load("netfx")
                    cls._LOADED_RUNTIME = "netfx"
                cls._CLR_LOADED = True
            elif installation.uses_coreclr and cls._LOADED_RUNTIME != "coreclr":
                raise RuntimeError("Cannot load PowerShell Core after .NET Framework is already loaded.")
            elif not installation.uses_coreclr and cls._LOADED_RUNTIME != "netfx":
                raise RuntimeError("Cannot load Windows PowerShell after CoreCLR is already loaded.")

            import clr

            clr.AddReference(str(installation.assembly))
            cls.runtime = clr

    @classmethod
    def find(cls, path: str | os.PathLike[str] | None = None, **kwargs) -> Path | None:
        installation = cls.find_installation(path, **kwargs)
        return installation.home if installation else None

    @classmethod
    def find_installation(
        cls,
        path: str | os.PathLike[str] | None = None,
        **kwargs,
    ) -> PowerShell.Installation | None:
        for installation in cls.find_installations(path, **kwargs):
            return installation
        return None

    @classmethod
    def find_installations(
        cls,
        path: str | os.PathLike[str] | None = None,
        **kwargs,
    ) -> Generator[PowerShell.Installation, None, None]:
        candidates: List[Path] = []

        if path is not None:
            if not cls.is_valid_home(Path(path)):
                raise RuntimeError(f"Invalid PowerShell home path: {path}")
            candidates.append(Path(path))

        if os.environ.get("PSHOME"):
            pshome = Path(os.environ["PSHOME"])
            if pshome.is_dir() and cls.is_valid_home(pshome):
                candidates.append(pshome)

        executable = Environment.executable("pwsh.exe", "pwsh")
        if executable:
            candidates.append(executable.parent)

        env = Environment.current()
        if env.is_windows():
            for pf in env.program_files():
                candidates.extend(cls.versioned_homes(pf / "PowerShell"))
            windirs = [Path(os.environ["WINDIR"])] if os.environ.get("WINDIR") else [
                disk / "Windows" for disk in env.get_disks()
            ]
            for windir in windirs:
                for version in cls.versioned_homes(windir / "System32" / "WindowsPowerShell"):
                    candidates.append(version)
                for version in cls.versioned_homes(windir / "SysWOW64" / "WindowsPowerShell"):
                    candidates.append(version)
        elif env.is_mac():
            homebrew_prefix = env.homebrew_prefix()
            if homebrew_prefix:
                candidates.extend(cls.versioned_homes(homebrew_prefix / "microsoft" / "powershell"))
            candidates.extend(cls.versioned_homes(Path("/usr/local/microsoft/powershell")))
            candidates.extend(cls.versioned_homes(Path("/opt/homebrew/microsoft/powershell")))
        elif env.is_linux():
            homebrew_prefix = env.homebrew_prefix()
            if homebrew_prefix:
                candidates.extend(cls.versioned_homes(homebrew_prefix / "microsoft" / "powershell"))
            candidates.extend(cls.versioned_homes(Path("/opt/microsoft/powershell")))
            candidates.append(Path("/usr/lib/powershell"))
            snap_dir = env.snap_dir()
            if snap_dir:
                candidates.append(snap_dir / "powershell" / "current" / "opt" / "powershell")

        for candidate in Environment.dedupe_paths(candidates):
            installation = cls.describe_installation(candidate)
            if installation:
                yield installation

        return None

    @classmethod
    def describe_installation(cls, home: Path) -> PowerShell.Installation | None:
        home = Path(home)

        core_assembly = home / "System.Management.Automation.dll"
        runtime_config = home / "pwsh.runtimeconfig.json"
        if core_assembly.exists() and runtime_config.exists():
            return cls.Installation(
                home=home,
                assembly=core_assembly,
                runtime_config=runtime_config,
                edition="core",
            )

        if Environment.is_windows() and (home / "powershell.exe").exists():
            desktop_assembly = cls.find_desktop_assembly()
            if desktop_assembly:
                return cls.Installation(
                    home=home,
                    assembly=desktop_assembly,
                    edition="desktop",
                )

        return None

    @staticmethod
    def find_desktop_assembly() -> Path | None:
        windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        gac_root = windir / "Microsoft.Net" / "assembly" / "GAC_MSIL" / "System.Management.Automation"
        if not gac_root.is_dir():
            return None

        try:
            assemblies = list(gac_root.glob("*/System.Management.Automation.dll"))
        except OSError:
            return None

        return sorted(assemblies, key=lambda p: p.parent.name, reverse=True)[0] if assemblies else None

    @classmethod
    def versioned_homes(cls, root: Path) -> list[Path]:
        if not root.is_dir():
            return []

        try:
            homes = [path for path in root.iterdir() if path.is_dir()]
        except OSError:
            return []

        return sorted(homes, key=cls._version_sort_key, reverse=True)

    @staticmethod
    def _version_sort_key(path: Path) -> tuple[tuple[int, ...], bool, str]:
        numbers = tuple(int(value) for value in re.findall(r"\d+", path.name))
        is_stable = "preview" not in path.name.lower()
        return numbers, is_stable, path.name.lower()

    @staticmethod
    def is_valid_home(path: Path) -> bool:
        return PowerShell.describe_installation(path) is not None

    @staticmethod
    def _quote_string(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    @staticmethod
    def _reset_last_exit_code_script() -> str:
        return "$global:LASTEXITCODE = $null"

    @staticmethod
    def _read_last_exit_code_script() -> str:
        return (
            "if (Get-Variable -Name LASTEXITCODE -Scope Global -ErrorAction SilentlyContinue) "
            "{ $global:LASTEXITCODE } else { $null }"
        )

    @staticmethod
    def _coerce_last_exit_code(value: Any) -> int | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    @staticmethod
    def _command_exit_code(*, had_errors: bool, last_exit_code: int | None) -> int:
        if last_exit_code is not None and last_exit_code != 0:
            return last_exit_code
        if had_errors:
            return 1
        return last_exit_code if last_exit_code is not None else 0

    def is_running(self) -> bool:
        return not self._closed and getattr(self, "runspace", None) is not None

    def run_result(self, script: str, *, check: bool = True) -> PowerShellResult:
        if not self.is_running():
            raise RuntimeError("PowerShell session is closed.")

        with self._lock:
            ps = self._PowerShell.Create()
            try:
                ps.Runspace = self.runspace
                ps.AddScript(self._reset_last_exit_code_script(), False)
                ps.AddStatement()
                ps.AddScript(script, False)

                output = list(ps.Invoke())
                errors = [str(error) for error in ps.Streams.Error]
                had_errors = bool(ps.HadErrors)
            finally:
                ps.Dispose()
            last_exit_code = self._read_last_exit_code()

        result = PowerShellResult(
            command=script,
            output=output,
            errors=errors,
            had_errors=had_errors,
            exit_code=self._command_exit_code(
                had_errors=had_errors,
                last_exit_code=last_exit_code,
            ),
            last_exit_code=last_exit_code,
        )
        if check:
            result.check_returncode()
        return result

    def run(self, script: str, *, check: bool = True) -> list[Any]:
        return self.run_result(script, check=check).output

    def text(self, script: str, *, check: bool = True) -> str:
        if not self.is_running():
            raise RuntimeError("PowerShell session is closed.")

        with self._lock:
            ps = self._PowerShell.Create()
            try:
                ps.Runspace = self.runspace
                ps.AddScript(self._reset_last_exit_code_script(), False)
                ps.AddStatement()
                ps.AddScript(script, False)
                ps.AddCommand("Out-String")

                result = list(ps.Invoke())
                errors = [str(error) for error in ps.Streams.Error]
                had_errors = bool(ps.HadErrors)
            finally:
                ps.Dispose()
            last_exit_code = self._read_last_exit_code()

        invoke_result = PowerShellResult(
            command=script,
            output=result,
            errors=errors,
            had_errors=had_errors,
            exit_code=self._command_exit_code(
                had_errors=had_errors,
                last_exit_code=last_exit_code,
            ),
            last_exit_code=last_exit_code,
        )
        if check:
            invoke_result.check_returncode()

        return "".join(str(item) for item in result).rstrip()

    def _read_last_exit_code(self) -> int | None:
        ps = self._PowerShell.Create()
        try:
            ps.Runspace = self.runspace
            ps.AddScript(self._read_last_exit_code_script(), False)
            values = list(ps.Invoke())
            if not values:
                return None
            return self._coerce_last_exit_code(values[-1])
        finally:
            ps.Dispose()

    def close(self) -> None:
        if self._closed:
            return

        with self._lock:
            if self._closed:
                return

            runspace = getattr(self, "runspace", None)
            if runspace is not None:
                try:
                    runspace.Close()
                finally:
                    runspace.Dispose()

            self._closed = True
            self.runspace = None


class AsyncPowerShell(AsyncPreloadableSession):
    Installation = PowerShell.Installation

    def __init__(
        self,
        pshome: str | os.PathLike[str] | None = None,
        *,
        cwd: str | os.PathLike[str] | None = None,
    ):
        self.pshome = pshome
        self.cwd = cwd
        self._session: PowerShell | None = None
        self._start_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._closed = False

    def __del__(self):
        session = getattr(self, "_session", None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    @classmethod
    def is_preloaded(cls) -> bool:
        return PowerShell.is_preloaded()

    @classmethod
    def preload(cls, path: str | os.PathLike[str] | None = None) -> None:
        PowerShell.preload(path)

    @classmethod
    def find(cls, path: str | os.PathLike[str] | None = None, **kwargs) -> Path | None:
        return PowerShell.find(path, **kwargs)

    @classmethod
    def find_installation(
        cls,
        path: str | os.PathLike[str] | None = None,
        **kwargs,
    ) -> PowerShell.Installation | None:
        return PowerShell.find_installation(path, **kwargs)

    @classmethod
    def find_installations(
        cls,
        path: str | os.PathLike[str] | None = None,
        **kwargs,
    ) -> Generator[PowerShell.Installation, None, None]:
        return PowerShell.find_installations(path, **kwargs)

    @classmethod
    def describe_installation(cls, home: Path) -> PowerShell.Installation | None:
        return PowerShell.describe_installation(home)

    @staticmethod
    def find_desktop_assembly() -> Path | None:
        return PowerShell.find_desktop_assembly()

    @classmethod
    def versioned_homes(cls, root: Path) -> list[Path]:
        return PowerShell.versioned_homes(root)

    @staticmethod
    def _version_sort_key(path: Path) -> tuple[tuple[int, ...], bool, str]:
        return PowerShell._version_sort_key(path)

    @staticmethod
    def is_valid_home(path: Path) -> bool:
        return PowerShell.is_valid_home(path)

    async def start(self):
        if self._closed:
            raise RuntimeError("AsyncPowerShell session is closed.")
        if self.is_running():
            return self

        async with self._start_lock:
            if self._closed:
                raise RuntimeError("AsyncPowerShell session is closed.")
            if self._session is None:
                self._session = await asyncio.to_thread(
                    PowerShell,
                    self.pshome,
                    cwd=self.cwd,
                )
            elif not self._session.is_running():
                raise RuntimeError("AsyncPowerShell session is closed.")

        return self

    def is_running(self) -> bool:
        return (
            not self._closed
            and self._session is not None
            and self._session.is_running()
        )

    async def run(self, script: str, *, check: bool = True) -> list[Any]:
        await self.start()
        if self._session is None:
            raise RuntimeError("AsyncPowerShell session is not connected.")
        async with self._operation_lock:
            return await asyncio.to_thread(self._session.run, script, check=check)

    async def run_result(self, script: str, *, check: bool = True) -> PowerShellResult:
        await self.start()
        if self._session is None:
            raise RuntimeError("AsyncPowerShell session is not connected.")
        async with self._operation_lock:
            return await asyncio.to_thread(self._session.run_result, script, check=check)

    async def text(self, script: str, *, check: bool = True) -> str:
        await self.start()
        if self._session is None:
            raise RuntimeError("AsyncPowerShell session is not connected.")
        async with self._operation_lock:
            return await asyncio.to_thread(self._session.text, script, check=check)

    async def close(self) -> None:
        if self._closed:
            return

        async with self._operation_lock:
            if self._closed:
                return

            session = self._session
            if session is not None:
                await asyncio.to_thread(session.close)

            self._closed = True
