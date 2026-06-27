from __future__ import annotations

import re

from .process import ProcessSession


class Cmd(ProcessSession):
    executable_names = ("cmd.exe", "cmd")
    startup_args = ("/D", "/Q", "/K")
    line_ending = "\r\n"

    def wrap_command(self, command: str, token: str) -> str:
        return (
            f"{command}{self.line_ending}"
            f"echo {self.sentinel_prefix}:{token}:%ERRORLEVEL%{self.line_ending}"
        )

    def clean_output(self, output: str) -> str:
        without_prompts = re.sub(r"(?m)^[A-Za-z]:\\[^>\r\n]*>", "", output)
        return without_prompts.strip()
