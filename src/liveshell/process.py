from __future__ import annotations

import asyncio
import locale
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Callable, Sequence

from .runtime import AsyncProcessBackedSession, ProcessBackedSession
from .utils import Environment


@dataclass(frozen=True)
class ProcessResult:
    command: str
    output: str
    exit_code: int
    stderr: str = ""

    def check_returncode(self) -> None:
        if self.exit_code != 0:
            raise RuntimeError(self.stderr or self.output)


class ProcessSessionBase:
    executable_names: Sequence[str] = ()
    startup_args: Sequence[str] = ()
    exit_command = "exit"
    line_ending = "\n"
    sentinel_prefix = "__LIVESHELL_DONE__"
    stderr_begin_prefix = "__LIVESHELL_STDERR_BEGIN__"
    stderr_end_prefix = "__LIVESHELL_STDERR_END__"

    @classmethod
    def find(cls, path: str | os.PathLike[str] | None = None, **kwargs) -> Path | None:
        if path is not None:
            executable = Path(path).resolve()
            if executable.is_file():
                return executable
            raise RuntimeError(f"Invalid executable path: {path}")
        return Environment.executable(*cls.executable_names)

    @classmethod
    def is_available(cls) -> bool:
        return cls.find() is not None

    def clean_output(self, output: str) -> str:
        return output.rstrip()

    def wrap_command(
        self,
        command: str,
        token: str,
        *,
        stderr_path: str | None = None,
    ) -> str:
        raise NotImplementedError

    def parse_sentinel(self, line: str, token: str) -> int | None:
        marker = f"{self.sentinel_prefix}:{token}:"
        stripped = line.strip()
        index = stripped.find(marker)
        if index == -1:
            return None
        try:
            return int(stripped[index + len(marker):])
        except ValueError:
            return 1

    def is_stderr_begin(self, line: str, token: str) -> bool:
        return f"{self.stderr_begin_prefix}:{token}" in line.strip()

    def is_stderr_end(self, line: str, token: str) -> bool:
        return f"{self.stderr_end_prefix}:{token}" in line.strip()

    def clean_stderr(self, output: str) -> str:
        return output.rstrip()


class ProcessSession(ProcessSessionBase, ProcessBackedSession):
    def __init__(
        self,
        executable: str | os.PathLike[str] | None = None,
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: dict[str, str] | None = None,
        encoding: str | None = None,
    ):
        self.executable = self.find(executable)
        if self.executable is None:
            raise RuntimeError(f"Could not find {self.__class__.__name__} executable.")

        self.cwd = Path(cwd) if cwd is not None else None
        self.encoding = encoding or locale.getpreferredencoding(False)
        self._lock = RLock()
        self._closed = False
        self.process = subprocess.Popen(
            [str(self.executable), *self.startup_args],
            cwd=str(self.cwd) if self.cwd is not None else None,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding=self.encoding,
            errors="replace",
            bufsize=1,
        )

    def is_running(self) -> bool:
        return not self._closed and self.process.poll() is None

    def run(self, command: str, *, check: bool = True) -> ProcessResult:
        return self.run_stream(command, check=check)

    def run_stream(
        self,
        command: str,
        *,
        check: bool = True,
        stdout_callback: Callable[[str], None] | None = None,
        stderr_callback: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        if not self.is_running():
            raise RuntimeError(f"{self.__class__.__name__} session is closed.")

        token = uuid.uuid4().hex
        stderr_path = self._new_stderr_path()
        wrapped = self.wrap_command(command, token, stderr_path=stderr_path)

        with self._lock:
            try:
                if self.process.stdin is None or self.process.stdout is None:
                    raise RuntimeError(f"{self.__class__.__name__} session is not connected.")

                self.process.stdin.write(wrapped)
                self.process.stdin.flush()

                output_parts: list[str] = []
                stderr_parts: list[str] = []
                exit_code: int | None = None
                reading_stderr = False
                while True:
                    line = self.process.stdout.readline()
                    if line == "":
                        if self.process.poll() is not None:
                            raise RuntimeError(f"{self.__class__.__name__} session exited unexpectedly.")
                        continue

                    if self.is_stderr_begin(line, token):
                        reading_stderr = True
                        continue
                    if self.is_stderr_end(line, token):
                        reading_stderr = False
                        continue

                    parsed = self.parse_sentinel(line, token)
                    if parsed is not None:
                        exit_code = parsed
                        break

                    if reading_stderr:
                        stderr_parts.append(line)
                    else:
                        output_parts.append(line)
                        if stdout_callback is not None:
                            stdout_callback(line)
            finally:
                self._cleanup_stderr_path(stderr_path)

        output = self.clean_output("".join(output_parts))
        stderr = self.clean_stderr("".join(stderr_parts))
        if stderr and stderr_callback is not None:
            stderr_callback(stderr)

        result = ProcessResult(
            command=command,
            output=output,
            exit_code=exit_code if exit_code is not None else 1,
            stderr=stderr,
        )
        if check:
            result.check_returncode()
        return result

    def _new_stderr_path(self) -> str:
        handle = tempfile.NamedTemporaryFile(
            prefix="liveshell-stderr-",
            suffix=".txt",
            delete=False,
        )
        try:
            return handle.name
        finally:
            handle.close()

    def _cleanup_stderr_path(self, path: str) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    def text(self, command: str, *, check: bool = True) -> str:
        return self.run(command, check=check).output

    def close(self) -> None:
        if self._closed:
            return

        with self._lock:
            if self._closed:
                return

            if self.process.poll() is None and self.process.stdin is not None:
                try:
                    self.process.stdin.write(f"{self.exit_command}{self.line_ending}")
                    self.process.stdin.flush()
                    self.process.wait(timeout=2)
                except Exception:
                    if self.process.poll() is None:
                        self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        if self.process.poll() is None:
                            self.process.kill()
                        try:
                            self.process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            pass

            if self.process.stdin is not None:
                self.process.stdin.close()
            if self.process.stdout is not None:
                self.process.stdout.close()

            self._closed = True


class AsyncProcessSession(ProcessSessionBase, AsyncProcessBackedSession):
    def __init__(
        self,
        executable: str | os.PathLike[str] | None = None,
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: dict[str, str] | None = None,
        encoding: str | None = None,
    ):
        self.executable = self.find(executable)
        if self.executable is None:
            raise RuntimeError(f"Could not find {self.__class__.__name__} executable.")

        self.cwd = Path(cwd) if cwd is not None else None
        self.env = env
        self.encoding = encoding or locale.getpreferredencoding(False)
        self._start_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._closed = False
        self.process: asyncio.subprocess.Process | None = None

    def __del__(self):
        process = getattr(self, "process", None)
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass

    async def start(self):
        if self._closed:
            raise RuntimeError(f"{self.__class__.__name__} session is closed.")
        if self.is_running():
            return self

        async with self._start_lock:
            if self._closed:
                raise RuntimeError(f"{self.__class__.__name__} session is closed.")
            if self.is_running():
                return self
            if self.process is not None:
                raise RuntimeError(f"{self.__class__.__name__} session is closed.")

            self.process = await asyncio.create_subprocess_exec(
                str(self.executable),
                *self.startup_args,
                cwd=str(self.cwd) if self.cwd is not None else None,
                env=self.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        return self

    def is_running(self) -> bool:
        return (
            not self._closed
            and self.process is not None
            and self.process.returncode is None
        )

    async def run(self, command: str, *, check: bool = True) -> ProcessResult:
        await self.start()

        token = uuid.uuid4().hex
        wrapped = self.wrap_command(command, token).encode(self.encoding, errors="replace")

        async with self._lock:
            process = self.process
            if (
                process is None
                or process.stdin is None
                or process.stdout is None
                or process.returncode is not None
            ):
                raise RuntimeError(f"{self.__class__.__name__} session is not connected.")

            process.stdin.write(wrapped)
            await process.stdin.drain()

            output_parts: list[str] = []
            exit_code: int | None = None
            while True:
                line_bytes = await process.stdout.readline()
                if line_bytes == b"":
                    if process.returncode is not None:
                        raise RuntimeError(f"{self.__class__.__name__} session exited unexpectedly.")
                    await asyncio.sleep(0)
                    continue

                line = line_bytes.decode(self.encoding, errors="replace")
                parsed = self.parse_sentinel(line, token)
                if parsed is not None:
                    exit_code = parsed
                    break

                output_parts.append(line)

        result = ProcessResult(
            command=command,
            output=self.clean_output("".join(output_parts)),
            exit_code=exit_code if exit_code is not None else 1,
        )
        if check:
            result.check_returncode()
        return result

    async def text(self, command: str, *, check: bool = True) -> str:
        return (await self.run(command, check=check)).output

    async def close(self) -> None:
        if self._closed:
            return

        async with self._start_lock:
            if self._closed:
                return

            process = self.process
            if process is None:
                self._closed = True
                return

            async with self._lock:
                if process.returncode is None and process.stdin is not None:
                    try:
                        process.stdin.write(f"{self.exit_command}{self.line_ending}".encode(self.encoding))
                        await process.stdin.drain()
                        await asyncio.wait_for(process.wait(), timeout=2)
                    except Exception:
                        if process.returncode is None:
                            try:
                                process.terminate()
                            except ProcessLookupError:
                                pass
                        try:
                            await asyncio.wait_for(process.wait(), timeout=2)
                        except asyncio.TimeoutError:
                            if process.returncode is None:
                                try:
                                    process.kill()
                                except ProcessLookupError:
                                    pass
                            await process.wait()

                if process.stdin is not None:
                    process.stdin.close()
                    try:
                        await process.stdin.wait_closed()
                    except (BrokenPipeError, ConnectionResetError):
                        pass

                self._closed = True
