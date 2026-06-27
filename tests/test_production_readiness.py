from __future__ import annotations

import io
import contextlib
import sys
import tempfile
import threading
import time
import json

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import liveshell  # noqa: E402
from liveshell import AsyncBash, AsyncCmd, Bash, Cmd  # noqa: E402
from liveshell.client import LiveShellClient, LiveShellProtocolError  # noqa: E402
from liveshell.cli import main  # noqa: E402
from liveshell.daemon import JsonLineDaemon, LiveShellService  # noqa: E402
from liveshell.daemon import request_daemon_shutdown_marker  # noqa: E402
from liveshell.models import (  # noqa: E402
    COMMAND_QUEUED,
    COMMAND_RUNNING,
    ERROR_INVALID_PARAMS,
    ERROR_UNKNOWN_METHOD,
    PROTOCOL_VERSION,
    CommandEvent,
    CommandSpec,
    SessionSpec,
)
from liveshell.powershell import PowerShellResult  # noqa: E402
from liveshell.store import CURRENT_SCHEMA_VERSION, Store  # noqa: E402


def timeout_streaming_shell() -> tuple[str, str] | None:
    if Cmd.is_available():
        return "cmd", "echo liveshell-timeout-partial & ping -n 8 127.0.0.1 >NUL"
    if Bash.is_available():
        return "bash", "printf 'liveshell-timeout-partial\\n'; sleep 5"
    return None


def async_stderr_shell() -> tuple[type, str] | None:
    if AsyncCmd.is_available():
        return AsyncCmd, "echo liveshell-async-stdout & echo liveshell-async-stderr 1>&2"
    if AsyncBash.is_available():
        return (
            AsyncBash,
            "printf 'liveshell-async-stdout\\n'; printf 'liveshell-async-stderr\\n' >&2",
        )
    return None


class StoreProductionTests(unittest.TestCase):
    def test_schema_version_metadata_and_active_command_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store.from_state_dir(temp_dir)
            session = store.create_session(
                SessionSpec(kind="cmd", metadata={"owner": "owner-1"}),
                status="running",
            )
            running = store.create_command(
                CommandSpec(session_id=session.id, command="echo running"),
                status=COMMAND_RUNNING,
            )
            terminal = store.create_command(
                CommandSpec(session_id=session.id, command="echo done"),
                status="completed",
            )

            self.assertEqual(store.schema_version(), CURRENT_SCHEMA_VERSION)
            self.assertEqual(
                store.store_metadata()["schema_version"],
                str(CURRENT_SCHEMA_VERSION),
            )
            self.assertEqual(store.active_command_for_session(session.id).id, running.id)
            self.assertFalse(store.is_terminal_command(running.id))
            self.assertTrue(store.is_terminal_command(terminal.id))
            self.assertTrue(store.session_owned_by(session.id, "owner-1"))

    def test_batched_events_preserve_order_and_replay_from_arbitrary_seq(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store.from_state_dir(temp_dir)
            session = store.create_session(SessionSpec(kind="cmd"), status="running")
            command = store.create_command(
                CommandSpec(session_id=session.id, command="echo batched")
            )

            events = store.append_command_events(
                command.id,
                [
                    {"event_type": "stdout", "text": "a"},
                    {"event_type": "stdout", "text": "b"},
                    {"event_type": "stderr", "text": "c"},
                ],
            )
            replayed = store.list_command_events(command.id, since_seq=events[1].seq)

            self.assertEqual([event.seq for event in events], [1, 2, 3])
            self.assertEqual([event.text for event in replayed], ["c"])
            self.assertEqual(store.get_command(command.id).event_count, 3)


class ProtocolProductionTests(unittest.TestCase):
    def test_capability_discovery_includes_protocol_version_and_error_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            daemon = JsonLineDaemon(LiveShellService(Store.from_state_dir(temp_dir)))

            capabilities = daemon.handle_request(
                {"id": "req_caps", "method": "capability.discover", "params": {}}
            )
            unknown = daemon.handle_request(
                {"id": "req_unknown", "method": "missing.method", "params": {}}
            )
            invalid = daemon.handle_request(
                {"id": "req_invalid", "method": "session.create", "params": {}}
            )

            self.assertEqual(capabilities["result"]["protocol_version"], PROTOCOL_VERSION)
            self.assertEqual(unknown["error"]["type"], "ValueError")
            self.assertEqual(unknown["error"]["code"], ERROR_UNKNOWN_METHOD)
            self.assertEqual(invalid["error"]["code"], ERROR_INVALID_PARAMS)
            self.assertEqual(Store.from_state_dir(temp_dir).list_sessions(), [])

    def test_daemon_status_and_shutdown_protocol_methods(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            daemon = JsonLineDaemon(LiveShellService(Store.from_state_dir(temp_dir)))

            status = daemon.handle_request(
                {"id": "req_status", "method": "daemon.status", "params": {}}
            )
            shutdown = daemon.handle_request(
                {
                    "id": "req_shutdown",
                    "method": "daemon.shutdown",
                    "params": {"reason": "test"},
                }
            )

            self.assertTrue(status["ok"])
            self.assertEqual(status["result"]["protocol_version"], PROTOCOL_VERSION)
            self.assertTrue(shutdown["result"]["shutdown_requested"])

    def test_daemon_shutdown_closes_live_sessions(self) -> None:
        class FakeHostedSession:
            def __init__(self, *, cwd=None):
                self._closed = False

            def is_running(self):
                return not self._closed

            def run_result(self, command, *, check=True):
                return PowerShellResult(
                    command=command,
                    output=["ok"],
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
                store = Store.from_state_dir(temp_dir)
                daemon = JsonLineDaemon(LiveShellService(store))
                create = daemon.handle_request(
                    {
                        "id": "req_create",
                        "method": "session.create",
                        "params": {"kind": "powershell"},
                    }
                )
                session_id = create["result"]["id"]

                shutdown = daemon.handle_request(
                    {"id": "req_shutdown", "method": "daemon.shutdown", "params": {}}
                )

                self.assertTrue(shutdown["result"]["shutdown_requested"])
                self.assertEqual(store.get_session(session_id).status, "closed")
                self.assertEqual(
                    shutdown["result"]["closed_sessions"],
                    [{"session_id": session_id, "status": "closed"}],
                )
        finally:
            LiveShellService._session_type = staticmethod(original_session_type)

    def test_stdio_eof_closes_live_sessions(self) -> None:
        class FakeHostedSession:
            def __init__(self, *, cwd=None):
                self._closed = False

            def is_running(self):
                return not self._closed

            def close(self):
                self._closed = True

        original_session_type = LiveShellService._session_type
        LiveShellService._session_type = staticmethod(lambda kind: FakeHostedSession)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                store = Store.from_state_dir(temp_dir)
                service = LiveShellService(store)
                session = service.create_session("powershell")
                daemon = JsonLineDaemon(service)

                daemon.serve_stdio(
                    input_stream=io.StringIO(""),
                    output_stream=io.StringIO(),
                )

                self.assertEqual(store.get_session(session.id).status, "closed")
                self.assertTrue(service.shutdown_requested())
        finally:
            LiveShellService._session_type = staticmethod(original_session_type)

    def test_service_start_clears_stale_shutdown_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            request_daemon_shutdown_marker(temp_dir, reason="old marker")

            service = LiveShellService(Store.from_state_dir(temp_dir))

            self.assertFalse(service.shutdown_requested())


class DaemonProductionTests(unittest.TestCase):
    def test_same_session_commands_are_fifo_queued(self) -> None:
        class SlowHostedSession:
            first_started = threading.Event()
            release_first = threading.Event()

            def __init__(self, *, cwd=None):
                self._closed = False

            def is_running(self):
                return not self._closed

            def run_result(self, command, *, check=True):
                if command == "first":
                    type(self).first_started.set()
                    type(self).release_first.wait(timeout=5)
                return PowerShellResult(
                    command=command,
                    output=[command],
                    errors=[],
                    had_errors=False,
                    exit_code=0,
                )

            def close(self):
                self._closed = True

        original_session_type = LiveShellService._session_type
        LiveShellService._session_type = staticmethod(lambda kind: SlowHostedSession)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                service = LiveShellService(Store.from_state_dir(temp_dir))
                session = service.create_session("powershell")
                try:
                    first = service.start_command(session.id, "first", timeout_seconds=5)
                    self.assertTrue(SlowHostedSession.first_started.wait(timeout=2))
                    second = service.start_command(session.id, "second", timeout_seconds=5)

                    self.assertEqual(second.poll().status, COMMAND_QUEUED)
                    SlowHostedSession.release_first.set()

                    self.assertEqual(first.wait(poll_interval=0.01).stdout, "first")
                    self.assertEqual(second.wait(poll_interval=0.01).stdout, "second")
                finally:
                    latest = service.session_snapshot(session.id)
                    if latest.status == "running":
                        service.close_session(session.id)
        finally:
            LiveShellService._session_type = staticmethod(original_session_type)

    def test_large_output_is_chunked_without_changing_result_text(self) -> None:
        class LargeHostedSession:
            def __init__(self, *, cwd=None):
                self._closed = False

            def is_running(self):
                return not self._closed

            def run_result(self, command, *, check=True):
                return PowerShellResult(
                    command=command,
                    output=["x" * 35],
                    errors=[],
                    had_errors=False,
                    exit_code=0,
                )

            def close(self):
                self._closed = True

        original_session_type = LiveShellService._session_type
        LiveShellService._session_type = staticmethod(lambda kind: LargeHostedSession)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                service = LiveShellService(
                    Store.from_state_dir(temp_dir),
                    event_chunk_size=10,
                )
                session = service.create_session("powershell")
                try:
                    result = service.start_command(
                        session.id,
                        "large",
                        timeout_seconds=5,
                    ).wait(poll_interval=0.01)
                    stdout_events = [
                        event for event in result.events if event.event_type == "stdout"
                    ]

                    self.assertEqual(result.stdout, "x" * 35)
                    self.assertGreater(len(stdout_events), 1)
                    self.assertTrue(stdout_events[0].metadata["chunked"])
                finally:
                    service.close_session(session.id)
        finally:
            LiveShellService._session_type = staticmethod(original_session_type)

    def test_timeout_preserves_partial_output_in_result_and_tail(self) -> None:
        shell = timeout_streaming_shell()
        if shell is None:
            self.skipTest("No process-backed shell is available")
        kind, command = shell

        with tempfile.TemporaryDirectory() as temp_dir:
            service = LiveShellService(Store.from_state_dir(temp_dir))
            session = service.create_session(kind)
            result = service.start_command(
                session.id,
                command,
                timeout_seconds=0.5,
            ).wait(poll_interval=0.05)

            self.assertEqual(result.command.status, "timed_out")
            self.assertIn("liveshell-timeout-partial", result.stdout)
            self.assertIn("liveshell-timeout-partial", result.command.stdout_tail)


class ClientProductionTests(unittest.TestCase):
    def test_client_close_requests_daemon_shutdown_and_closes_sessions(self) -> None:
        if Cmd.is_available():
            kind = "cmd"
        elif Bash.is_available():
            kind = "bash"
        else:
            self.skipTest("No process-backed shell is available")

        with tempfile.TemporaryDirectory() as temp_dir:
            client = LiveShellClient.stdio(temp_dir)
            session = client.create_session(kind)

            client.close()

            snapshot = Store.from_state_dir(temp_dir).get_session(session.session_id)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.status, "closed")

    def test_client_reports_response_id_mismatch(self) -> None:
        client = LiveShellClient(
            io.StringIO(),
            io.StringIO('{"id":"other","ok":true,"result":null}\n'),
        )

        with self.assertRaises(LiveShellProtocolError):
            client.request("daemon.status", request_id="req_expected")

    def test_client_reports_malformed_json_response(self) -> None:
        client = LiveShellClient(io.StringIO(), io.StringIO("{not-json}\n"))

        with self.assertRaises(LiveShellProtocolError):
            client.request("daemon.status")

    def test_client_request_timeout_and_close_idempotency(self) -> None:
        class SlowOutput(io.StringIO):
            def readline(self, *args, **kwargs):
                time.sleep(0.2)
                return ""

        client = LiveShellClient(
            io.StringIO(),
            SlowOutput(),
            request_timeout_seconds=0.01,
        )

        with self.assertRaises(LiveShellProtocolError):
            client.request("daemon.status")
        client.close()
        client.close()


class CliProductionTests(unittest.TestCase):
    def test_daemon_status_shutdown_and_pretty_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_output = io.StringIO()
            with contextlib.redirect_stdout(status_output):
                status_code = main(
                    ["--json-pretty", "daemon", "status", "--state-dir", temp_dir]
                )

            shutdown_output = io.StringIO()
            with contextlib.redirect_stdout(shutdown_output):
                shutdown_code = main(
                    [
                        "daemon",
                        "shutdown",
                        "--state-dir",
                        temp_dir,
                        "--reason",
                        "test",
                    ]
                )

            status_payload = json.loads(status_output.getvalue())
            shutdown_payload = json.loads(shutdown_output.getvalue())

            self.assertEqual(status_code, 0)
            self.assertEqual(shutdown_code, 0)
            self.assertIn("\n  ", status_output.getvalue())
            self.assertTrue(status_payload["ok"])
            self.assertTrue(shutdown_payload["result"]["shutdown_requested"])


class PackagingProductionTests(unittest.TestCase):
    def test_base_package_imports_without_hard_pythonnet_dependency(self) -> None:
        if tomllib is None:
            self.skipTest("tomllib is only available on Python 3.11+ (or install tomli).")
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"].get("dependencies"), [])
        self.assertIn(
            "pythonnet>=3.0",
            pyproject["project"]["optional-dependencies"]["powershell"],
        )
        self.assertTrue(hasattr(liveshell, "PowerShell"))
        self.assertTrue(issubclass(CommandEvent, object))
        self.assertIn("py.typed", pyproject["tool"]["setuptools"]["package-data"]["liveshell"])
        self.assertTrue((ROOT / "LICENSE").is_file())


class AsyncProductionTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_client_close_can_skip_graceful_shutdown_request(self) -> None:
        class TrackingInput:
            def __init__(self):
                self.writes: list[str] = []
                self.closed = False

            def write(self, text):
                self.writes.append(text)
                return len(text)

            def flush(self):
                return None

            def close(self):
                self.closed = True

        class FakeProcess:
            def __init__(self):
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

            def terminate(self):
                self.returncode = 0

            def kill(self):
                self.returncode = 0

        input_stream = TrackingInput()
        client = LiveShellClient(
            input_stream,
            io.StringIO(""),
            process=FakeProcess(),
        )

        await client.close_async(graceful=False)

        self.assertEqual(input_stream.writes, [])
        self.assertTrue(input_stream.closed)

    async def test_async_process_session_separates_stderr(self) -> None:
        shell = async_stderr_shell()
        if shell is None:
            self.skipTest("No async process-backed shell is available")
        session_type, command = shell

        async with session_type() as session:
            result = await session.run(command)

        self.assertIn("liveshell-async-stdout", result.output)
        self.assertIn("liveshell-async-stderr", result.stderr)
        self.assertNotIn("liveshell-async-stderr", result.output)


if __name__ == "__main__":
    unittest.main()
