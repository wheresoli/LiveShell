from __future__ import annotations

import asyncio
import tempfile
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from liveshell import Bash, Cmd  # noqa: E402
from liveshell.client import LiveShellClient, LiveShellResponseError  # noqa: E402


def available_process_shell() -> tuple[str, str] | None:
    if Cmd.is_available():
        return "cmd", "echo liveshell-client-ok"
    if Bash.is_available():
        return "bash", "printf liveshell-client-ok"
    return None


class LiveShellClientTests(unittest.TestCase):
    def test_stdio_client_discovers_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with LiveShellClient.stdio(temp_dir) as client:
                capabilities = client.discover_capabilities()

            self.assertIn("command.poll", {capability.name for capability in capabilities})

    def test_stdio_client_session_handle_runs_command_and_closes(self) -> None:
        shell = available_process_shell()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        kind, command = shell

        with tempfile.TemporaryDirectory() as temp_dir:
            with LiveShellClient.stdio(temp_dir) as client:
                session = client.create_session(kind)
                result = session.run(command, timeout_seconds=5, poll_interval=0.05)
                closed = session.close()

            self.assertEqual(result.command.status, "completed")
            self.assertIn("liveshell-client-ok", result.stdout)
            self.assertEqual(closed.status, "closed")

    def test_stdio_client_raises_response_error_for_daemon_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with LiveShellClient.stdio(temp_dir) as client:
                with self.assertRaises(LiveShellResponseError) as context:
                    client.session_snapshot("sess_missing")

            self.assertEqual(context.exception.error_type, "KeyError")


class AsyncLiveShellClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_client_methods_wrap_sync_protocol_operations(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0
        with tempfile.TemporaryDirectory() as temp_dir:
            client = LiveShellClient.stdio(temp_dir)
            try:
                capabilities = await client.discover_capabilities_async()
            finally:
                await client.close_async()

            self.assertIn("command.poll", {capability.name for capability in capabilities})


if __name__ == "__main__":
    unittest.main()
