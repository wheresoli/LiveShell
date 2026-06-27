from __future__ import annotations

import hashlib
import io
import json
import tempfile
import time
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from liveshell import Bash, Cmd  # noqa: E402
from liveshell.daemon import JsonLineDaemon, LiveShellService  # noqa: E402
from liveshell.models import CommandSpec, SessionSpec  # noqa: E402
from liveshell.powershell import PowerShellResult  # noqa: E402
from liveshell.store import Store  # noqa: E402


def available_process_shell() -> tuple[str, str, str, str] | None:
    if Cmd.is_available():
        return (
            "cmd",
            "echo liveshell-ok",
            "ping -n 10 127.0.0.1 >NUL",
            'cmd /c "echo liveshell-failed & exit /b 7"',
        )
    if Bash.is_available():
        return "bash", "printf liveshell-ok", "sleep 10", "printf liveshell-failed; false"
    return None


def persistent_process_shell() -> tuple[str, str, str] | None:
    if Cmd.is_available():
        return (
            "cmd",
            "set LIVESHELL_DAEMON_PERSIST=liveshell-persist-ok",
            "echo %LIVESHELL_DAEMON_PERSIST%",
        )
    if Bash.is_available():
        return (
            "bash",
            "LIVESHELL_DAEMON_PERSIST=liveshell-persist-ok",
            'printf "%s" "$LIVESHELL_DAEMON_PERSIST"',
        )
    return None


def streaming_process_shell() -> tuple[str, str] | None:
    if Cmd.is_available():
        return (
            "cmd",
            "echo liveshell-stream-start & ping -n 4 127.0.0.1 >NUL & echo liveshell-stream-end",
        )
    if Bash.is_available():
        return (
            "bash",
            "printf 'liveshell-stream-start\\n'; sleep 3; printf 'liveshell-stream-end\\n'",
        )
    return None


def stderr_process_shell() -> tuple[str, str] | None:
    if Cmd.is_available():
        return "cmd", "echo liveshell-stdout & echo liveshell-stderr 1>&2"
    if Bash.is_available():
        return "bash", "printf 'liveshell-stdout\\n'; printf 'liveshell-stderr\\n' >&2"
    return None


class DaemonTests(unittest.TestCase):
    def test_protocol_handler_returns_capabilities_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            daemon = JsonLineDaemon(LiveShellService(Store.from_state_dir(temp_dir)))

            response = daemon.handle_request(
                {"id": "req_1", "method": "capability.discover", "params": {}}
            )
            encoded = json.dumps(response)

            self.assertTrue(response["ok"])
            self.assertEqual(response["id"], "req_1")
            self.assertIsInstance(encoded, str)
            self.assertIn("capabilities", response["result"])

    def test_protocol_validation_returns_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            daemon = JsonLineDaemon(LiveShellService(Store.from_state_dir(temp_dir)))

            missing_method = daemon.handle_request({"id": "req_missing", "params": {}})
            bad_params = daemon.handle_request(
                {"id": "req_params", "method": "capability.discover", "params": []}
            )

            self.assertFalse(missing_method["ok"])
            self.assertEqual(missing_method["error"]["type"], "ValueError")
            self.assertFalse(bad_params["ok"])
            self.assertEqual(bad_params["error"]["type"], "ValueError")

    def test_protocol_accepts_null_params_and_missing_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            daemon = JsonLineDaemon(LiveShellService(Store.from_state_dir(temp_dir)))

            response = daemon.handle_request(
                {"method": "session.list", "params": None}
            )

            self.assertIsNone(response["id"])
            self.assertTrue(response["ok"])
            self.assertEqual(response["result"], [])

    def test_protocol_reports_unknown_methods_and_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            daemon = JsonLineDaemon(LiveShellService(Store.from_state_dir(temp_dir)))

            unknown_method = daemon.handle_request(
                {"id": "req_unknown", "method": "missing.method", "params": {}}
            )
            unknown_session = daemon.handle_request(
                {
                    "id": "req_session",
                    "method": "session.snapshot",
                    "params": {"session_id": "sess_missing"},
                }
            )
            unknown_command = daemon.handle_request(
                {
                    "id": "req_command",
                    "method": "command.poll",
                    "params": {"command_id": "cmd_missing"},
                }
            )

            self.assertFalse(unknown_method["ok"])
            self.assertEqual(unknown_method["error"]["type"], "ValueError")
            self.assertFalse(unknown_session["ok"])
            self.assertEqual(unknown_session["error"]["type"], "KeyError")
            self.assertFalse(unknown_command["ok"])
            self.assertEqual(unknown_command["error"]["type"], "KeyError")

    def test_stdio_loop_reports_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            daemon = JsonLineDaemon(LiveShellService(Store.from_state_dir(temp_dir)))
            output_stream = io.StringIO()

            daemon.serve_stdio(
                input_stream=io.StringIO("{not-json}\n"),
                output_stream=output_stream,
            )

            response = json.loads(output_stream.getvalue())

            self.assertIsNone(response["id"])
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"]["type"], "JSONDecodeError")

    def test_stdio_loop_handles_multiple_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            daemon = JsonLineDaemon(LiveShellService(Store.from_state_dir(temp_dir)))
            input_stream = io.StringIO(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "id": "req_1",
                                "method": "capability.discover",
                                "params": {},
                            }
                        ),
                        json.dumps(
                            {
                                "id": "req_2",
                                "method": "session.list",
                                "params": {},
                            }
                        ),
                    ]
                )
                + "\n"
            )
            output_stream = io.StringIO()

            daemon.serve_stdio(input_stream=input_stream, output_stream=output_stream)

            responses = [
                json.loads(line)
                for line in output_stream.getvalue().splitlines()
                if line.strip()
            ]
            self.assertEqual([response["id"] for response in responses], ["req_1", "req_2"])
            self.assertTrue(all(response["ok"] for response in responses))
            self.assertIn("capabilities", responses[0]["result"])
            self.assertEqual(responses[1]["result"], [])

    def test_session_command_result_and_event_replay(self) -> None:
        shell = available_process_shell()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        kind, command, _, _ = shell

        with tempfile.TemporaryDirectory() as temp_dir:
            service = LiveShellService(Store.from_state_dir(temp_dir))
            session = service.create_session(kind)
            try:
                handle = service.start_command(session.id, command, timeout_seconds=5)
                result = handle.wait(poll_interval=0.05)
                events = handle.events()
                replayed = handle.events(since_seq=events[0].seq)

                self.assertEqual(result.command.status, "completed")
                self.assertIn("liveshell-ok", result.stdout)
                self.assertEqual([event.seq for event in events], sorted(event.seq for event in events))
                self.assertEqual([event.seq for event in replayed], [event.seq for event in events[1:]])
            finally:
                service.close_session(session.id)

    def test_commands_preserve_session_state_across_same_daemon_session(self) -> None:
        shell = persistent_process_shell()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        kind, set_command, read_command = shell

        with tempfile.TemporaryDirectory() as temp_dir:
            service = LiveShellService(Store.from_state_dir(temp_dir))
            session = service.create_session(kind)
            try:
                set_result = service.start_command(
                    session.id,
                    set_command,
                    timeout_seconds=5,
                ).wait(poll_interval=0.05)
                read_result = service.start_command(
                    session.id,
                    read_command,
                    timeout_seconds=5,
                ).wait(poll_interval=0.05)

                self.assertEqual(set_result.command.status, "completed")
                self.assertEqual(read_result.command.status, "completed")
                self.assertIn("liveshell-persist-ok", read_result.stdout)
            finally:
                latest = service.session_snapshot(session.id)
                if latest.status == "running":
                    service.close_session(session.id)

    def test_process_backed_commands_stream_stdout_events_before_completion(self) -> None:
        shell = streaming_process_shell()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        kind, command = shell

        with tempfile.TemporaryDirectory() as temp_dir:
            service = LiveShellService(Store.from_state_dir(temp_dir))
            session = service.create_session(kind)
            try:
                handle = service.start_command(session.id, command, timeout_seconds=10)
                observed_streaming_event = False
                deadline = time.monotonic() + 6

                while time.monotonic() < deadline:
                    snapshot = handle.poll()
                    stdout_events = [
                        event
                        for event in handle.events()
                        if event.event_type == "stdout"
                    ]
                    if any("liveshell-stream-start" in event.text for event in stdout_events):
                        observed_streaming_event = (
                            snapshot.status not in {"completed", "failed", "timed_out", "canceled"}
                        )
                        break
                    time.sleep(0.05)

                result = handle.wait(poll_interval=0.05)
                stdout = "".join(
                    event.text for event in result.events if event.event_type == "stdout"
                )

                self.assertTrue(observed_streaming_event)
                self.assertIn("liveshell-stream-start", stdout)
                self.assertIn("liveshell-stream-end", result.stdout)
                self.assertEqual(result.stdout, stdout)
            finally:
                latest = service.session_snapshot(session.id)
                if latest.status == "running":
                    service.close_session(session.id)

    def test_process_backed_commands_record_stderr_separately(self) -> None:
        shell = stderr_process_shell()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        kind, command = shell

        with tempfile.TemporaryDirectory() as temp_dir:
            service = LiveShellService(Store.from_state_dir(temp_dir))
            session = service.create_session(kind)
            try:
                result = service.start_command(
                    session.id,
                    command,
                    timeout_seconds=5,
                ).wait(poll_interval=0.05)
                stdout_events = [
                    event for event in result.events if event.event_type == "stdout"
                ]
                stderr_events = [
                    event for event in result.events if event.event_type == "stderr"
                ]

                self.assertEqual(result.command.status, "completed")
                self.assertIn("liveshell-stdout", result.stdout)
                self.assertIn("liveshell-stderr", result.stderr)
                self.assertNotIn("liveshell-stderr", result.stdout)
                self.assertTrue(stdout_events)
                self.assertTrue(stderr_events)
                self.assertEqual(result.command.stderr_tail, result.stderr)
            finally:
                latest = service.session_snapshot(session.id)
                if latest.status == "running":
                    service.close_session(session.id)

    def test_completed_command_records_output_hash_and_tails(self) -> None:
        shell = available_process_shell()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        kind, command, _, _ = shell

        with tempfile.TemporaryDirectory() as temp_dir:
            service = LiveShellService(Store.from_state_dir(temp_dir))
            session = service.create_session(kind)
            try:
                handle = service.start_command(session.id, command, timeout_seconds=5)
                result = handle.wait(poll_interval=0.05)
                output = result.stdout + result.stderr
                expected_hash = hashlib.sha256(
                    output.encode("utf-8", errors="replace")
                ).hexdigest()

                self.assertEqual(result.command.output_hash, expected_hash)
                self.assertEqual(result.command.stdout_tail, result.stdout)
                self.assertEqual(result.command.stderr_tail, result.stderr)
            finally:
                service.close_session(session.id)

    def test_protocol_session_create_command_start_poll_result_events(self) -> None:
        shell = available_process_shell()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        kind, command, _, _ = shell

        with tempfile.TemporaryDirectory() as temp_dir:
            daemon = JsonLineDaemon(LiveShellService(Store.from_state_dir(temp_dir)))

            create_response = daemon.handle_request(
                {
                    "id": "req_create",
                    "method": "session.create",
                    "params": {"kind": kind},
                }
            )
            self.assertTrue(create_response["ok"])
            session_id = create_response["result"]["id"]

            start_response = daemon.handle_request(
                {
                    "id": "req_start",
                    "method": "command.start",
                    "params": {
                        "session_id": session_id,
                        "command": command,
                        "timeout_seconds": 5,
                    },
                }
            )
            self.assertTrue(start_response["ok"])
            command_id = start_response["result"]["command_id"]

            deadline = time.monotonic() + 5
            while True:
                poll_response = daemon.handle_request(
                    {
                        "id": "req_poll",
                        "method": "command.poll",
                        "params": {"command_id": command_id},
                    }
                )
                self.assertTrue(poll_response["ok"])
                if poll_response["result"]["status"] in {
                    "completed",
                    "failed",
                    "timed_out",
                    "canceled",
                }:
                    break
                if time.monotonic() >= deadline:
                    self.fail("Timed out waiting for protocol command completion")
                time.sleep(0.05)

            result_response = daemon.handle_request(
                {
                    "id": "req_result",
                    "method": "command.result",
                    "params": {"command_id": command_id},
                }
            )
            events_response = daemon.handle_request(
                {
                    "id": "req_events",
                    "method": "command.events",
                    "params": {"command_id": command_id, "since_seq": 0},
                }
            )

            self.assertTrue(result_response["ok"])
            self.assertTrue(events_response["ok"])
            self.assertEqual(result_response["result"]["command"]["status"], "completed")
            self.assertIn("liveshell-ok", result_response["result"]["stdout"])
            self.assertIn(
                "command_completed",
                {event["event_type"] for event in events_response["result"]},
            )

            close_response = daemon.handle_request(
                {
                    "id": "req_close",
                    "method": "session.close",
                    "params": {"session_id": session_id},
                }
            )
            self.assertTrue(close_response["ok"])

    def test_command_start_rejects_unsupported_per_command_cwd(self) -> None:
        shell = available_process_shell()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        kind, command, _, _ = shell

        with tempfile.TemporaryDirectory() as temp_dir:
            daemon = JsonLineDaemon(LiveShellService(Store.from_state_dir(temp_dir)))
            create_response = daemon.handle_request(
                {
                    "id": "req_create",
                    "method": "session.create",
                    "params": {"kind": kind},
                }
            )
            self.assertTrue(create_response["ok"])

            start_response = daemon.handle_request(
                {
                    "id": "req_start",
                    "method": "command.start",
                    "params": {
                        "session_id": create_response["result"]["id"],
                        "command": command,
                        "cwd": temp_dir,
                    },
                }
            )

            self.assertFalse(start_response["ok"])
            self.assertEqual(start_response["error"]["type"], "ValueError")
            self.assertEqual(Store.from_state_dir(temp_dir).list_commands(), [])

    def test_command_start_rejects_invalid_timeout_before_creating_record(self) -> None:
        shell = available_process_shell()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        kind, command, _, _ = shell

        with tempfile.TemporaryDirectory() as temp_dir:
            daemon = JsonLineDaemon(LiveShellService(Store.from_state_dir(temp_dir)))
            create_response = daemon.handle_request(
                {
                    "id": "req_create",
                    "method": "session.create",
                    "params": {"kind": kind},
                }
            )
            self.assertTrue(create_response["ok"])

            for timeout_seconds in (0, -1, "soon", True):
                start_response = daemon.handle_request(
                    {
                        "id": "req_start",
                        "method": "command.start",
                        "params": {
                            "session_id": create_response["result"]["id"],
                            "command": command,
                            "timeout_seconds": timeout_seconds,
                        },
                    }
                )
                self.assertFalse(start_response["ok"])
                self.assertEqual(start_response["error"]["type"], "ValueError")

            self.assertEqual(Store.from_state_dir(temp_dir).list_commands(), [])

    def test_command_start_allows_matching_session_cwd(self) -> None:
        shell = available_process_shell()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        kind, command, _, _ = shell

        with tempfile.TemporaryDirectory() as temp_dir:
            service = LiveShellService(Store.from_state_dir(temp_dir))
            session = service.create_session(kind, cwd=temp_dir)
            try:
                handle = service.start_command(
                    session.id,
                    command,
                    cwd=temp_dir,
                    timeout_seconds=5,
                )
                result = handle.wait(poll_interval=0.05)

                self.assertEqual(result.command.status, "completed")
                self.assertIn("liveshell-ok", result.stdout)
            finally:
                service.close_session(session.id)

    def test_session_create_passes_cwd_to_hosted_session_types(self) -> None:
        class FakeHostedSession:
            created_cwd = None

            def __init__(self, *, cwd=None):
                type(self).created_cwd = cwd
                self._closed = False

            def is_running(self):
                return not self._closed

            def close(self):
                self._closed = True

        original_session_type = LiveShellService._session_type
        LiveShellService._session_type = staticmethod(lambda kind: FakeHostedSession)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                service = LiveShellService(Store.from_state_dir(temp_dir))
                session = service.create_session("powershell", cwd=temp_dir)

                self.assertEqual(FakeHostedSession.created_cwd, temp_dir)
                self.assertEqual(session.cwd, temp_dir)
        finally:
            LiveShellService._session_type = staticmethod(original_session_type)

    def test_hosted_session_result_success_is_recorded_without_process_backend(self) -> None:
        class FakeHostedSession:
            def __init__(self, *, cwd=None):
                self._closed = False

            def is_running(self):
                return not self._closed

            def run_result(self, command, *, check=True):
                return PowerShellResult(
                    command=command,
                    output=["hosted stdout"],
                    errors=[],
                    had_errors=False,
                    exit_code=0,
                )

            def close(self):
                self._closed = True

        original_session_type = LiveShellService._session_type
        LiveShellService._session_type = staticmethod(lambda kind: FakeHostedSession)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                service = LiveShellService(Store.from_state_dir(temp_dir))
                session = service.create_session("powershell")
                try:
                    result = service.start_command(
                        session.id,
                        "Write-Output 'hosted stdout'",
                        timeout_seconds=5,
                    ).wait(poll_interval=0.05)

                    self.assertEqual(result.command.status, "completed")
                    self.assertEqual(result.stdout, "hosted stdout")
                    self.assertEqual(result.stderr, "")
                finally:
                    service.close_session(session.id)
        finally:
            LiveShellService._session_type = staticmethod(original_session_type)

    def test_hosted_session_result_errors_fail_command_with_stderr(self) -> None:
        class FakeHostedSession:
            def __init__(self, *, cwd=None):
                self._closed = False

            def is_running(self):
                return not self._closed

            def run_result(self, command, *, check=True):
                return PowerShellResult(
                    command=command,
                    output=["hosted stdout"],
                    errors=["hosted stderr"],
                    had_errors=True,
                    exit_code=1,
                )

            def close(self):
                self._closed = True

        original_session_type = LiveShellService._session_type
        LiveShellService._session_type = staticmethod(lambda kind: FakeHostedSession)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                service = LiveShellService(Store.from_state_dir(temp_dir))
                session = service.create_session("powershell")
                try:
                    result = service.start_command(
                        session.id,
                        "Write-Error 'hosted stderr'",
                        timeout_seconds=5,
                    ).wait(poll_interval=0.05)
                    event_types = {event.event_type for event in result.events}

                    self.assertEqual(result.command.status, "failed")
                    self.assertEqual(result.command.exit_code, 1)
                    self.assertEqual(result.stdout, "hosted stdout")
                    self.assertEqual(result.stderr, "hosted stderr")
                    self.assertIn("stdout", event_types)
                    self.assertIn("stderr", event_types)
                    self.assertIn("command_failed", event_types)
                finally:
                    service.close_session(session.id)
        finally:
            LiveShellService._session_type = staticmethod(original_session_type)

    def test_hosted_native_exit_code_fails_command_without_error_record(self) -> None:
        class FakeHostedSession:
            def __init__(self, *, cwd=None):
                self._closed = False

            def is_running(self):
                return not self._closed

            def run_result(self, command, *, check=True):
                return PowerShellResult(
                    command=command,
                    output=["native stdout"],
                    errors=[],
                    had_errors=False,
                    exit_code=7,
                    last_exit_code=7,
                )

            def close(self):
                self._closed = True

        original_session_type = LiveShellService._session_type
        LiveShellService._session_type = staticmethod(lambda kind: FakeHostedSession)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                service = LiveShellService(Store.from_state_dir(temp_dir))
                session = service.create_session("powershell")
                try:
                    result = service.start_command(
                        session.id,
                        "cmd /c exit /b 7",
                        timeout_seconds=5,
                    ).wait(poll_interval=0.05)
                    event_types = {event.event_type for event in result.events}

                    self.assertEqual(result.command.status, "failed")
                    self.assertEqual(result.command.exit_code, 7)
                    self.assertEqual(result.stdout, "native stdout")
                    self.assertEqual(result.stderr, "")
                    self.assertIn("stdout", event_types)
                    self.assertIn("command_failed", event_types)
                finally:
                    service.close_session(session.id)
        finally:
            LiveShellService._session_type = staticmethod(original_session_type)

    def test_cancel_running_command_marks_terminal_canceled(self) -> None:
        shell = available_process_shell()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        kind, _, long_command, _ = shell

        with tempfile.TemporaryDirectory() as temp_dir:
            service = LiveShellService(Store.from_state_dir(temp_dir))
            session = service.create_session(kind)

            handle = service.start_command(session.id, long_command, timeout_seconds=30)
            time.sleep(0.2)
            snapshot = handle.cancel("test cancel")
            events = handle.events()

            self.assertEqual(snapshot.status, "canceled")
            self.assertIn("command_canceled", {event.event_type for event in events})

    def test_immediate_cancel_does_not_move_command_back_to_running(self) -> None:
        shell = available_process_shell()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        kind, _, long_command, _ = shell

        with tempfile.TemporaryDirectory() as temp_dir:
            service = LiveShellService(Store.from_state_dir(temp_dir))
            session = service.create_session(kind)

            handle = service.start_command(session.id, long_command, timeout_seconds=30)
            snapshot = handle.cancel("immediate cancel")

            self.assertEqual(snapshot.status, "canceled")
            self.assertEqual(handle.poll().status, "canceled")

    def test_session_close_cancels_running_commands(self) -> None:
        shell = available_process_shell()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        kind, _, long_command, _ = shell

        with tempfile.TemporaryDirectory() as temp_dir:
            service = LiveShellService(Store.from_state_dir(temp_dir))
            session = service.create_session(kind)
            handle = service.start_command(session.id, long_command, timeout_seconds=30)

            time.sleep(0.2)
            closed = service.close_session(session.id)
            command = handle.poll()
            events = handle.events()

            self.assertEqual(closed.status, "closed")
            self.assertEqual(command.status, "canceled")
            self.assertIn(command.id, closed.metadata["closed_running_commands"])
            self.assertIn("command_canceled", {event.event_type for event in events})

    def test_immediate_session_close_does_not_move_command_back_to_running(self) -> None:
        shell = available_process_shell()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        kind, _, long_command, _ = shell

        with tempfile.TemporaryDirectory() as temp_dir:
            service = LiveShellService(Store.from_state_dir(temp_dir))
            session = service.create_session(kind)
            handle = service.start_command(session.id, long_command, timeout_seconds=30)

            closed = service.close_session(session.id)
            command = handle.poll()

            self.assertEqual(closed.status, "closed")
            self.assertEqual(command.status, "canceled")

    def test_timeout_marks_command_timed_out(self) -> None:
        shell = available_process_shell()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        kind, _, long_command, _ = shell

        with tempfile.TemporaryDirectory() as temp_dir:
            service = LiveShellService(Store.from_state_dir(temp_dir))
            session = service.create_session(kind)

            handle = service.start_command(session.id, long_command, timeout_seconds=0.1)
            result = handle.wait(poll_interval=0.05)

            self.assertEqual(result.command.status, "timed_out")
            self.assertIn("command_timed_out", {event.event_type for event in result.events})

    def test_failed_command_preserves_stdout_without_stderr_duplication(self) -> None:
        shell = available_process_shell()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        kind, _, _, failing_command = shell

        with tempfile.TemporaryDirectory() as temp_dir:
            service = LiveShellService(Store.from_state_dir(temp_dir))
            session = service.create_session(kind)
            try:
                handle = service.start_command(session.id, failing_command, timeout_seconds=5)
                result = handle.wait(poll_interval=0.05)

                self.assertEqual(result.command.status, "failed")
                self.assertIn("liveshell-failed", result.stdout)
                self.assertEqual(result.stderr, "")
            finally:
                service.close_session(session.id)

    def test_recovery_marks_running_records_honestly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store.from_state_dir(temp_dir)
            session = store.create_session(SessionSpec(kind="cmd"), status="running")
            command = store.create_command(
                CommandSpec(session_id=session.id, command="echo unfinished"),
                status="running",
            )

            LiveShellService(store)

            recovered_session = store.get_session(session.id)
            recovered_command = store.get_command(command.id)
            self.assertEqual(recovered_session.status, "crashed")
            self.assertEqual(recovered_command.status, "failed")
            self.assertTrue(recovered_session.metadata["recovered_without_process_handle"])
            self.assertTrue(recovered_command.metadata["recovered_without_worker"])


if __name__ == "__main__":
    unittest.main()
