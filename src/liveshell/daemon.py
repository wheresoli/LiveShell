from __future__ import annotations

import hashlib
import json
from numbers import Real
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


class LiveShellService:
    def __init__(self, store: Store | str | Path, *, recover: bool = True):
        self.store = store if isinstance(store, Store) else Store(store)
        self._sessions: dict[str, Any] = {}
        self._command_cancels: dict[str, threading.Event] = {}
        self._command_threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        if recover:
            self.recover_orphaned_records()

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
                self.store.append_command_event(
                    command.id,
                    EVENT_COMMAND_FAILED,
                    "Command was running when the daemon restarted.",
                    metadata={"recovered_without_worker": True},
                )
                self.store.update_command(
                    command.id,
                    status=COMMAND_FAILED,
                    ended_at=now,
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
        kind = kind.lower()
        session_type = self._session_type(kind)
        spec = SessionSpec(kind=kind, cwd=cwd, metadata=metadata or {})
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
        snapshot = self.store.get_session(session_id)
        if snapshot is None:
            raise KeyError(f"Unknown session: {session_id}")
        return snapshot

    def close_session(self, session_id: str) -> SessionSnapshot:
        snapshot = self.session_snapshot(session_id)
        with self._lock:
            session = self._sessions.pop(session_id, None)
            active_commands = [
                command
                for command in self.store.list_commands(session_id=session_id)
                if command.status in {COMMAND_QUEUED, COMMAND_STARTING, COMMAND_RUNNING}
            ]
            active_threads = [
                self._command_threads.get(command.id)
                for command in active_commands
            ]
            active_cancel_events = [
                self._command_cancels.get(command.id)
                for command in active_commands
            ]

        for cancel_event in active_cancel_events:
            if cancel_event is not None:
                cancel_event.set()

        if active_commands:
            self._terminate_session(session)
            for command in active_commands:
                self._finish_canceled(
                    command.id,
                    reason="session_closed",
                    session=session,
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
        session_snapshot = self.session_snapshot(session_id)
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
            metadata=metadata or {},
        )
        snapshot = self.store.create_command(spec)
        cancel_event = threading.Event()
        with self._lock:
            self._command_cancels[snapshot.id] = cancel_event
        thread = threading.Thread(
            target=self._run_command_worker,
            args=(snapshot.id, session, cancel_event),
            daemon=True,
            name=f"liveshell-command-{snapshot.id}",
        )
        with self._lock:
            self._command_threads[snapshot.id] = thread
        thread.start()
        return CommandHandle(snapshot.id, self.store, self)

    def poll_command(self, command_id: str) -> CommandSnapshot:
        snapshot = self.store.get_command(command_id)
        if snapshot is None:
            raise KeyError(f"Unknown command: {command_id}")
        return snapshot

    def command_events(self, command_id: str, *, since_seq: int = 0):
        self.poll_command(command_id)
        return self.store.list_command_events(command_id, since_seq=since_seq)

    def command_result(self, command_id: str) -> CommandResult | None:
        return CommandHandle(command_id, self.store, self).result()

    def cancel_command(
        self,
        command_id: str,
        *,
        reason: str | None = None,
    ) -> CommandSnapshot:
        snapshot = self.poll_command(command_id)
        if snapshot.status in TERMINAL_COMMAND_STATUSES:
            return snapshot

        with self._lock:
            cancel_event = self._command_cancels.get(command_id)
            session = self._sessions.get(snapshot.session_id)
            thread = self._command_threads.get(command_id)
        if cancel_event is not None:
            cancel_event.set()
        self._terminate_session(session)
        snapshot = self._finish_canceled(command_id, reason=reason, session=session)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=WORKER_JOIN_TIMEOUT_SECONDS)
        return snapshot

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
                    self._terminate_session(session)
                    self._finish_canceled_if_active(
                        command_id,
                        reason="cancel_requested",
                        session=session,
                    )
                    exec_thread.join(timeout=EXEC_THREAD_JOIN_TIMEOUT_SECONDS)
                    return
                if deadline is not None and time.monotonic() >= deadline:
                    self._terminate_session(session)
                    if not self._is_terminal(command_id):
                        self._finish_timed_out(command_id, session=session)
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
                    self.store.append_command_event(command_id, EVENT_STDERR, error_text)
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
                self.store.append_command_event(command_id, EVENT_STDOUT, stdout)
            if stderr and not stderr_streamed:
                self.store.append_command_event(command_id, EVENT_STDERR, stderr)

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
                        self.store.append_command_event(command_id, EVENT_STDERR, error_text)
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

    def _finish_canceled_if_active(
        self,
        command_id: str,
        *,
        reason: str | None,
        session: Any,
    ) -> CommandSnapshot | None:
        if self._is_terminal(command_id):
            return self.store.get_command(command_id)
        return self._finish_canceled(command_id, reason=reason, session=session)

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
                    self.store.append_command_event(
                        command_id,
                        EVENT_STDOUT,
                        text,
                        metadata={"streamed": True},
                    )

            def append_stderr(text: str) -> None:
                stderr_parts.append(text)
                if not self._is_terminal(command_id):
                    self.store.append_command_event(
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

    def _finish_timed_out(self, command_id: str, *, session: Any) -> CommandSnapshot:
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
            metadata_updates={"session_closed": session is not None},
        )

    def _finish_canceled(
        self,
        command_id: str,
        *,
        reason: str | None,
        session: Any,
    ) -> CommandSnapshot:
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
                "session_closed": session is not None,
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
        self.store.append_command_event(command_id, event_type, event_text)
        output = stdout + stderr
        return self.store.update_command(
            command_id,
            status=status,
            exit_code=exit_code,
            ended_at=utc_now(),
            stdout_tail=stdout,
            stderr_tail=stderr,
            output_hash=hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest(),
            metadata=metadata,
        )

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

    def _terminate_session(self, session: Any) -> None:
        if session is None:
            return
        process = getattr(session, "process", None)
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=PROCESS_TERMINATE_TIMEOUT_SECONDS)
                return
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=PROCESS_TERMINATE_TIMEOUT_SECONDS)
                    return
                except Exception:
                    pass
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=PROCESS_TERMINATE_TIMEOUT_SECONDS)
                    return
                except Exception:
                    pass
        try:
            session.close()
        except Exception:
            pass

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
                "error": {
                    "type": exc.__class__.__name__,
                    "message": str(exc),
                },
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
        for line in input_stream:
            line = line.strip()
            if not line:
                if once:
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
                    "error": {
                        "type": exc.__class__.__name__,
                        "message": str(exc),
                    },
                }
            output_stream.write(json.dumps(response, separators=(",", ":")) + "\n")
            output_stream.flush()
            if once:
                break

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "capability.discover":
            return {"capabilities": self.service.discover_capabilities()}
        if method == "session.create":
            return self.service.create_session(
                params["kind"],
                cwd=params.get("cwd"),
                metadata=params.get("metadata"),
            ).to_dict()
        if method == "session.list":
            return [session.to_dict() for session in self.service.list_sessions()]
        if method == "session.snapshot":
            return self.service.session_snapshot(params["session_id"]).to_dict()
        if method == "session.close":
            return self.service.close_session(params["session_id"]).to_dict()
        if method == "command.start":
            handle = self.service.start_command(
                params["session_id"],
                params["command"],
                cwd=params.get("cwd"),
                timeout_seconds=params.get("timeout_seconds"),
                metadata=params.get("metadata"),
            )
            return {
                "command_id": handle.command_id,
                "command": handle.poll().to_dict(),
            }
        if method == "command.poll":
            return self.service.poll_command(params["command_id"]).to_dict()
        if method == "command.events":
            return [
                event.to_dict()
                for event in self.service.command_events(
                    params["command_id"],
                    since_seq=int(params.get("since_seq", 0)),
                )
            ]
        if method == "command.cancel":
            return self.service.cancel_command(
                params["command_id"],
                reason=params.get("reason"),
            ).to_dict()
        if method == "command.result":
            result = self.service.command_result(params["command_id"])
            return result.to_dict() if result is not None else None
        raise ValueError(f"Unknown method: {method}")


def serve_stdio(state_dir: str | Path, *, once: bool = False) -> None:
    service = LiveShellService(Store.from_state_dir(state_dir))
    JsonLineDaemon(service).serve_stdio(once=once)
