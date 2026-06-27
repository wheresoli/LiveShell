from __future__ import annotations

import contextlib
import io
import json
import tempfile
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from liveshell.cli import main  # noqa: E402
from liveshell import Bash, Cmd  # noqa: E402
from liveshell.models import CommandSpec, SessionSpec  # noqa: E402
from liveshell.store import Store  # noqa: E402


def available_process_shell() -> tuple[str, str] | None:
    if Cmd.is_available():
        return "cmd", "echo liveshell-cli-run-ok"
    if Bash.is_available():
        return "bash", "printf liveshell-cli-run-ok"
    return None


class CliTests(unittest.TestCase):
    def test_capability_discover_succeeds_as_json(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(["capability", "discover"])

        payload = json.loads(output.getvalue())
        capability_names = {
            capability["name"] for capability in payload["result"]["capabilities"]
        }

        self.assertEqual(status, 0)
        self.assertTrue(payload["ok"])
        self.assertIn("command.poll", capability_names)

    def test_daemon_stdio_once_prints_protocol_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = io.StringIO()
            request = json.dumps(
                {"id": "req_1", "method": "capability.discover", "params": {}}
            )
            with (
                contextlib.redirect_stdout(output),
                mock.patch("sys.stdin", io.StringIO(request + "\n")),
            ):
                status = main(["daemon", "stdio", "--state-dir", temp_dir, "--once"])

            response = json.loads(output.getvalue())

            self.assertEqual(status, 0)
            self.assertEqual(response["id"], "req_1")
            self.assertTrue(response["ok"])
            self.assertIn("capabilities", response["result"])

    def test_session_list_and_snapshot_succeed_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store.from_state_dir(temp_dir)
            session = store.create_session(
                SessionSpec(kind="cmd", cwd=temp_dir, metadata={"purpose": "cli-test"}),
                status="closed",
            )

            list_output = io.StringIO()
            with contextlib.redirect_stdout(list_output):
                list_status = main(["session", "list", "--state-dir", temp_dir])

            snapshot_output = io.StringIO()
            with contextlib.redirect_stdout(snapshot_output):
                snapshot_status = main(
                    [
                        "session",
                        "snapshot",
                        "--session-id",
                        session.id,
                        "--state-dir",
                        temp_dir,
                    ]
                )

            list_payload = json.loads(list_output.getvalue())
            snapshot_payload = json.loads(snapshot_output.getvalue())

            self.assertEqual(list_status, 0)
            self.assertEqual(snapshot_status, 0)
            self.assertEqual(list_payload["result"][0]["id"], session.id)
            self.assertEqual(snapshot_payload["result"]["metadata"]["purpose"], "cli-test")

    def test_command_read_commands_succeed_as_json_for_terminal_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store.from_state_dir(temp_dir)
            session = store.create_session(SessionSpec(kind="cmd"), status="closed")
            command = store.create_command(
                CommandSpec(session_id=session.id, command="echo hello"),
                status="running",
            )
            store.append_command_event(command.id, "stdout", "hello\n")
            store.append_command_event(command.id, "command_completed")
            store.update_command(command.id, status="completed", exit_code=0)

            poll_output = io.StringIO()
            with contextlib.redirect_stdout(poll_output):
                poll_status = main(
                    ["command", "poll", "--command-id", command.id, "--state-dir", temp_dir]
                )

            events_output = io.StringIO()
            with contextlib.redirect_stdout(events_output):
                events_status = main(
                    [
                        "command",
                        "events",
                        "--command-id",
                        command.id,
                        "--since-seq",
                        "0",
                        "--state-dir",
                        temp_dir,
                    ]
                )

            result_output = io.StringIO()
            with contextlib.redirect_stdout(result_output):
                result_status = main(
                    [
                        "command",
                        "result",
                        "--command-id",
                        command.id,
                        "--state-dir",
                        temp_dir,
                    ]
                )

            cancel_output = io.StringIO()
            with contextlib.redirect_stdout(cancel_output):
                cancel_status = main(
                    [
                        "command",
                        "cancel",
                        "--command-id",
                        command.id,
                        "--state-dir",
                        temp_dir,
                    ]
                )

            poll_payload = json.loads(poll_output.getvalue())
            events_payload = json.loads(events_output.getvalue())
            result_payload = json.loads(result_output.getvalue())
            cancel_payload = json.loads(cancel_output.getvalue())

            self.assertEqual(poll_status, 0)
            self.assertEqual(events_status, 0)
            self.assertEqual(result_status, 0)
            self.assertEqual(cancel_status, 0)
            self.assertEqual(poll_payload["result"]["status"], "completed")
            self.assertEqual(events_payload["result"][0]["text"], "hello\n")
            self.assertEqual(result_payload["result"]["stdout"], "hello\n")
            self.assertEqual(cancel_payload["result"]["status"], "completed")

    def test_run_executes_command_through_stdio_daemon_client(self) -> None:
        shell = available_process_shell()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        kind, command = shell

        with tempfile.TemporaryDirectory() as temp_dir:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(
                    [
                        "run",
                        "--kind",
                        kind,
                        "--command",
                        command,
                        "--timeout-seconds",
                        "5",
                        "--poll-interval",
                        "0.05",
                        "--state-dir",
                        temp_dir,
                    ]
                )

            payload = json.loads(output.getvalue())

            self.assertEqual(status, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["result"]["command"]["status"], "completed")
            self.assertIn("liveshell-cli-run-ok", payload["result"]["stdout"])
            self.assertEqual(payload["result"]["closed_session"]["status"], "closed")

    def test_session_create_without_live_daemon_fails_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(
                    [
                        "session",
                        "create",
                        "--kind",
                        "cmd",
                        "--state-dir",
                        temp_dir,
                    ]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(status, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["type"], "RuntimeError")

    def test_command_start_without_live_daemon_fails_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(
                    [
                        "command",
                        "start",
                        "--session-id",
                        "sess_missing",
                        "--command",
                        "echo hello",
                        "--state-dir",
                        temp_dir,
                    ]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(status, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["type"], "RuntimeError")

    def test_command_cancel_without_live_daemon_fails_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store.from_state_dir(temp_dir)
            session = store.create_session(SessionSpec(kind="cmd"), status="running")
            command = store.create_command(
                CommandSpec(session_id=session.id, command="echo hello"),
                status="running",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(
                    [
                        "command",
                        "cancel",
                        "--command-id",
                        command.id,
                        "--state-dir",
                        temp_dir,
                    ]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(status, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["type"], "RuntimeError")
            self.assertEqual(store.get_command(command.id).status, "running")

    def test_command_events_for_unknown_command_fails_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(
                    [
                        "command",
                        "events",
                        "--command-id",
                        "cmd_missing",
                        "--state-dir",
                        temp_dir,
                    ]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(status, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["type"], "KeyError")


if __name__ == "__main__":
    unittest.main()
