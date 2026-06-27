from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


SESSION_STARTING = "starting"
SESSION_RUNNING = "running"
SESSION_CLOSED = "closed"
SESSION_CRASHED = "crashed"

COMMAND_QUEUED = "queued"
COMMAND_STARTING = "starting"
COMMAND_RUNNING = "running"
COMMAND_COMPLETED = "completed"
COMMAND_FAILED = "failed"
COMMAND_TIMED_OUT = "timed_out"
COMMAND_CANCELED = "canceled"

TERMINAL_COMMAND_STATUSES = {
    COMMAND_COMPLETED,
    COMMAND_FAILED,
    COMMAND_TIMED_OUT,
    COMMAND_CANCELED,
}

EVENT_SESSION_STARTED = "session_started"
EVENT_SESSION_CLOSED = "session_closed"
EVENT_SESSION_CRASHED = "session_crashed"
EVENT_COMMAND_STARTED = "command_started"
EVENT_STDOUT = "stdout"
EVENT_STDERR = "stderr"
EVENT_HEARTBEAT = "heartbeat"
EVENT_COMMAND_COMPLETED = "command_completed"
EVENT_COMMAND_FAILED = "command_failed"
EVENT_COMMAND_TIMED_OUT = "command_timed_out"
EVENT_COMMAND_CANCELED = "command_canceled"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class JsonRecord:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Capability(JsonRecord):
    name: str
    available: bool
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionSpec(JsonRecord):
    kind: str
    cwd: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionSnapshot(JsonRecord):
    id: str
    kind: str
    status: str
    cwd: str | None
    pid: int | None
    started_at: str
    updated_at: str
    closed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandSpec(JsonRecord):
    session_id: str
    command: str
    cwd: str | None = None
    timeout_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandSnapshot(JsonRecord):
    id: str
    session_id: str
    command: str
    status: str
    cwd: str | None
    timeout_seconds: float | None
    exit_code: int | None
    started_at: str | None
    updated_at: str
    ended_at: str | None
    stdout_tail: str
    stderr_tail: str
    output_hash: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandEvent(JsonRecord):
    id: str
    command_id: str
    seq: int
    event_type: str
    text: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandResult(JsonRecord):
    command: CommandSnapshot
    events: list[CommandEvent]
    stdout: str
    stderr: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command.to_dict(),
            "events": [event.to_dict() for event in self.events],
            "stdout": self.stdout,
            "stderr": self.stderr,
        }
