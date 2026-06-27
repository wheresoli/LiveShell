from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from liveshell.models import CommandSpec, SessionSpec  # noqa: E402
from liveshell.store import BUSY_TIMEOUT_MS, TAIL_LIMIT, Store  # noqa: E402


class StoreTests(unittest.TestCase):
    def test_store_uses_wal_and_busy_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store.from_state_dir(temp_dir)

            with store._connect() as connection:
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

            self.assertEqual(journal_mode.lower(), "wal")
            self.assertEqual(busy_timeout, BUSY_TIMEOUT_MS)

    def test_create_list_update_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store.from_state_dir(temp_dir)

            session = store.create_session(
                SessionSpec(kind="cmd", cwd="C:\\tmp", metadata={"owner": "test"}),
                status="starting",
            )
            updated = store.update_session(session.id, status="running", pid=1234)
            sessions = store.list_sessions()

            self.assertEqual(updated.status, "running")
            self.assertEqual(updated.pid, 1234)
            self.assertEqual(updated.metadata["owner"], "test")
            self.assertEqual([item.id for item in sessions], [session.id])

    def test_command_events_are_ordered_and_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store.from_state_dir(temp_dir)
            session = store.create_session(SessionSpec(kind="cmd"), status="running")
            command = store.create_command(
                CommandSpec(session_id=session.id, command="echo hello")
            )

            first = store.append_command_event(command.id, "command_started")
            second = store.append_command_event(command.id, "stdout", "hello")
            third = store.append_command_event(command.id, "command_completed")
            replayed = store.list_command_events(command.id, since_seq=first.seq)

            self.assertEqual((first.seq, second.seq, third.seq), (1, 2, 3))
            self.assertEqual([event.seq for event in replayed], [2, 3])
            self.assertEqual(store.command_result(command.id).stdout, "hello")

    def test_command_tail_fields_are_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store.from_state_dir(temp_dir)
            session = store.create_session(SessionSpec(kind="cmd"), status="running")
            command = store.create_command(
                CommandSpec(session_id=session.id, command="echo long")
            )
            output = "x" * (TAIL_LIMIT + 10)

            updated = store.update_command(command.id, stdout_tail=output, stderr_tail=output)

            self.assertEqual(updated.stdout_tail, output[-TAIL_LIMIT:])
            self.assertEqual(updated.stderr_tail, output[-TAIL_LIMIT:])

    def test_session_and_command_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store.from_state_dir(temp_dir)
            running = store.create_session(SessionSpec(kind="cmd"), status="running")
            closed = store.create_session(SessionSpec(kind="cmd"), status="closed")
            running_command = store.create_command(
                CommandSpec(session_id=running.id, command="echo running"),
                status="running",
            )
            store.create_command(
                CommandSpec(session_id=closed.id, command="echo closed"),
                status="completed",
            )

            self.assertEqual(
                [session.id for session in store.list_sessions(status="running")],
                [running.id],
            )
            self.assertEqual(
                [command.id for command in store.list_commands(session_id=running.id)],
                [running_command.id],
            )
            self.assertEqual(
                [command.id for command in store.list_commands(status="running")],
                [running_command.id],
            )

    def test_missing_records_and_invalid_updates_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store.from_state_dir(temp_dir)

            self.assertIsNone(store.command_result("cmd_missing"))
            with self.assertRaises(KeyError):
                store.update_session("sess_missing", status="closed")
            with self.assertRaises(KeyError):
                store.update_command("cmd_missing", status="completed")
            with self.assertRaises(KeyError):
                store.append_command_event("cmd_missing", "stdout", "hello")

            session = store.create_session(SessionSpec(kind="cmd"), status="running")
            command = store.create_command(
                CommandSpec(session_id=session.id, command="echo hello")
            )

            with self.assertRaises(ValueError):
                store.update_session(session.id, unsupported=True)
            with self.assertRaises(ValueError):
                store.update_command(command.id, unsupported=True)

    def test_metadata_round_trips_on_sessions_commands_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store.from_state_dir(temp_dir)
            session = store.create_session(
                SessionSpec(kind="cmd", metadata={"owner": "models"}),
                metadata={"source": "test"},
            )
            command = store.create_command(
                CommandSpec(
                    session_id=session.id,
                    command="echo hello",
                    metadata={"request": "one"},
                ),
                metadata={"attempt": 1},
            )
            event = store.append_command_event(
                command.id,
                "stdout",
                "hello",
                metadata={"chunk": 1},
            )

            self.assertEqual(session.metadata, {"owner": "models", "source": "test"})
            self.assertEqual(command.metadata, {"request": "one", "attempt": 1})
            self.assertEqual(event.metadata, {"chunk": 1})
            self.assertEqual(
                store.list_command_events(command.id)[0].metadata,
                {"chunk": 1},
            )


if __name__ == "__main__":
    unittest.main()
