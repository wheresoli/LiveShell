from __future__ import annotations

from dataclasses import MISSING, asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Mapping, TypeVar, cast


PROTOCOL_VERSION = "1.0"

ERROR_INVALID_REQUEST = "invalid_request"
ERROR_INVALID_PARAMS = "invalid_params"
ERROR_NOT_FOUND = "not_found"
ERROR_CONFLICT = "conflict"
ERROR_UNKNOWN_METHOD = "unknown_method"
ERROR_INTERNAL = "internal_error"


SESSION_STARTING = "starting"
SESSION_RUNNING = "running"
SESSION_CLOSED = "closed"
SESSION_CRASHED = "crashed"
SESSION_STATUSES = {
    SESSION_STARTING,
    SESSION_RUNNING,
    SESSION_CLOSED,
    SESSION_CRASHED,
}

COMMAND_QUEUED = "queued"
COMMAND_STARTING = "starting"
COMMAND_RUNNING = "running"
COMMAND_COMPLETED = "completed"
COMMAND_FAILED = "failed"
COMMAND_TIMED_OUT = "timed_out"
COMMAND_CANCELED = "canceled"
COMMAND_STATUSES = {
    COMMAND_QUEUED,
    COMMAND_STARTING,
    COMMAND_RUNNING,
    COMMAND_COMPLETED,
    COMMAND_FAILED,
    COMMAND_TIMED_OUT,
    COMMAND_CANCELED,
}

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
EVENT_TYPES = {
    EVENT_SESSION_STARTED,
    EVENT_SESSION_CLOSED,
    EVENT_SESSION_CRASHED,
    EVENT_COMMAND_STARTED,
    EVENT_STDOUT,
    EVENT_STDERR,
    EVENT_HEARTBEAT,
    EVENT_COMMAND_COMPLETED,
    EVENT_COMMAND_FAILED,
    EVENT_COMMAND_TIMED_OUT,
    EVENT_COMMAND_CANCELED,
}

SESSION_KINDS = {"cmd", "bash", "powershell"}


TJsonRecord = TypeVar("TJsonRecord", bound="JsonRecord")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class JsonRecord:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls: type[TJsonRecord], data: Mapping[str, Any]) -> TJsonRecord:
        """Build a dataclass record from a dict while ignoring newer unknown fields."""
        if not isinstance(data, Mapping):
            raise TypeError(f"{cls.__name__}.from_dict requires a mapping.")

        values: dict[str, Any] = {}
        for item in fields(cls):
            if item.name in data:
                values[item.name] = data[item.name]
            elif item.default is not MISSING or item.default_factory is not MISSING:
                continue
            else:
                raise KeyError(f"Missing required field for {cls.__name__}: {item.name}")
        return cast(TJsonRecord, cls(**values))


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
    command_count: int = 0
    active_command_count: int = 0


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
    event_count: int = 0
    stdout_event_count: int = 0
    stderr_event_count: int = 0


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

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CommandResult:
        if not isinstance(data, Mapping):
            raise TypeError("CommandResult.from_dict requires a mapping.")
        return cls(
            command=CommandSnapshot.from_dict(data["command"]),
            events=[CommandEvent.from_dict(item) for item in data.get("events", [])],
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
        )


def record_to_dict(record: JsonRecord) -> dict[str, Any]:
    return record.to_dict()


def record_from_dict(record_type: type[TJsonRecord], data: Mapping[str, Any]) -> TJsonRecord:
    return record_type.from_dict(data)
