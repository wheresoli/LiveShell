from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import (
    COMMAND_QUEUED,
    COMMAND_RUNNING,
    COMMAND_STARTING,
    COMMAND_STATUSES,
    EVENT_TYPES,
    SESSION_KINDS,
    SESSION_RUNNING,
    SESSION_STATUSES,
    TERMINAL_COMMAND_STATUSES,
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
CURRENT_SCHEMA_VERSION = 1
ACTIVE_COMMAND_STATUSES = (COMMAND_QUEUED, COMMAND_STARTING, COMMAND_RUNNING)


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
        self.state_dir = self.db_path.parent
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
            current_version = self._schema_version(connection)
            if current_version > CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"LiveShell store schema {current_version} is newer than "
                    f"this package supports ({CURRENT_SCHEMA_VERSION})."
                )
            if current_version < CURRENT_SCHEMA_VERSION:
                self._migrate(connection, current_version, CURRENT_SCHEMA_VERSION)

    def schema_version(self) -> int:
        with self._connect() as connection:
            return self._schema_version(connection)

    def store_metadata(self) -> dict[str, str]:
        with self._connect() as connection:
            rows = connection.execute("SELECT key, value FROM store_metadata").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def _schema_version(self, connection: sqlite3.Connection) -> int:
        row = connection.execute("PRAGMA user_version").fetchone()
        version = int(row[0]) if row else 0
        if version:
            return version
        metadata_exists = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'store_metadata'
            """
        ).fetchone()
        if metadata_exists is None:
            return 0
        row = connection.execute(
            "SELECT value FROM store_metadata WHERE key = 'schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def _migrate(
        self,
        connection: sqlite3.Connection,
        from_version: int,
        to_version: int,
    ) -> None:
        version = from_version
        while version < to_version:
            if version == 0:
                self._create_schema_v1(connection)
                version = 1
                self._set_schema_version(connection, version)
                continue
            raise RuntimeError(f"No migration available from schema version {version}.")

    def _set_schema_version(self, connection: sqlite3.Connection, version: int) -> None:
        now = utc_now()
        connection.execute(
            """
            INSERT INTO store_metadata (key, value, updated_at)
            VALUES ('schema_version', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (str(version), now),
        )
        connection.execute(f"PRAGMA user_version = {version}")

    def _create_schema_v1(self, connection: sqlite3.Connection) -> None:
        session_statuses = ", ".join(repr(status) for status in sorted(SESSION_STATUSES))
        session_kinds = ", ".join(repr(kind) for kind in sorted(SESSION_KINDS))
        command_statuses = ", ".join(repr(status) for status in sorted(COMMAND_STATUSES))
        event_types = ", ".join(repr(event_type) for event_type in sorted(EVENT_TYPES))
        connection.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS store_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ({session_kinds})),
                status TEXT NOT NULL CHECK(status IN ({session_statuses})),
                cwd TEXT,
                pid INTEGER CHECK(pid IS NULL OR pid > 0),
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{{}}',
                CHECK(closed_at IS NULL OR status IN ('closed', 'crashed'))
            );

            CREATE TABLE IF NOT EXISTS command (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                command TEXT NOT NULL CHECK(length(command) > 0),
                status TEXT NOT NULL CHECK(status IN ({command_statuses})),
                cwd TEXT,
                timeout_seconds REAL CHECK(timeout_seconds IS NULL OR timeout_seconds > 0),
                exit_code INTEGER,
                started_at TEXT,
                updated_at TEXT NOT NULL,
                ended_at TEXT,
                stdout_tail TEXT NOT NULL DEFAULT '',
                stderr_tail TEXT NOT NULL DEFAULT '',
                output_hash TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{{}}',
                FOREIGN KEY(session_id) REFERENCES session(id),
                CHECK(ended_at IS NULL OR status IN ('completed', 'failed', 'timed_out', 'canceled'))
            );

            CREATE TABLE IF NOT EXISTS command_event (
                id TEXT PRIMARY KEY,
                command_id TEXT NOT NULL,
                seq INTEGER NOT NULL CHECK(seq > 0),
                event_type TEXT NOT NULL CHECK(event_type IN ({event_types})),
                text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{{}}',
                UNIQUE(command_id, seq),
                FOREIGN KEY(command_id) REFERENCES command(id)
            );

            CREATE INDEX IF NOT EXISTS idx_session_status
                ON session(status);
            CREATE INDEX IF NOT EXISTS idx_command_session_status
                ON command(session_id, status);
            CREATE INDEX IF NOT EXISTS idx_command_status_updated
                ON command(status, updated_at);
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
                self._session_select_sql() + " WHERE session.id = ?",
                (session_id,),
            ).fetchone()
        return self._session_from_row(row) if row else None

    def list_sessions(self, *, status: str | None = None) -> list[SessionSnapshot]:
        query = self._session_select_sql()
        values: tuple[Any, ...] = ()
        if status is not None:
            query += " WHERE session.status = ?"
            values = (status,)
        query += " ORDER BY session.started_at, session.id"
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
                self._command_select_sql() + " WHERE command.id = ?",
                (command_id,),
            ).fetchone()
        return self._command_from_row(row) if row else None

    def list_commands(
        self,
        *,
        session_id: str | None = None,
        status: str | None = None,
    ) -> list[CommandSnapshot]:
        query = self._command_select_sql()
        conditions = []
        values: list[Any] = []
        if session_id is not None:
            conditions.append("command.session_id = ?")
            values.append(session_id)
        if status is not None:
            conditions.append("command.status = ?")
            values.append(status)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY command.updated_at, command.id"
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
        return self.append_command_events(
            command_id,
            [
                {
                    "event_type": event_type,
                    "text": text,
                    "metadata": metadata,
                    "created_at": created_at,
                }
            ],
        )[0]

    def append_command_events(
        self,
        command_id: str,
        events: list[dict[str, Any]],
    ) -> list[CommandEvent]:
        if not events:
            return []
        now = utc_now()
        prepared = [
            {
                "id": _new_id("evt"),
                "event_type": str(event["event_type"]),
                "text": str(event.get("text") or ""),
                "metadata": event.get("metadata") if event.get("metadata") is not None else {},
                "created_at": event.get("created_at") or now,
            }
            for event in events
        ]
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
            next_seq = int(row["next_seq"])
            rows = []
            for offset, event in enumerate(prepared):
                rows.append(
                    (
                        event["id"],
                        command_id,
                        next_seq + offset,
                        event["event_type"],
                        event["text"],
                        event["created_at"],
                        _json_dump(event["metadata"]),
                    )
                )
            connection.executemany(
                """
                INSERT INTO command_event (
                    id, command_id, seq, event_type, text, created_at,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return [
            CommandEvent(
                id=row[0],
                command_id=command_id,
                seq=int(row[2]),
                event_type=str(row[3]),
                text=str(row[4]),
                created_at=str(row[5]),
                metadata=dict(prepared[index]["metadata"]),
            )
            for index, row in enumerate(rows)
        ]

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

    def is_terminal_command(self, command_id: str) -> bool:
        command = self.get_command(command_id)
        return command is not None and command.status in TERMINAL_COMMAND_STATUSES

    def active_command_for_session(self, session_id: str) -> CommandSnapshot | None:
        commands = self.list_active_commands(session_id=session_id)
        return commands[0] if commands else None

    def list_active_commands(
        self,
        *,
        session_id: str | None = None,
    ) -> list[CommandSnapshot]:
        query = self._command_select_sql()
        placeholders = ", ".join("?" for _ in ACTIVE_COMMAND_STATUSES)
        values: list[Any] = list(ACTIVE_COMMAND_STATUSES)
        conditions = [f"command.status IN ({placeholders})"]
        if session_id is not None:
            conditions.append("command.session_id = ?")
            values.append(session_id)
        query += " WHERE " + " AND ".join(conditions)
        query += """
            ORDER BY
                CASE command.status
                    WHEN 'running' THEN 0
                    WHEN 'starting' THEN 1
                    ELSE 2
                END,
                command.started_at,
                command.updated_at,
                command.id
        """
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [self._command_from_row(row) for row in rows]

    def session_owned_by(self, session_id: str, owner: str) -> bool:
        session = self.get_session(session_id)
        if session is None:
            return False
        return owner in {
            session.metadata.get("owner"),
            session.metadata.get("daemon_id"),
        }

    def finish_command(
        self,
        command_id: str,
        *,
        status: str,
        event_type: str,
        event_text: str = "",
        exit_code: int | None = None,
        stdout_tail: str = "",
        stderr_tail: str = "",
        output_hash: str | None = None,
        metadata: dict[str, Any] | None = None,
        ended_at: str | None = None,
    ) -> CommandSnapshot:
        ended_at = ended_at or utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM command WHERE id = ?",
                (command_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown command: {command_id}")
            if row["status"] in TERMINAL_COMMAND_STATUSES:
                existing = connection.execute(
                    self._command_select_sql() + " WHERE command.id = ?",
                    (command_id,),
                ).fetchone()
                if existing is None:
                    raise KeyError(f"Unknown command: {command_id}")
                return self._command_from_row(existing)

            seq_row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM command_event WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO command_event (
                    id, command_id, seq, event_type, text, created_at,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_id("evt"),
                    command_id,
                    int(seq_row["next_seq"]),
                    event_type,
                    event_text,
                    ended_at,
                    _json_dump(None),
                ),
            )
            cursor = connection.execute(
                """
                UPDATE command
                SET status = ?,
                    exit_code = ?,
                    ended_at = ?,
                    stdout_tail = ?,
                    stderr_tail = ?,
                    output_hash = ?,
                    metadata_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    exit_code,
                    ended_at,
                    stdout_tail[-TAIL_LIMIT:],
                    stderr_tail[-TAIL_LIMIT:],
                    output_hash,
                    _json_dump(metadata),
                    ended_at,
                    command_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown command: {command_id}")

        snapshot = self.get_command(command_id)
        if snapshot is None:
            raise KeyError(f"Unknown command: {command_id}")
        return snapshot

    @staticmethod
    def _session_select_sql() -> str:
        active_statuses = ", ".join(repr(status) for status in ACTIVE_COMMAND_STATUSES)
        return f"""
            SELECT
                session.*,
                (
                    SELECT COUNT(*)
                    FROM command
                    WHERE command.session_id = session.id
                ) AS command_count,
                (
                    SELECT COUNT(*)
                    FROM command
                    WHERE command.session_id = session.id
                      AND command.status IN ({active_statuses})
                ) AS active_command_count
            FROM session
        """

    @staticmethod
    def _command_select_sql() -> str:
        return """
            SELECT
                command.*,
                (
                    SELECT COUNT(*)
                    FROM command_event
                    WHERE command_event.command_id = command.id
                ) AS event_count,
                (
                    SELECT COUNT(*)
                    FROM command_event
                    WHERE command_event.command_id = command.id
                      AND command_event.event_type = 'stdout'
                ) AS stdout_event_count,
                (
                    SELECT COUNT(*)
                    FROM command_event
                    WHERE command_event.command_id = command.id
                      AND command_event.event_type = 'stderr'
                ) AS stderr_event_count
            FROM command
        """

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
            command_count=int(row["command_count"]) if "command_count" in row.keys() else 0,
            active_command_count=(
                int(row["active_command_count"]) if "active_command_count" in row.keys() else 0
            ),
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
            event_count=int(row["event_count"]) if "event_count" in row.keys() else 0,
            stdout_event_count=(
                int(row["stdout_event_count"]) if "stdout_event_count" in row.keys() else 0
            ),
            stderr_event_count=(
                int(row["stderr_event_count"]) if "stderr_event_count" in row.keys() else 0
            ),
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
