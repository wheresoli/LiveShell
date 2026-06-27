from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import (
    COMMAND_QUEUED,
    CommandEvent,
    CommandResult,
    CommandSnapshot,
    CommandSpec,
    SessionSnapshot,
    SessionSpec,
    utc_now,
)


DEFAULT_DB_NAME = "liveshell.sqlite3"
TAIL_LIMIT = 8192
BUSY_TIMEOUT_MS = 5000


def state_db_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / DEFAULT_DB_NAME


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json_dump(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def _json_load(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else {}


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @classmethod
    def from_state_dir(cls, state_dir: str | Path) -> Store:
        return cls(state_db_path(state_dir))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS session (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cwd TEXT,
                    pid INTEGER,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT,
                    metadata_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS command (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cwd TEXT,
                    timeout_seconds REAL,
                    exit_code INTEGER,
                    started_at TEXT,
                    updated_at TEXT NOT NULL,
                    ended_at TEXT,
                    stdout_tail TEXT NOT NULL,
                    stderr_tail TEXT NOT NULL,
                    output_hash TEXT,
                    metadata_json TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES session(id)
                );

                CREATE TABLE IF NOT EXISTS command_event (
                    id TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    UNIQUE(command_id, seq),
                    FOREIGN KEY(command_id) REFERENCES command(id)
                );

                CREATE INDEX IF NOT EXISTS idx_session_status
                    ON session(status);
                CREATE INDEX IF NOT EXISTS idx_command_session_status
                    ON command(session_id, status);
                CREATE INDEX IF NOT EXISTS idx_command_event_command_seq
                    ON command_event(command_id, seq);
                """
            )

    def create_session(
        self,
        spec: SessionSpec,
        *,
        session_id: str | None = None,
        status: str = "starting",
        pid: int | None = None,
        started_at: str | None = None,
        closed_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionSnapshot:
        now = utc_now()
        session_id = session_id or _new_id("sess")
        merged_metadata = dict(spec.metadata)
        if metadata:
            merged_metadata.update(metadata)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO session (
                    id, kind, status, cwd, pid, started_at, updated_at,
                    closed_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    spec.kind,
                    status,
                    spec.cwd,
                    pid,
                    started_at or now,
                    now,
                    closed_at,
                    _json_dump(merged_metadata),
                ),
            )
        snapshot = self.get_session(session_id)
        if snapshot is None:
            raise RuntimeError(f"Failed to create session: {session_id}")
        return snapshot

    def update_session(self, session_id: str, **updates: Any) -> SessionSnapshot:
        columns = {
            "kind": "kind",
            "status": "status",
            "cwd": "cwd",
            "pid": "pid",
            "started_at": "started_at",
            "closed_at": "closed_at",
            "metadata": "metadata_json",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in updates.items():
            if key not in columns:
                raise ValueError(f"Unsupported session update field: {key}")
            column = columns[key]
            assignments.append(f"{column} = ?")
            values.append(_json_dump(value) if key == "metadata" else value)
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(session_id)

        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE session SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown session: {session_id}")

        snapshot = self.get_session(session_id)
        if snapshot is None:
            raise KeyError(f"Unknown session: {session_id}")
        return snapshot

    def get_session(self, session_id: str) -> SessionSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM session WHERE id = ?",
                (session_id,),
            ).fetchone()
        return self._session_from_row(row) if row else None

    def list_sessions(self, *, status: str | None = None) -> list[SessionSnapshot]:
        query = "SELECT * FROM session"
        values: tuple[Any, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            values = (status,)
        query += " ORDER BY started_at, id"
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [self._session_from_row(row) for row in rows]

    def create_command(
        self,
        spec: CommandSpec,
        *,
        command_id: str | None = None,
        status: str = COMMAND_QUEUED,
        started_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CommandSnapshot:
        now = utc_now()
        command_id = command_id or _new_id("cmd")
        merged_metadata = dict(spec.metadata)
        if metadata:
            merged_metadata.update(metadata)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO command (
                    id, session_id, command, status, cwd, timeout_seconds,
                    exit_code, started_at, updated_at, ended_at, stdout_tail,
                    stderr_tail, output_hash, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, '', '', NULL, ?)
                """,
                (
                    command_id,
                    spec.session_id,
                    spec.command,
                    status,
                    spec.cwd,
                    spec.timeout_seconds,
                    started_at,
                    now,
                    _json_dump(merged_metadata),
                ),
            )
        snapshot = self.get_command(command_id)
        if snapshot is None:
            raise RuntimeError(f"Failed to create command: {command_id}")
        return snapshot

    def update_command(self, command_id: str, **updates: Any) -> CommandSnapshot:
        columns = {
            "session_id": "session_id",
            "command": "command",
            "status": "status",
            "cwd": "cwd",
            "timeout_seconds": "timeout_seconds",
            "exit_code": "exit_code",
            "started_at": "started_at",
            "ended_at": "ended_at",
            "stdout_tail": "stdout_tail",
            "stderr_tail": "stderr_tail",
            "output_hash": "output_hash",
            "metadata": "metadata_json",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in updates.items():
            if key not in columns:
                raise ValueError(f"Unsupported command update field: {key}")
            column = columns[key]
            assignments.append(f"{column} = ?")
            if key == "metadata":
                values.append(_json_dump(value))
            elif key in {"stdout_tail", "stderr_tail"} and isinstance(value, str):
                values.append(value[-TAIL_LIMIT:])
            else:
                values.append(value)
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(command_id)

        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE command SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown command: {command_id}")

        snapshot = self.get_command(command_id)
        if snapshot is None:
            raise KeyError(f"Unknown command: {command_id}")
        return snapshot

    def get_command(self, command_id: str) -> CommandSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM command WHERE id = ?",
                (command_id,),
            ).fetchone()
        return self._command_from_row(row) if row else None

    def list_commands(
        self,
        *,
        session_id: str | None = None,
        status: str | None = None,
    ) -> list[CommandSnapshot]:
        query = "SELECT * FROM command"
        conditions = []
        values: list[Any] = []
        if session_id is not None:
            conditions.append("session_id = ?")
            values.append(session_id)
        if status is not None:
            conditions.append("status = ?")
            values.append(status)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY updated_at, id"
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [self._command_from_row(row) for row in rows]

    def append_command_event(
        self,
        command_id: str,
        event_type: str,
        text: str = "",
        *,
        metadata: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> CommandEvent:
        event_id = _new_id("evt")
        created_at = created_at or utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            command = connection.execute(
                "SELECT id FROM command WHERE id = ?",
                (command_id,),
            ).fetchone()
            if command is None:
                raise KeyError(f"Unknown command: {command_id}")
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM command_event WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            seq = int(row["next_seq"])
            connection.execute(
                """
                INSERT INTO command_event (
                    id, command_id, seq, event_type, text, created_at,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    command_id,
                    seq,
                    event_type,
                    text,
                    created_at,
                    _json_dump(metadata),
                ),
            )
        return CommandEvent(
            id=event_id,
            command_id=command_id,
            seq=seq,
            event_type=event_type,
            text=text,
            created_at=created_at,
            metadata=metadata or {},
        )

    def list_command_events(
        self,
        command_id: str,
        *,
        since_seq: int = 0,
    ) -> list[CommandEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM command_event
                WHERE command_id = ? AND seq > ?
                ORDER BY seq
                """,
                (command_id, since_seq),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def command_result(self, command_id: str) -> CommandResult | None:
        command = self.get_command(command_id)
        if command is None:
            return None
        events = self.list_command_events(command_id)
        stdout = "".join(event.text for event in events if event.event_type == "stdout")
        stderr = "".join(event.text for event in events if event.event_type == "stderr")
        return CommandResult(
            command=command,
            events=events,
            stdout=stdout,
            stderr=stderr,
        )

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> SessionSnapshot:
        return SessionSnapshot(
            id=row["id"],
            kind=row["kind"],
            status=row["status"],
            cwd=row["cwd"],
            pid=row["pid"],
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            closed_at=row["closed_at"],
            metadata=_json_load(row["metadata_json"]),
        )

    @staticmethod
    def _command_from_row(row: sqlite3.Row) -> CommandSnapshot:
        return CommandSnapshot(
            id=row["id"],
            session_id=row["session_id"],
            command=row["command"],
            status=row["status"],
            cwd=row["cwd"],
            timeout_seconds=row["timeout_seconds"],
            exit_code=row["exit_code"],
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            ended_at=row["ended_at"],
            stdout_tail=row["stdout_tail"],
            stderr_tail=row["stderr_tail"],
            output_hash=row["output_hash"],
            metadata=_json_load(row["metadata_json"]),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> CommandEvent:
        return CommandEvent(
            id=row["id"],
            command_id=row["command_id"],
            seq=row["seq"],
            event_type=row["event_type"],
            text=row["text"],
            created_at=row["created_at"],
            metadata=_json_load(row["metadata_json"]),
        )


LocalStore = Store
