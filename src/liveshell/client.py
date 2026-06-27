from __future__ import annotations

import asyncio
from collections import deque
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any, TextIO

from .handles import CommandHandle, SessionHandle
from .models import (
    Capability,
    CommandEvent,
    CommandResult,
    CommandSnapshot,
    SessionSnapshot,
)


class LiveShellClientError(RuntimeError):
    """Base error for local daemon client failures."""


class LiveShellProtocolError(LiveShellClientError):
    """Raised when the daemon transport cannot produce a valid response."""


class LiveShellResponseError(LiveShellClientError):
    """Raised when the daemon returns an application-level error response."""

    def __init__(self, error_type: str, message: str, *, response: dict[str, Any]):
        super().__init__(f"{error_type}: {message}" if message else error_type)
        self.error_type = error_type
        self.message = message
        self.response = response


class LiveShellClient:
    """Synchronous client for the JSON-lines stdio daemon protocol."""

    def __init__(
        self,
        input_stream: TextIO,
        output_stream: TextIO,
        *,
        process: subprocess.Popen[str] | None = None,
        stderr_stream: TextIO | None = None,
    ):
        self._input_stream = input_stream
        self._output_stream = output_stream
        self._process = process
        self._stderr_stream = stderr_stream
        self._request_ids = itertools.count(1)
        self._lock = threading.RLock()
        self._closed = False
        self._stderr_tail: deque[str] = deque(maxlen=50)
        if stderr_stream is not None:
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr,
                args=(stderr_stream,),
                daemon=True,
                name="liveshell-client-stderr",
            )
            self._stderr_thread.start()
        else:
            self._stderr_thread = None

    @classmethod
    def stdio(
        cls,
        state_dir: str | Path,
        *,
        python_executable: str | Path | None = None,
        env: dict[str, str] | None = None,
    ) -> LiveShellClient:
        executable = str(python_executable or sys.executable)
        command = [
            executable,
            "-m",
            "liveshell.cli",
            "daemon",
            "stdio",
            "--state-dir",
            str(state_dir),
        ]
        child_env = cls._child_env(env)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=child_env,
        )
        if process.stdin is None or process.stdout is None:
            process.kill()
            raise LiveShellProtocolError("Failed to open daemon stdio pipes.")
        return cls(
            process.stdin,
            process.stdout,
            process=process,
            stderr_stream=process.stderr,
        )

    @staticmethod
    def _child_env(env: dict[str, str] | None) -> dict[str, str]:
        child_env = dict(os.environ if env is None else env)
        package_root = str(Path(__file__).resolve().parents[1])
        existing = child_env.get("PYTHONPATH")
        child_env["PYTHONPATH"] = (
            package_root if not existing else package_root + os.pathsep + existing
        )
        return child_env

    def __enter__(self) -> LiveShellClient:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def request_envelope(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(method, str) or not method:
            raise ValueError("method must be a non-empty string.")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValueError("params must be a JSON object.")

        request_id = request_id or f"req_{next(self._request_ids)}"
        request = {"id": request_id, "method": method, "params": params}
        encoded = json.dumps(request, separators=(",", ":"))

        with self._lock:
            self._ensure_open()
            try:
                self._input_stream.write(encoded + "\n")
                self._input_stream.flush()
            except OSError as exc:
                raise self._transport_error("Failed to write daemon request.") from exc

            line = self._output_stream.readline()
            if line == "":
                raise self._transport_error("Daemon closed stdout without a response.")

        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LiveShellProtocolError(f"Invalid daemon JSON response: {line!r}") from exc

        if not isinstance(response, dict):
            raise LiveShellProtocolError("Daemon response must be a JSON object.")
        if response.get("id") != request_id:
            raise LiveShellProtocolError(
                f"Daemon response id mismatch: expected {request_id!r}, "
                f"got {response.get('id')!r}."
            )
        return response

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> Any:
        response = self.request_envelope(method, params, request_id=request_id)
        if response.get("ok") is True:
            return response.get("result")
        error = response.get("error")
        if not isinstance(error, dict):
            raise LiveShellProtocolError("Daemon error response is malformed.")
        raise LiveShellResponseError(
            str(error.get("type", "Error")),
            str(error.get("message", "")),
            response=response,
        )

    def discover_capabilities(self) -> list[Capability]:
        result = self.request("capability.discover")
        return [_capability_from_dict(item) for item in result["capabilities"]]

    def create_session(
        self,
        kind: str,
        *,
        cwd: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionHandle:
        params: dict[str, Any] = {"kind": kind}
        if cwd is not None:
            params["cwd"] = cwd
        if metadata is not None:
            params["metadata"] = metadata
        snapshot = _session_from_dict(self.request("session.create", params))
        return SessionHandle(snapshot.id, self)

    def list_sessions(self) -> list[SessionSnapshot]:
        return [_session_from_dict(item) for item in self.request("session.list")]

    def session_snapshot(self, session_id: str) -> SessionSnapshot:
        return _session_from_dict(
            self.request("session.snapshot", {"session_id": session_id})
        )

    def close_session(self, session_id: str) -> SessionSnapshot:
        return _session_from_dict(
            self.request("session.close", {"session_id": session_id})
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
        params: dict[str, Any] = {
            "session_id": session_id,
            "command": command,
        }
        if cwd is not None:
            params["cwd"] = cwd
        if timeout_seconds is not None:
            params["timeout_seconds"] = timeout_seconds
        if metadata is not None:
            params["metadata"] = metadata
        result = self.request("command.start", params)
        return CommandHandle(result["command_id"], service=self)

    def poll_command(self, command_id: str) -> CommandSnapshot:
        return _command_from_dict(
            self.request("command.poll", {"command_id": command_id})
        )

    def command_events(
        self,
        command_id: str,
        *,
        since_seq: int = 0,
    ) -> list[CommandEvent]:
        result = self.request(
            "command.events",
            {"command_id": command_id, "since_seq": since_seq},
        )
        return [_event_from_dict(item) for item in result]

    def command_result(self, command_id: str) -> CommandResult | None:
        result = self.request("command.result", {"command_id": command_id})
        return _result_from_dict(result) if result is not None else None

    def cancel_command(
        self,
        command_id: str,
        *,
        reason: str | None = None,
    ) -> CommandSnapshot:
        params: dict[str, Any] = {"command_id": command_id}
        if reason is not None:
            params["reason"] = reason
        return _command_from_dict(self.request("command.cancel", params))

    async def request_async(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> Any:
        return await asyncio.to_thread(
            self.request,
            method,
            params,
            request_id=request_id,
        )

    async def discover_capabilities_async(self) -> list[Capability]:
        return await asyncio.to_thread(self.discover_capabilities)

    async def create_session_async(
        self,
        kind: str,
        *,
        cwd: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionHandle:
        return await asyncio.to_thread(
            self.create_session,
            kind,
            cwd=cwd,
            metadata=metadata,
        )

    async def close_async(self, *, timeout: float = 2.0) -> None:
        await asyncio.to_thread(self.close, timeout=timeout)

    def close(self, *, timeout: float = 2.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._input_stream.close()
            except Exception:
                pass

        if self._process is None:
            self._close_output_streams()
            return

        try:
            try:
                self._process.wait(timeout=timeout)
                return
            except subprocess.TimeoutExpired:
                self._process.terminate()
            try:
                self._process.wait(timeout=timeout)
                return
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=timeout)
        finally:
            self._close_output_streams()

    def _ensure_open(self) -> None:
        if self._closed:
            raise LiveShellProtocolError("LiveShellClient is closed.")
        if self._process is not None and self._process.poll() is not None:
            raise self._transport_error("Daemon process is no longer running.")

    def _transport_error(self, message: str) -> LiveShellProtocolError:
        return_code = self._process.poll() if self._process is not None else None
        stderr = "".join(self._stderr_tail).strip()
        details = []
        if return_code is not None:
            details.append(f"exit_code={return_code}")
        if stderr:
            details.append(f"stderr={stderr}")
        suffix = " (" + "; ".join(details) + ")" if details else ""
        return LiveShellProtocolError(message + suffix)

    def _drain_stderr(self, stderr_stream: TextIO) -> None:
        try:
            for line in stderr_stream:
                self._stderr_tail.append(line)
        except Exception:
            pass

    def _close_output_streams(self) -> None:
        for stream in (self._output_stream, self._stderr_stream):
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:
                pass


def _capability_from_dict(data: dict[str, Any]) -> Capability:
    return Capability(
        name=str(data["name"]),
        available=bool(data["available"]),
        details=dict(data.get("details") or {}),
    )


def _session_from_dict(data: dict[str, Any]) -> SessionSnapshot:
    return SessionSnapshot(
        id=data["id"],
        kind=data["kind"],
        status=data["status"],
        cwd=data.get("cwd"),
        pid=data.get("pid"),
        started_at=data["started_at"],
        updated_at=data["updated_at"],
        closed_at=data.get("closed_at"),
        metadata=dict(data.get("metadata") or {}),
    )


def _command_from_dict(data: dict[str, Any]) -> CommandSnapshot:
    return CommandSnapshot(
        id=data["id"],
        session_id=data["session_id"],
        command=data["command"],
        status=data["status"],
        cwd=data.get("cwd"),
        timeout_seconds=data.get("timeout_seconds"),
        exit_code=data.get("exit_code"),
        started_at=data.get("started_at"),
        updated_at=data["updated_at"],
        ended_at=data.get("ended_at"),
        stdout_tail=data.get("stdout_tail", ""),
        stderr_tail=data.get("stderr_tail", ""),
        output_hash=data.get("output_hash"),
        metadata=dict(data.get("metadata") or {}),
    )


def _event_from_dict(data: dict[str, Any]) -> CommandEvent:
    return CommandEvent(
        id=data["id"],
        command_id=data["command_id"],
        seq=int(data["seq"]),
        event_type=data["event_type"],
        text=data.get("text", ""),
        created_at=data["created_at"],
        metadata=dict(data.get("metadata") or {}),
    )


def _result_from_dict(data: dict[str, Any]) -> CommandResult:
    return CommandResult(
        command=_command_from_dict(data["command"]),
        events=[_event_from_dict(item) for item in data["events"]],
        stdout=data.get("stdout", ""),
        stderr=data.get("stderr", ""),
    )
