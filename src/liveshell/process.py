from __future__ import annotations

import locale
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Sequence

from .runtime import ProcessBackedSession
from .utils import Environment


@dataclass(frozen=True)
class ProcessResult:
    command: str
    output: str
    exit_code: int

    def check_returncode(self) -> None:
        if self.exit_code != 0:
            raise RuntimeError(self.output)


class ProcessSession(ProcessBackedSession):
    executable_names: Sequence[str] = ()
    startup_args: Sequence[str] = ()
    exit_command = "exit"
    line_ending = "\n"
    sentinel_prefix = "__LIVESHELL_DONE__"

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

    @classmethod
    def find(cls, path: str | os.PathLike[str] | None = None, **kwargs) -> Path | None:
        if path is not None:
            executable = Path(path)
            if executable.exists():
                return executable
            raise RuntimeError(f"Invalid executable path: {path}")
        return Environment.executable(*cls.executable_names)

    @classmethod
    def is_available(cls) -> bool:
        return cls.find() is not None

    def is_running(self) -> bool:
        return not self._closed and self.process.poll() is None

    def run(self, command: str, *, check: bool = True) -> ProcessResult:
        if not self.is_running():
            raise RuntimeError(f"{self.__class__.__name__} session is closed.")

        token = uuid.uuid4().hex
        wrapped = self.wrap_command(command, token)

        with self._lock:
            if self.process.stdin is None or self.process.stdout is None:
                raise RuntimeError(f"{self.__class__.__name__} session is not connected.")

            self.process.stdin.write(wrapped)
            self.process.stdin.flush()

            output_parts: list[str] = []
            exit_code: int | None = None
            while True:
                line = self.process.stdout.readline()
                if line == "":
                    if self.process.poll() is not None:
                        raise RuntimeError(f"{self.__class__.__name__} session exited unexpectedly.")
                    continue

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

    def text(self, command: str, *, check: bool = True) -> str:
        return self.run(command, check=check).output

    def clean_output(self, output: str) -> str:
        return output.rstrip()

    def wrap_command(self, command: str, token: str) -> str:
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

    def close(self) -> None:
        if self._closed:
            return

        if self.process.poll() is None and self.process.stdin is not None:
            try:
                self.process.stdin.write(f"{self.exit_command}{self.line_ending}")
                self.process.stdin.flush()
                self.process.wait(timeout=2)
            except Exception:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()

        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()

        self._closed = True
