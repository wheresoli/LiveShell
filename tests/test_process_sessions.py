from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from liveshell import Bash, Cmd  # noqa: E402


class ProcessSessionTests(unittest.TestCase):
    @unittest.skipUnless(Cmd.is_available(), "cmd.exe is not available")
    def test_cmd_session_persists_environment(self) -> None:
        with Cmd() as cmd:
            cmd.run("set LIVESHELL_TEST_VALUE=40")

            self.assertEqual(cmd.text("echo %LIVESHELL_TEST_VALUE%"), "40")

    @unittest.skipUnless(Bash.is_available(), "bash is not available")
    def test_bash_session_persists_environment(self) -> None:
        with Bash() as bash:
            bash.run("LIVESHELL_TEST_VALUE=40")

            self.assertEqual(bash.text("printf '%s' \"$LIVESHELL_TEST_VALUE\""), "40")


if __name__ == "__main__":
    unittest.main()
