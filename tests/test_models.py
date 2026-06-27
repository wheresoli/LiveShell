from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from liveshell.models import (  # noqa: E402
    Capability,
    CommandEvent,
    CommandResult,
    CommandSnapshot,
    SessionSnapshot,
)


class ModelSerializationTests(unittest.TestCase):
    def test_snapshots_events_results_are_json_serializable(self) -> None:
        session = SessionSnapshot(
            id="sess_1",
            kind="cmd",
            status="running",
            cwd="C:\\tmp",
            pid=123,
            started_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:01Z",
            closed_at=None,
            metadata={"owner": "test"},
        )
        command = CommandSnapshot(
            id="cmd_1",
            session_id=session.id,
            command="echo hello",
            status="completed",
            cwd=None,
            timeout_seconds=5.0,
            exit_code=0,
            started_at="2026-01-01T00:00:02Z",
            updated_at="2026-01-01T00:00:03Z",
            ended_at="2026-01-01T00:00:03Z",
            stdout_tail="hello",
            stderr_tail="",
            output_hash="abc",
            metadata={"attempt": 1},
        )
        event = CommandEvent(
            id="evt_1",
            command_id=command.id,
            seq=1,
            event_type="stdout",
            text="hello",
            created_at="2026-01-01T00:00:03Z",
            metadata={"chunk": 1},
        )
        capability = Capability(
            name="command.poll",
            available=True,
            details={"scope": "local"},
        )
        result = CommandResult(
            command=command,
            events=[event],
            stdout="hello",
            stderr="",
        )

        encoded = json.dumps(
            {
                "capability": capability.to_dict(),
                "session": session.to_dict(),
                "command": command.to_dict(),
                "event": event.to_dict(),
                "result": result.to_dict(),
            }
        )

        self.assertIn('"command.poll"', encoded)
        self.assertEqual(result.to_dict()["events"][0]["seq"], 1)


if __name__ == "__main__":
    unittest.main()
