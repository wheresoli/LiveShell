from __future__ import annotations

from collections import deque
import hashlib
import ipaddress
import json
from numbers import Real
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, TextIO

from .bash import Bash
from .capabilities import discover_capabilities
from .cmd import Cmd
from .handles import CommandHandle
from .models import (
    COMMAND_CANCELED,
    COMMAND_COMPLETED,
    COMMAND_FAILED,
    COMMAND_QUEUED,
    COMMAND_RUNNING,
    COMMAND_STARTING,
    COMMAND_TIMED_OUT,
    EVENT_COMMAND_CANCELED,
    EVENT_COMMAND_COMPLETED,
    EVENT_COMMAND_FAILED,
    EVENT_COMMAND_STARTED,
    EVENT_COMMAND_TIMED_OUT,
    EVENT_STDERR,
    EVENT_STDOUT,
    ERROR_CONFLICT,
    ERROR_INTERNAL,
    ERROR_INVALID_PARAMS,
    ERROR_INVALID_REQUEST,
    ERROR_NOT_FOUND,
    ERROR_UNKNOWN_METHOD,
    PROTOCOL_VERSION,
    SESSION_KINDS,
    SESSION_CLOSED,
    SESSION_CRASHED,
    SESSION_RUNNING,
    TERMINAL_COMMAND_STATUSES,
    CommandResult,
    CommandSnapshot,
    CommandSpec,
    SessionSnapshot,
    SessionSpec,
    utc_now,
)
from .powershell import PowerShell, PowerShellResult
from .process import ProcessSession, ProcessResult as ShellProcessResult
from .store import Store


EXEC_THREAD_JOIN_TIMEOUT_SECONDS = 0.5
WORKER_JOIN_TIMEOUT_SECONDS = 1.0
PROCESS_TERMINATE_TIMEOUT_SECONDS = 0.5
DEFAULT_EVENT_CHUNK_SIZE = 65536
DAEMON_STATE_FILE = "daemon.json"
DAEMON_LOCK_FILE = "daemon.lock"
DAEMON_SHUTDOWN_FILE = "daemon.shutdown.json"


class UnknownMethodError(ValueError):
    pass


def protocol_error_code(exc: BaseException) -> str:
    if isinstance(exc, UnknownMethodError):
        return ERROR_UNKNOWN_METHOD
    if isinstance(exc, KeyError):
        return ERROR_NOT_FOUND
    if isinstance(exc, (json.JSONDecodeError, TypeError)):
        return ERROR_INVALID_REQUEST
    if isinstance(exc, ValueError):
        return ERROR_INVALID_PARAMS
    if isinstance(exc, RuntimeError):
        return ERROR_CONFLICT
    return ERROR_INTERNAL


def protocol_error_payload(exc: BaseException) -> dict[str, str]:
    error_type = "ValueError" if isinstance(exc, UnknownMethodError) else exc.__class__.__name__
    return {
        "type": error_type,
        "code": protocol_error_code(exc),
        "message": str(exc),
    }


class LiveShellService:
    def __init__(
        self,
        store: Store | str | Path,
        *,
        recover: bool = True,
        event_chunk_size: int | None = DEFAULT_EVENT_CHUNK_SIZE,
        transport: str = "in_process",
    ):
        self.store = store if isinstance(store, Store) else Store(store)
        self._sessions: dict[str, Any] = {}
        self._command_cancels: dict[str, threading.Event] = {}
        self._command_threads: dict[str, threading.Thread] = {}
        self._session_queues: dict[str, deque[str]] = {}
        self._session_queue_threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        self.daemon_id = f"daemon_{os.getpid()}_{int(time.time() * 1000)}"
        self.started_at = utc_now()
        self.transport = transport
        self.event_chunk_size = (
            None
            if event_chunk_size is None
            else max(1, int(event_chunk_size))
        )
        self._shutdown_requested = threading.Event()
        self._socket_host: str | None = None
        self._socket_port: int | None = None
        self._clear_shutdown_marker()
        self._write_daemon_state()
        if recover:
            self.recover_orphaned_records()

    def set_socket_address(self, host: str, port: int) -> None:
        """Record the loopback address a background (socket-transport) daemon is
        listening on so cross-process clients can reconnect via the state dir."""
        self._socket_host = str(host)
        self._socket_port = int(port)
        self._write_daemon_state()

    def daemon_status(self) -> dict[str, Any]:
        with self._lock:
            live_session_ids = sorted(self._sessions)
            queued_by_session = {
                session_id: list(queue)
                for session_id, queue in self._session_queues.items()
                if queue
            }
        sessions = self.store.list_sessions()
        commands = self.store.list_commands()
        active_commands = self.store.list_active_commands()
        return {
            "daemon_id": self.daemon_id,
            "pid": os.getpid(),
            "protocol_version": PROTOCOL_VERSION,
            "transport": self.transport,
            "state_dir": str(self.store.state_dir),
            "started_at": self.started_at,
            "shutdown_requested": self._shutdown_requested.is_set(),
            "schema_version": self.store.schema_version(),
            "session_count": len(sessions),
            "live_session_ids": live_session_ids,
            "active_command_ids": [command.id for command in active_commands],
            "queued_by_session": queued_by_session,
            "command_count": len(commands),
            "socket_host": self._socket_host,
            "socket_port": self._socket_port,
        }

    def request_shutdown(self, *, reason: str | None = None) -> dict[str, Any]:
        self._shutdown_requested.set()
        payload = {
            "daemon_id": self.daemon_id,
            "pid": os.getpid(),
            "requested_at": utc_now(),
            "reason": reason,
            "transport": self.transport,
        }
        self._write_json_file(DAEMON_SHUTDOWN_FILE, payload)
        payload["closed_sessions"] = self._close_live_sessions_for_shutdown()
        self._write_daemon_state()
        return {"shutdown_requested": True, **payload}

    def _close_live_sessions_for_shutdown(self) -> list[dict[str, Any]]:
        with self._lock:
            session_ids = list(self._sessions)
        closed: list[dict[str, Any]] = []
        for session_id in session_ids:
            try:
                snapshot = self.close_session(session_id)
                closed.append({"session_id": session_id, "status": snapshot.status})
            except Exception as exc:
                closed.append(
                    {
                        "session_id": session_id,
                        "error": {
                            "type": exc.__class__.__name__,
                            "message": str(exc),
                        },
                    }
                )
        return closed

    def shutdown_requested(self) -> bool:
        return self._shutdown_requested.is_set() or self._shutdown_file_exists()

    def _write_daemon_state(self) -> None:
        payload = {
            "daemon_id": self.daemon_id,
            "pid": os.getpid(),
            "protocol_version": PROTOCOL_VERSION,
            "transport": self.transport,
            "state_dir": str(self.store.state_dir),
            "started_at": self.started_at,
            "updated_at": utc_now(),
            "shutdown_requested": self._shutdown_requested.is_set(),
            "socket_host": self._socket_host,
            "socket_port": self._socket_port,
        }
        self._write_json_file(DAEMON_STATE_FILE, payload)
        self._write_json_file(DAEMON_LOCK_FILE, payload)

    def _write_json_file(self, filename: str, payload: dict[str, Any]) -> None:
        path = self.store.state_dir / filename
        try:
            path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        except OSError:
            pass

    def _shutdown_file_exists(self) -> bool:
        return (self.store.state_dir / DAEMON_SHUTDOWN_FILE).exists()

    def _clear_shutdown_marker(self) -> None:
        try:
            (self.store.state_dir / DAEMON_SHUTDOWN_FILE).unlink(missing_ok=True)
        except OSError:
            pass

    def recover_orphaned_records(self) -> None:
        now = utc_now()
        for session in self.store.list_sessions():
            if session.status in {"starting", SESSION_RUNNING}:
                metadata = dict(session.metadata)
                metadata["recovered_without_process_handle"] = True
                metadata["recovery_reason"] = "daemon_start_without_live_session"
                self.store.update_session(
                    session.id,
                    status=SESSION_CRASHED,
                    closed_at=now,
                    metadata=metadata,
                )

        for command in self.store.list_commands():
            if command.status in {"queued", COMMAND_STARTING, COMMAND_RUNNING}:
                metadata = dict(command.metadata)
                metadata["recovered_without_worker"] = True
                metadata["recovery_reason"] = "daemon_start_without_live_command"
                self.store.finish_command(
                    command.id,
                    status=COMMAND_FAILED,
                    event_type=EVENT_COMMAND_FAILED,
                    event_text="Command was running when the daemon restarted.",
                    ended_at=now,
                    output_hash=hashlib.sha256(b"").hexdigest(),
                    metadata=metadata,
                )

    def discover_capabilities(self) -> list[dict[str, Any]]:
        return [capability.to_dict() for capability in discover_capabilities()]

    def create_session(
        self,
        kind: str,
        *,
        cwd: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionSnapshot:
        if not isinstance(kind, str) or not kind:
            raise ValueError("kind must be a non-empty string.")
        kind = kind.lower()
        if kind not in SESSION_KINDS:
            raise ValueError(f"Unsupported session kind: {kind}")
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError("cwd must be a string when provided.")
        metadata = self._validate_metadata(metadata)
        metadata.setdefault("daemon_id", self.daemon_id)
        metadata.setdefault("daemon_pid", os.getpid())
        session_type = self._session_type(kind)
        spec = SessionSpec(kind=kind, cwd=cwd, metadata=metadata)
        snapshot = self.store.create_session(spec, status="starting")
        session = None
        try:
            if issubclass(session_type, ProcessSession):
                session = session_type(cwd=cwd)
            else:
                session = session_type(cwd=cwd)
            pid = getattr(getattr(session, "process", None), "pid", None)
            with self._lock:
                self._sessions[snapshot.id] = session
                self._session_queues.setdefault(snapshot.id, deque())
            return self.store.update_session(
                snapshot.id,
                status=SESSION_RUNNING,
                pid=pid,
            )
        except Exception:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            metadata = dict(snapshot.metadata)
            metadata["create_failed"] = True
            self.store.update_session(
                snapshot.id,
                status=SESSION_CRASHED,
                closed_at=utc_now(),
                metadata=metadata,
            )
            raise

    def list_sessions(self) -> list[SessionSnapshot]:
        return self.store.list_sessions()

    def session_snapshot(self, session_id: str) -> SessionSnapshot:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string.")
        snapshot = self.store.get_session(session_id)
        if snapshot is None:
            raise KeyError(f"Unknown session: {session_id}")
        return snapshot

    def close_session(self, session_id: str) -> SessionSnapshot:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string.")
        snapshot = self.session_snapshot(session_id)
        with self._lock:
            session = self._sessions.pop(session_id, None)
            self._session_queues.pop(session_id, None)
            active_commands = self.store.list_active_commands(session_id=session_id)
            active_threads = list(
                {
                    thread
                    for command in active_commands
                    if (thread := self._command_threads.get(command.id)) is not None
                }
            )
            active_cancel_events = [
                self._command_cancels.get(command.id) for command in active_commands
            ]

        for cancel_event in active_cancel_events:
            if cancel_event is not None:
                cancel_event.set()

        if active_commands:
            termination_strategy = self._terminate_session(session)
            for command in active_commands:
                self._finish_canceled(
                    command.id,
                    reason="session_closed",
                    session=session,
                    termination_strategy=termination_strategy,
                    close_session=True,
                )
            for thread in active_threads:
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=WORKER_JOIN_TIMEOUT_SECONDS)

        if session is not None and not active_commands:
            try:
                session.close()
            except Exception:
                metadata = dict(snapshot.metadata)
                metadata["close_error"] = True
                return self.store.update_session(
                    session_id,
                    status=SESSION_CRASHED,
                    closed_at=utc_now(),
                    metadata=metadata,
                )

        latest = self.store.get_session(session_id) or snapshot
        metadata = dict(latest.metadata)
        if active_commands:
            metadata["closed_running_commands"] = [command.id for command in active_commands]
            metadata["close_skipped_blocking_session_close"] = True

        return self.store.update_session(
            session_id,
            status=SESSION_CLOSED,
            closed_at=utc_now(),
            metadata=metadata,
        )

    def start_command(
        self,
        session_id: str,
        command: str,
        *,
        cwd: str | None = None,
        timeout_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CommandHandle:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string.")
        if not isinstance(command, str) or not command:
            raise ValueError("command must be a non-empty string.")
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError("cwd must be a string when provided.")
        metadata = self._validate_metadata(metadata)
        session_snapshot = self.session_snapshot(session_id)
        if session_snapshot.status != SESSION_RUNNING:
            raise RuntimeError(f"Session {session_id} is not running.")
        self._validate_command_cwd(session_snapshot, cwd)
        timeout_seconds = self._validate_timeout_seconds(timeout_seconds)
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None or not session.is_running():
            raise RuntimeError(
                f"Session {session_id} is not live in this service. "
                "Start commands through the running daemon that owns the session."
            )

        spec = CommandSpec(
            session_id=session_id,
            command=command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            metadata=metadata,
        )
        snapshot = self.store.create_command(spec)
        cancel_event = threading.Event()
        with self._lock:
            self._command_cancels[snapshot.id] = cancel_event
            queue = self._session_queues.setdefault(session_id, deque())
            queue.append(snapshot.id)
            thread = self._session_queue_threads.get(session_id)
            should_start = thread is None or not thread.is_alive()
            if should_start:
                thread = threading.Thread(
                    target=self._run_session_queue_worker,
                    args=(session_id,),
                    daemon=True,
                    name=f"liveshell-session-queue-{session_id}",
                )
                self._session_queue_threads[session_id] = thread
            self._command_threads[snapshot.id] = thread
        if should_start:
            thread.start()
        return CommandHandle(snapshot.id, self.store, self)

    def poll_command(self, command_id: str) -> CommandSnapshot:
        if not isinstance(command_id, str) or not command_id:
            raise ValueError("command_id must be a non-empty string.")
        snapshot = self.store.get_command(command_id)
        if snapshot is None:
            raise KeyError(f"Unknown command: {command_id}")
        return snapshot

    def command_events(self, command_id: str, *, since_seq: int = 0):
        if isinstance(since_seq, bool):
            raise ValueError("since_seq must be a non-negative integer.")
        try:
            since_seq = int(since_seq)
        except (TypeError, ValueError) as exc:
            raise ValueError("since_seq must be a non-negative integer.") from exc
        if since_seq < 0:
            raise ValueError("since_seq must be a non-negative integer.")
        self.poll_command(command_id)
        return self.store.list_command_events(command_id, since_seq=since_seq)

    def command_result(self, command_id: str) -> CommandResult | None:
        self.poll_command(command_id)
        return CommandHandle(command_id, self.store, self).result()

    def cancel_command(
        self,
        command_id: str,
        *,
        reason: str | None = None,
    ) -> CommandSnapshot:
        if not isinstance(command_id, str) or not command_id:
            raise ValueError("command_id must be a non-empty string.")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("reason must be a string when provided.")
        snapshot = self.poll_command(command_id)
        if snapshot.status in TERMINAL_COMMAND_STATUSES:
            return snapshot

        with self._lock:
            cancel_event = self._command_cancels.get(command_id)
            session = self._sessions.get(snapshot.session_id)
            thread = self._command_threads.get(command_id)
        if cancel_event is not None:
            cancel_event.set()
        if snapshot.status == COMMAND_QUEUED:
            termination_strategy = "queued_cancel"
            close_session = False
        else:
            termination_strategy = self._terminate_session(session)
            close_session = True
        snapshot = self._finish_canceled(
            command_id,
            reason=reason,
            session=session,
            termination_strategy=termination_strategy,
            close_session=close_session,
        )
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=WORKER_JOIN_TIMEOUT_SECONDS)
        return snapshot

    def _run_session_queue_worker(self, session_id: str) -> None:
        while True:
            with self._lock:
                queue = self._session_queues.get(session_id)
                if not queue:
                    self._session_queue_threads.pop(session_id, None)
                    return
                command_id = queue.popleft()
                session = self._sessions.get(session_id)
                cancel_event = self._command_cancels.get(command_id)
                self._command_threads[command_id] = threading.current_thread()

            if cancel_event is None:
                continue
            if self._is_terminal(command_id):
                self._forget_command_runtime(command_id)
                continue
            if cancel_event.is_set():
                self._finish_canceled_if_active(
                    command_id,
                    reason="cancel_requested",
                    session=session,
                    termination_strategy="queued_cancel",
                    close_session=False,
                )
                self._forget_command_runtime(command_id)
                continue
            if session is None or not session.is_running():
                self._finish_canceled_if_active(
                    command_id,
                    reason="session_not_live",
                    session=session,
                    termination_strategy="none",
                    close_session=True,
                )
                self._forget_command_runtime(command_id)
                continue

            self._run_command_worker(command_id, session, cancel_event)

    def _run_command_worker(
        self,
        command_id: str,
        session: Any,
        cancel_event: threading.Event,
    ) -> None:
        try:
            if self._is_terminal(command_id):
                return
            if cancel_event.is_set():
                self._finish_canceled_if_active(
                    command_id,
                    reason="cancel_requested",
                    session=session,
                    termination_strategy="queued_cancel",
                    close_session=False,
                )
                return

            started_at = utc_now()
            self.store.update_command(
                command_id,
                status=COMMAND_STARTING,
                started_at=started_at,
            )
            self.store.append_command_event(command_id, EVENT_COMMAND_STARTED)

            if self._is_terminal(command_id):
                return
            if cancel_event.is_set():
                self._finish_canceled_if_active(
                    command_id,
                    reason="cancel_requested",
                    session=session,
                    termination_strategy="queued_cancel",
                    close_session=False,
                )
                return

            self.store.update_command(command_id, status=COMMAND_RUNNING)

            snapshot = self.poll_command(command_id)
            result_holder: dict[str, Any] = {}
            exec_thread = threading.Thread(
                target=self._execute_command,
                args=(command_id, session, snapshot.command, result_holder),
                daemon=True,
                name=f"liveshell-exec-{command_id}",
            )
            exec_thread.start()

            deadline = (
                time.monotonic() + float(snapshot.timeout_seconds)
                if snapshot.timeout_seconds is not None
                else None
            )

            while exec_thread.is_alive():
                if self._is_terminal(command_id):
                    return
                if cancel_event.is_set():
                    termination_strategy = self._terminate_session(session)
                    self._finish_canceled_if_active(
                        command_id,
                        reason="cancel_requested",
                        session=session,
                        termination_strategy=termination_strategy,
                        close_session=True,
                    )
                    exec_thread.join(timeout=EXEC_THREAD_JOIN_TIMEOUT_SECONDS)
                    return
                if deadline is not None and time.monotonic() >= deadline:
                    termination_strategy = self._terminate_session(session)
                    if not self._is_terminal(command_id):
                        self._finish_timed_out(
                            command_id,
                            session=session,
                            termination_strategy=termination_strategy,
                        )
                    exec_thread.join(timeout=EXEC_THREAD_JOIN_TIMEOUT_SECONDS)
                    return
                time.sleep(0.05)

            exec_thread.join()
            if self._is_terminal(command_id):
                return

            error = result_holder.get("error")
            if error is not None:
                error_text = str(error)
                if error_text:
                    self._append_output_event(command_id, EVENT_STDERR, error_text)
                self._finish_failed(
                    command_id,
                    error_text,
                    stderr=error_text,
                    exit_code=None,
                )
                return

            stdout = result_holder.get("stdout", "")
            stderr = result_holder.get("stderr", "")
            exit_code = result_holder.get("exit_code", 0)
            stdout_streamed = bool(result_holder.get("stdout_streamed"))
            stderr_streamed = bool(result_holder.get("stderr_streamed"))
            if stdout and not stdout_streamed:
                self._append_output_event(command_id, EVENT_STDOUT, stdout)
            if stderr and not stderr_streamed:
                self._append_output_event(command_id, EVENT_STDERR, stderr)

            if exit_code == 0:
                self._finish_completed(command_id, stdout, stderr, exit_code)
            else:
                self._finish_failed(
                    command_id,
                    stderr or stdout,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=exit_code,
                )
        except Exception as exc:
            try:
                if not self._is_terminal(command_id):
                    error_text = str(exc)
                    if error_text:
                        self._append_output_event(command_id, EVENT_STDERR, error_text)
                    self._finish_failed(
                        command_id,
                        error_text,
                        stderr=error_text,
                        exit_code=None,
                    )
            except Exception:
                pass
        finally:
            with self._lock:
                self._command_cancels.pop(command_id, None)
                self._command_threads.pop(command_id, None)

    def _forget_command_runtime(self, command_id: str) -> None:
        with self._lock:
            self._command_cancels.pop(command_id, None)
            self._command_threads.pop(command_id, None)

    def _finish_canceled_if_active(
        self,
        command_id: str,
        *,
        reason: str | None,
        session: Any,
        termination_strategy: str,
        close_session: bool,
    ) -> CommandSnapshot | None:
        if self._is_terminal(command_id):
            return self.store.get_command(command_id)
        return self._finish_canceled(
            command_id,
            reason=reason,
            session=session,
            termination_strategy=termination_strategy,
            close_session=close_session,
        )

    def _execute_command(
        self,
        command_id: str,
        session: Any,
        command: str,
        result_holder: dict[str, Any],
    ) -> None:
        try:
            stdout_parts: list[str] = []
            stderr_parts: list[str] = []

            def append_stdout(text: str) -> None:
                stdout_parts.append(text)
                if not self._is_terminal(command_id):
                    self._append_output_event(
                        command_id,
                        EVENT_STDOUT,
                        text,
                        metadata={"streamed": True},
                    )

            def append_stderr(text: str) -> None:
                stderr_parts.append(text)
                if not self._is_terminal(command_id):
                    self._append_output_event(
                        command_id,
                        EVENT_STDERR,
                        text,
                        metadata={"streamed": False, "captured": True},
                    )

            if isinstance(session, ProcessSession):
                result = session.run_stream(
                    command,
                    check=False,
                    stdout_callback=append_stdout,
                    stderr_callback=append_stderr,
                )
            elif hasattr(session, "run_result"):
                result = session.run_result(command, check=False)
            else:
                result = session.run(command, check=False)

            if isinstance(result, ShellProcessResult):
                result_holder["stdout"] = (
                    "".join(stdout_parts) if stdout_parts else result.output
                )
                result_holder["stderr"] = (
                    "".join(stderr_parts) if stderr_parts else result.stderr
                )
                result_holder["exit_code"] = result.exit_code
                result_holder["stdout_streamed"] = bool(stdout_parts)
                result_holder["stderr_streamed"] = bool(stderr_parts)
            elif isinstance(result, PowerShellResult):
                result_holder["stdout"] = result.stdout
                result_holder["stderr"] = result.stderr
                result_holder["exit_code"] = result.exit_code
            else:
                result_holder["stdout"] = "\n".join(str(item) for item in result)
                result_holder["stderr"] = ""
                result_holder["exit_code"] = 0
        except BaseException as exc:
            result_holder["error"] = exc

    def _finish_completed(
        self,
        command_id: str,
        stdout: str,
        stderr: str,
        exit_code: int,
    ) -> CommandSnapshot:
        return self._finish_terminal(
            command_id,
            status=COMMAND_COMPLETED,
            event_type=EVENT_COMMAND_COMPLETED,
            event_text="",
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
        )

    def _finish_failed(
        self,
        command_id: str,
        text: str,
        *,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None,
    ) -> CommandSnapshot:
        return self._finish_terminal(
            command_id,
            status=COMMAND_FAILED,
            event_type=EVENT_COMMAND_FAILED,
            event_text=text,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
        )

    def _finish_timed_out(
        self,
        command_id: str,
        *,
        session: Any,
        termination_strategy: str,
    ) -> CommandSnapshot:
        self._mark_session_closed_for_command(
            command_id,
            reason="command_timed_out",
            status=SESSION_CLOSED,
        )
        return self._finish_terminal(
            command_id,
            status=COMMAND_TIMED_OUT,
            event_type=EVENT_COMMAND_TIMED_OUT,
            event_text="Command timed out.",
            stdout="",
            stderr="",
            exit_code=None,
            metadata_updates={
                "requested_at": utc_now(),
                "reason": "timeout",
                "session_closed": session is not None,
                "termination_strategy": termination_strategy,
            },
        )

    def _finish_canceled(
        self,
        command_id: str,
        *,
        reason: str | None,
        session: Any,
        termination_strategy: str,
        close_session: bool,
    ) -> CommandSnapshot:
        if close_session:
            self._mark_session_closed_for_command(
                command_id,
                reason="command_canceled",
                status=SESSION_CLOSED,
            )
        return self._finish_terminal(
            command_id,
            status=COMMAND_CANCELED,
            event_type=EVENT_COMMAND_CANCELED,
            event_text=reason or "",
            stdout="",
            stderr="",
            exit_code=None,
            metadata_updates={
                "cancel_reason": reason,
                "requested_at": utc_now(),
                "reason": reason,
                "session_closed": close_session and session is not None,
                "termination_strategy": termination_strategy,
            },
        )

    def _finish_terminal(
        self,
        command_id: str,
        *,
        status: str,
        event_type: str,
        event_text: str,
        stdout: str,
        stderr: str,
        exit_code: int | None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> CommandSnapshot:
        current = self.poll_command(command_id)
        if current.status in TERMINAL_COMMAND_STATUSES:
            return current
        metadata = dict(current.metadata)
        if metadata_updates:
            metadata.update(metadata_updates)
        if not stdout and not stderr:
            stdout, stderr = self._collected_output(command_id)
        output = stdout + stderr
        return self.store.finish_command(
            command_id,
            status=status,
            event_type=event_type,
            event_text=event_text,
            stdout_tail=stdout,
            stderr_tail=stderr,
            output_hash=hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest(),
            exit_code=exit_code,
            metadata=metadata,
        )

    def _collected_output(self, command_id: str) -> tuple[str, str]:
        events = self.store.list_command_events(command_id)
        stdout = "".join(event.text for event in events if event.event_type == EVENT_STDOUT)
        stderr = "".join(event.text for event in events if event.event_type == EVENT_STDERR)
        return stdout, stderr

    def _append_output_event(
        self,
        command_id: str,
        event_type: str,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not text:
            return
        chunk_size = self.event_chunk_size
        if chunk_size is None or len(text) <= chunk_size:
            self.store.append_command_event(
                command_id,
                event_type,
                text,
                metadata=metadata,
            )
            return

        chunks = [
            text[index : index + chunk_size]
            for index in range(0, len(text), chunk_size)
        ]
        events = []
        for index, chunk in enumerate(chunks):
            chunk_metadata = dict(metadata or {})
            chunk_metadata.update(
                {
                    "chunked": True,
                    "chunk_index": index,
                    "chunk_count": len(chunks),
                }
            )
            events.append(
                {
                    "event_type": event_type,
                    "text": chunk,
                    "metadata": chunk_metadata,
                }
            )
        self.store.append_command_events(command_id, events)

    def _mark_session_closed_for_command(
        self,
        command_id: str,
        *,
        reason: str,
        status: str,
    ) -> None:
        command = self.store.get_command(command_id)
        if command is None:
            return
        session = self.store.get_session(command.session_id)
        if session is None:
            return
        metadata = dict(session.metadata)
        metadata["closed_by_command"] = command_id
        metadata["closed_reason"] = reason
        self.store.update_session(
            session.id,
            status=status,
            closed_at=utc_now(),
            metadata=metadata,
        )
        with self._lock:
            self._sessions.pop(session.id, None)

    def _terminate_session(self, session: Any) -> str:
        if session is None:
            return "none"
        process = getattr(session, "process", None)
        if process is not None and process.poll() is None:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=PROCESS_TERMINATE_TIMEOUT_SECONDS)
                    return "posix_process_group_sigterm"
                except Exception:
                    pass
            else:
                ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
                if ctrl_break is not None:
                    try:
                        process.send_signal(ctrl_break)
                        process.wait(timeout=PROCESS_TERMINATE_TIMEOUT_SECONDS)
                        return "windows_process_group_ctrl_break"
                    except Exception:
                        pass
            try:
                process.terminate()
                process.wait(timeout=PROCESS_TERMINATE_TIMEOUT_SECONDS)
                return "process_terminate"
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=PROCESS_TERMINATE_TIMEOUT_SECONDS)
                    return "process_kill_after_timeout"
                except Exception:
                    pass
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=PROCESS_TERMINATE_TIMEOUT_SECONDS)
                    return "process_kill_after_error"
                except Exception:
                    pass
        try:
            session.close()
            return "session_close"
        except Exception:
            return "session_close_error"

    def _is_terminal(self, command_id: str) -> bool:
        snapshot = self.store.get_command(command_id)
        return snapshot is not None and snapshot.status in TERMINAL_COMMAND_STATUSES

    @staticmethod
    def _session_type(kind: str):
        if kind == "cmd":
            return Cmd
        if kind == "bash":
            return Bash
        if kind == "powershell":
            return PowerShell
        raise ValueError(f"Unsupported session kind: {kind}")

    @staticmethod
    def _validate_command_cwd(
        session_snapshot: SessionSnapshot,
        command_cwd: str | None,
    ) -> None:
        if command_cwd is None:
            return
        if session_snapshot.cwd is None:
            raise ValueError(
                "Per-command cwd is not supported for persistent sessions in this slice. "
                "Create the session with cwd instead."
            )
        session_cwd = Path(session_snapshot.cwd).resolve()
        requested_cwd = Path(command_cwd).resolve()
        if session_cwd != requested_cwd:
            raise ValueError(
                "Per-command cwd must match the persistent session cwd. "
                "Create a separate session for a different cwd."
            )

    @staticmethod
    def _validate_timeout_seconds(timeout_seconds: Any) -> float | None:
        if timeout_seconds is None:
            return None
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, Real):
            raise ValueError("timeout_seconds must be a positive number.")
        timeout = float(timeout_seconds)
        if timeout <= 0:
            raise ValueError("timeout_seconds must be greater than zero.")
        return timeout

    @staticmethod
    def _validate_metadata(metadata: Any) -> dict[str, Any]:
        if metadata is None:
            return {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be a JSON object when provided.")
        return dict(metadata)


class JsonLineDaemon:
    def __init__(self, service: LiveShellService):
        self.service = service

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        try:
            method = request.get("method")
            if not isinstance(method, str) or not method:
                raise ValueError("Request method must be a non-empty string.")
            params = request.get("params", {})
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise ValueError("Request params must be a JSON object.")
            result = self._dispatch(method, params)
            return {"id": request_id, "ok": True, "result": result}
        except Exception as exc:
            return {
                "id": request_id,
                "ok": False,
                "error": protocol_error_payload(exc),
            }

    def serve_stdio(
        self,
        *,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
        once: bool = False,
    ) -> None:
        input_stream = input_stream or sys.stdin
        output_stream = output_stream or sys.stdout
        try:
            for line in input_stream:
                line = line.strip()
                if not line:
                    if once or self.service.shutdown_requested():
                        break
                    continue
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError("Request must be a JSON object.")
                    response = self.handle_request(request)
                except Exception as exc:
                    response = {
                        "id": None,
                        "ok": False,
                        "error": protocol_error_payload(exc),
                    }
                output_stream.write(json.dumps(response, separators=(",", ":")) + "\n")
                output_stream.flush()
                if once or self.service.shutdown_requested():
                    break
        finally:
            if not self.service.shutdown_requested():
                self.service.request_shutdown(reason="stdio_closed")

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "capability.discover":
            return {
                "protocol_version": PROTOCOL_VERSION,
                "capabilities": self.service.discover_capabilities(),
            }
        if method == "daemon.status":
            return self.service.daemon_status()
        if method == "daemon.shutdown":
            reason = params.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise ValueError("reason must be a string when provided.")
            return self.service.request_shutdown(reason=reason)
        if method == "session.create":
            return self.service.create_session(
                self._required_string(params, "kind"),
                cwd=params.get("cwd"),
                metadata=params.get("metadata"),
            ).to_dict()
        if method == "session.list":
            return [session.to_dict() for session in self.service.list_sessions()]
        if method == "session.snapshot":
            return self.service.session_snapshot(
                self._required_string(params, "session_id")
            ).to_dict()
        if method == "session.close":
            return self.service.close_session(
                self._required_string(params, "session_id")
            ).to_dict()
        if method == "command.start":
            handle = self.service.start_command(
                self._required_string(params, "session_id"),
                self._required_string(params, "command"),
                cwd=params.get("cwd"),
                timeout_seconds=params.get("timeout_seconds"),
                metadata=params.get("metadata"),
            )
            return {
                "command_id": handle.command_id,
                "command": handle.poll().to_dict(),
            }
        if method == "command.poll":
            return self.service.poll_command(
                self._required_string(params, "command_id")
            ).to_dict()
        if method == "command.events":
            return [
                event.to_dict()
                for event in self.service.command_events(
                    self._required_string(params, "command_id"),
                    since_seq=params.get("since_seq", 0),
                )
            ]
        if method == "command.cancel":
            return self.service.cancel_command(
                self._required_string(params, "command_id"),
                reason=params.get("reason"),
            ).to_dict()
        if method == "command.result":
            result = self.service.command_result(
                self._required_string(params, "command_id")
            )
            return result.to_dict() if result is not None else None
        raise UnknownMethodError(f"Unknown method: {method}")

    @staticmethod
    def _required_string(params: dict[str, Any], name: str) -> str:
        value = params.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string.")
        return value


def serve_stdio(state_dir: str | Path, *, once: bool = False) -> None:
    service = LiveShellService(Store.from_state_dir(state_dir), transport="stdio")
    JsonLineDaemon(service).serve_stdio(once=once)


SOCKET_ACCEPT_POLL_SECONDS = 0.5


def _serve_socket_connection(daemon: "JsonLineDaemon", conn: Any) -> None:
    """Serve one persistent client connection: the same JSON-line request/response
    protocol as stdio, but framed over a socket. Closing the client socket (e.g. the
    launching process exits) ends only this connection; the daemon keeps running."""
    import socket as _socket

    reader = conn.makefile("r", encoding="utf-8")
    writer = conn.makefile("w", encoding="utf-8")
    try:
        for line in reader:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("Request must be a JSON object.")
                response = daemon.handle_request(request)
            except Exception as exc:
                response = {"id": None, "ok": False, "error": protocol_error_payload(exc)}
            try:
                writer.write(json.dumps(response, separators=(",", ":")) + "\n")
                writer.flush()
            except OSError:
                break
            if daemon.service.shutdown_requested():
                break
    except (OSError, _socket.error):
        pass
    finally:
        for stream in (reader, writer):
            try:
                stream.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass


def serve_socket(
    state_dir: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    recover: bool = True,
) -> dict[str, Any]:
    """Run a persistent LiveShell daemon over a loopback TCP socket.

    Unlike the stdio transport (whose lifetime is bound to the launching process's
    pipes), a socket daemon keeps running — and its commands keep executing — after
    any client disconnects. The bound address is published into the state dir so a
    fresh client process can reconnect via ``LiveShellClient.connect(state_dir)``.
    Intended to be launched detached by ``liveshell daemon start``."""
    import socket

    normalized_host = _normalize_loopback_host(host)
    service = LiveShellService(Store.from_state_dir(state_dir), transport="tcp", recover=recover)
    daemon = JsonLineDaemon(service)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((normalized_host, int(port)))
        server.listen(128)
        bound_host, bound_port = server.getsockname()[:2]
        service.set_socket_address(bound_host, int(bound_port))
        server.settimeout(SOCKET_ACCEPT_POLL_SECONDS)
        while not service.shutdown_requested():
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(None)
            thread = threading.Thread(
                target=_serve_socket_connection,
                args=(daemon, conn),
                daemon=True,
                name="liveshell-socket-conn",
            )
            thread.start()
    finally:
        try:
            server.close()
        except OSError:
            pass
        if not service.shutdown_requested():
            service.request_shutdown(reason="socket_server_stopped")
    return {"exited": True, "daemon_id": service.daemon_id, "socket_host": service._socket_host, "socket_port": service._socket_port}


def _normalize_loopback_host(host: str) -> str:
    text = str(host).strip()
    if not text:
        raise ValueError("host must be a loopback IPv4 address or 'localhost'.")
    if text.lower() == "localhost":
        return "127.0.0.1"
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError as exc:
        raise ValueError("host must be a loopback IPv4 address or 'localhost'.") from exc
    if parsed.version != 4 or not parsed.is_loopback:
        raise ValueError("host must be a loopback IPv4 address or 'localhost'.")
    return str(parsed)


def read_daemon_metadata(state_dir: str | Path) -> dict[str, Any]:
    state_path = Path(state_dir)
    metadata_path = state_path / DAEMON_STATE_FILE
    lock_path = state_path / DAEMON_LOCK_FILE
    payload: dict[str, Any] = {
        "state_dir": str(state_path),
        "metadata_path": str(metadata_path),
        "lock_path": str(lock_path),
        "running": False,
        "metadata": None,
    }
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload["status"] = "missing"
        return payload
    except (OSError, json.JSONDecodeError) as exc:
        payload["status"] = "unreadable"
        payload["error"] = {"type": exc.__class__.__name__, "message": str(exc)}
        return payload

    pid = metadata.get("pid")
    payload["metadata"] = metadata
    payload["pid"] = pid
    payload["running"] = _pid_is_running(pid)
    payload["status"] = "running" if payload["running"] else "stale"
    payload["shutdown_requested"] = (state_path / DAEMON_SHUTDOWN_FILE).exists()
    return payload


def request_daemon_shutdown_marker(
    state_dir: str | Path,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    state_path = Path(state_dir)
    state_path.mkdir(parents=True, exist_ok=True)
    requested_at = utc_now()
    payload = {
        "requested_at": requested_at,
        "reason": reason,
        "state_dir": str(state_path),
        "transport": "state_file_marker",
    }
    (state_path / DAEMON_SHUTDOWN_FILE).write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    status = read_daemon_metadata(state_path)
    return {
        "shutdown_requested": True,
        "request": payload,
        "daemon": status,
        "note": (
            "Stdio daemons also support the daemon.shutdown protocol method. "
            "The CLI marker is observed only when a daemon checks its state dir."
        ),
    }


def _pid_is_running(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        return _pid_is_running_windows(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except ValueError:
        return False
    return True


def _pid_is_running_windows(pid: int) -> bool:
    """Liveness probe for Windows.

    ``os.kill(pid, 0)`` is a POSIX idiom and is not valid here: on Windows
    CPython has no special case for signal ``0`` and routes through
    ``TerminateProcess``, so it neither reliably reports a dead pid nor leaves a
    live process untouched. Query the process object directly instead.
    """
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        # A still-running process never signals, so the zero-wait times out.
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)
