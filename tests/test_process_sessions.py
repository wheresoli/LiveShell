from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from liveshell import AsyncBash, AsyncCmd, Bash, Cmd  # noqa: E402


def stderr_process_command() -> tuple[type, str] | None:
    if Cmd.is_available():
        return Cmd, "echo liveshell-stdout & echo liveshell-stderr 1>&2"
    if Bash.is_available():
        return Bash, "printf 'liveshell-stdout\\n'; printf 'liveshell-stderr\\n' >&2"
    return None


class ProcessSessionTests(unittest.TestCase):
    @unittest.skipUnless(Cmd.is_available(), "cmd.exe is not available")
    def test_cmd_session_persists_environment(self) -> None:
        with Cmd() as cmd:
            cmd.run("set LIVESHELL_TEST_VALUE=40")

            self.assertEqual(cmd.text("echo %LIVESHELL_TEST_VALUE%"), "40")

    @unittest.skipUnless(Cmd.is_available(), "cmd.exe is not available")
    def test_cmd_run_stream_invokes_stdout_callback(self) -> None:
        with Cmd() as cmd:
            chunks = []

            result = cmd.run_stream(
                "echo LIVESHELL_STREAM_CALLBACK",
                stdout_callback=chunks.append,
            )

            self.assertEqual(result.exit_code, 0)
            self.assertIn("LIVESHELL_STREAM_CALLBACK", "".join(chunks))

    def test_process_session_separates_stderr(self) -> None:
        shell = stderr_process_command()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        session_type, command = shell

        with session_type() as session:
            result = session.run(command)

            self.assertIn("liveshell-stdout", result.output)
            self.assertIn("liveshell-stderr", result.stderr)
            self.assertNotIn("liveshell-stderr", result.output)

    @unittest.skipUnless(Bash.is_available(), "bash is not available")
    def test_bash_session_persists_environment(self) -> None:
        with Bash() as bash:
            bash.run("LIVESHELL_TEST_VALUE=40")

            self.assertEqual(bash.text("printf '%s' \"$LIVESHELL_TEST_VALUE\""), "40")


class AsyncProcessSessionTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(AsyncCmd.is_available(), "cmd.exe is not available")
    async def test_cmd_session_persists_environment(self) -> None:
        async with AsyncCmd() as cmd:
            await cmd.run("set LIVESHELL_TEST_VALUE=40")

            self.assertEqual(await cmd.text("echo %LIVESHELL_TEST_VALUE%"), "40")

    @unittest.skipUnless(AsyncBash.is_available(), "bash is not available")
    async def test_bash_session_persists_environment(self) -> None:
        async with AsyncBash() as bash:
            await bash.run("LIVESHELL_TEST_VALUE=40")

            self.assertEqual(
                await bash.text("printf '%s' \"$LIVESHELL_TEST_VALUE\""),
                "40",
            )


if __name__ == "__main__":
    unittest.main()
