from __future__ import annotations

import locale
import subprocess

from .process import ProcessSession


class Bash(ProcessSession):
    executable_names = ("bash",)
    startup_args = ("--noprofile", "--norc")

    @classmethod
    def is_available(cls) -> bool:
        executable = cls.find()
        if executable is None:
            return False

        try:
            result = subprocess.run(
                [str(executable), *cls.startup_args, "-c", "printf ok"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding=locale.getpreferredencoding(False),
                errors="replace",
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False

        return result.returncode == 0 and result.stdout == "ok"

    def wrap_command(self, command: str, token: str) -> str:
        return (
            f"{command}\n"
            f"printf '\\n{self.sentinel_prefix}:{token}:%s\\n' \"$?\"\n"
        )
