from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from liveshell.handles import CommandHandle, SessionHandle  # noqa: E402
from liveshell.models import CommandSpec, SessionSpec  # noqa: E402
from liveshell.store import Store  # noqa: E402


class CommandHandleTests(unittest.TestCase):
    def test_events_for_unknown_command_raises_key_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            handle = CommandHandle("cmd_missing", Store.from_state_dir(temp_dir))

            with self.assertRaises(KeyError):
                handle.events()

    def test_sync_handle_returns_terminal_result_and_cancel_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store.from_state_dir(temp_dir)
            session = store.create_session(SessionSpec(kind="cmd"), status="closed")
            command = store.create_command(
                CommandSpec(session_id=session.id, command="echo hello"),
                status="running",
            )
            store.append_command_event(command.id, "stdout", "hello")
            store.update_command(command.id, status="completed", exit_code=0)

            handle = CommandHandle(command.id, store)

            self.assertEqual(handle.poll().status, "completed")
            self.assertEqual(handle.events()[0].text, "hello")
            self.assertEqual(handle.result().stdout, "hello")
            self.assertEqual(handle.wait(poll_interval=0).command.status, "completed")
            self.assertEqual(handle.cancel("already done").status, "completed")


class SessionHandleTests(unittest.TestCase):
    def test_session_handle_delegates_snapshot_close_and_start_command(self) -> None:
        class FakeService:
            def __init__(self):
                self.started = None

            def session_snapshot(self, session_id):
                return store.get_session(session_id)

            def close_session(self, session_id):
                return store.update_session(session_id, status="closed")

            def start_command(self, session_id, command, **kwargs):
                self.started = (session_id, command, kwargs)
                created = store.create_command(
                    CommandSpec(session_id=session_id, command=command),
                    status="completed",
                )
                return CommandHandle(created.id, store)

        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store.from_state_dir(temp_dir)
            session = store.create_session(SessionSpec(kind="cmd"), status="running")
            service = FakeService()
            handle = SessionHandle(session.id, service)

            command = handle.start_command("echo session")
            closed = handle.close()

            self.assertEqual(handle.snapshot().status, "closed")
            self.assertEqual(command.poll().status, "completed")
            self.assertEqual(closed.status, "closed")
            self.assertEqual(service.started[0], session.id)


class AsyncCommandHandleTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_methods_wrap_sync_handle_operations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store.from_state_dir(temp_dir)
            session = store.create_session(SessionSpec(kind="cmd"), status="closed")
            command = store.create_command(
                CommandSpec(session_id=session.id, command="echo async"),
                status="running",
            )
            store.append_command_event(command.id, "stdout", "async")
            store.update_command(command.id, status="completed", exit_code=0)

            handle = CommandHandle(command.id, store)

            self.assertEqual((await handle.poll_async()).status, "completed")
            self.assertEqual((await handle.events_async())[0].text, "async")
            self.assertEqual((await handle.result_async()).stdout, "async")
            self.assertEqual(
                (await handle.wait_async(poll_interval=0)).command.status,
                "completed",
            )
            self.assertEqual(
                (await handle.cancel_async("already done")).status,
                "completed",
            )


if __name__ == "__main__":
    unittest.main()
