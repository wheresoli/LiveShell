from __future__ import annotations

import locale
import subprocess

from .process import AsyncProcessSession, ProcessSession


class BashMixin:
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

    def wrap_command(
        self,
        command: str,
        token: str,
        *,
        stderr_path: str | None = None,
    ) -> str:
        if stderr_path is None:
            return (
                f"{command}\n"
                f"printf '\\n{self.sentinel_prefix}:{token}:%s\\n' \"$?\"\n"
            )
        stderr_path = self._quote_path(stderr_path)
        return (
            f"{{ {command}; }} 2> {stderr_path}\n"
            f"__LIVESHELL_EXIT=$?\n"
            f"printf '\\n{self.stderr_begin_prefix}:{token}\\n'\n"
            f"cat {stderr_path}\n"
            f"printf '\\n{self.stderr_end_prefix}:{token}\\n'\n"
            f"rm -f {stderr_path}\n"
            f"printf '\\n{self.sentinel_prefix}:{token}:%s\\n' \"$__LIVESHELL_EXIT\"\n"
        )

    @staticmethod
    def _quote_path(path: str) -> str:
        return "'" + path.replace("'", "'\"'\"'") + "'"


class Bash(BashMixin, ProcessSession):
    pass


class AsyncBash(BashMixin, AsyncProcessSession):
    pass
