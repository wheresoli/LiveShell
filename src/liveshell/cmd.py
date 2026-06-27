from __future__ import annotations

import re

from .process import AsyncProcessSession, ProcessSession


class CmdMixin:
    executable_names = ("cmd.exe", "cmd")
    startup_args = ("/D", "/Q", "/K")
    line_ending = "\r\n"

    def wrap_command(
        self,
        command: str,
        token: str,
        *,
        stderr_path: str | None = None,
    ) -> str:
        if stderr_path is None:
            return (
                f"{command}{self.line_ending}"
                f"echo {self.sentinel_prefix}:{token}:%ERRORLEVEL%{self.line_ending}"
            )
        stderr_path = self._quote_path(stderr_path)
        return (
            f"({command}) 2> {stderr_path}{self.line_ending}"
            f"set \"__LIVESHELL_EXIT=%ERRORLEVEL%\"{self.line_ending}"
            f"echo {self.stderr_begin_prefix}:{token}{self.line_ending}"
            f"type {stderr_path}{self.line_ending}"
            f"echo {self.stderr_end_prefix}:{token}{self.line_ending}"
            f"del /q {stderr_path} >NUL 2>NUL{self.line_ending}"
            f"echo {self.sentinel_prefix}:{token}:%__LIVESHELL_EXIT%{self.line_ending}"
        )

    def clean_output(self, output: str) -> str:
        without_prompts = re.sub(r"(?m)^[A-Za-z]:\\[^>\r\n]*>", "", output)
        return without_prompts.strip()

    def clean_stderr(self, output: str) -> str:
        without_prompts = re.sub(r"(?m)^[A-Za-z]:\\[^>\r\n]*>", "", output)
        return without_prompts.strip()

    @staticmethod
    def _quote_path(path: str) -> str:
        return '"' + path.replace('"', '""') + '"'


class Cmd(CmdMixin, ProcessSession):
    pass


class AsyncCmd(CmdMixin, AsyncProcessSession):
    pass
