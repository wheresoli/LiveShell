from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

from .models import (
    CommandEvent,
    CommandResult,
    CommandSnapshot,
    SessionSnapshot,
    TERMINAL_COMMAND_STATUSES,
)
from .store import Store


class CommandService(Protocol):
    def poll_command(self, command_id: str) -> CommandSnapshot:
        ...

    def command_events(
        self,
        command_id: str,
        *,
        since_seq: int = 0,
    ) -> list[CommandEvent]:
        ...

    def command_result(self, command_id: str) -> CommandResult | None:
        ...

    def cancel_command(
        self,
        command_id: str,
        *,
        reason: str | None = None,
    ) -> CommandSnapshot:
        ...


class CommandHandle:
    def __init__(
        self,
        command_id: str,
        store: Store | None = None,
        service: CommandService | None = None,
    ):
        self.command_id = command_id
        self._store = store
        self._service = service

    def poll(self) -> CommandSnapshot:
        if self._store is None:
            if self._service is None:
                raise RuntimeError("CommandHandle has no store or service.")
            return self._service.poll_command(self.command_id)

        snapshot = self._store.get_command(self.command_id)
        if snapshot is None:
            raise KeyError(f"Unknown command: {self.command_id}")
        return snapshot

    def events(self, since_seq: int = 0) -> list[CommandEvent]:
        if self._store is None:
            if self._service is None:
                raise RuntimeError("CommandHandle has no store or service.")
            return self._service.command_events(self.command_id, since_seq=since_seq)

        self.poll()
        return self._store.list_command_events(self.command_id, since_seq=since_seq)

    def result(self) -> CommandResult | None:
        if self._store is None:
            if self._service is None:
                raise RuntimeError("CommandHandle has no store or service.")
            return self._service.command_result(self.command_id)

        snapshot = self.poll()
        if snapshot.status not in TERMINAL_COMMAND_STATUSES:
            return None
        return self._store.command_result(self.command_id)

    def cancel(self, reason: str | None = None) -> CommandSnapshot:
        if self._service is not None:
            return self._service.cancel_command(self.command_id, reason=reason)

        snapshot = self.poll()
        if snapshot.status in TERMINAL_COMMAND_STATUSES:
            return snapshot

        raise RuntimeError(
            f"Command {self.command_id} is not owned by a live service in this process. "
            "Send command.cancel to the daemon that started the command."
        )

    def wait(self, poll_interval: float = 0.1) -> CommandResult:
        while True:
            result = self.result()
            if result is not None:
                return result
            time.sleep(poll_interval)

    async def poll_async(self) -> CommandSnapshot:
        return await asyncio.to_thread(self.poll)

    async def events_async(self, since_seq: int = 0) -> list[CommandEvent]:
        return await asyncio.to_thread(self.events, since_seq)

    async def result_async(self) -> CommandResult | None:
        return await asyncio.to_thread(self.result)

    async def cancel_async(self, reason: str | None = None) -> CommandSnapshot:
        return await asyncio.to_thread(self.cancel, reason)

    async def wait_async(self, poll_interval: float = 0.1) -> CommandResult:
        while True:
            result = await self.result_async()
            if result is not None:
                return result
            await asyncio.sleep(poll_interval)


class SessionService(Protocol):
    def session_snapshot(self, session_id: str) -> SessionSnapshot:
        ...

    def close_session(self, session_id: str) -> SessionSnapshot:
        ...

    def start_command(
        self,
        session_id: str,
        command: str,
        *,
        cwd: str | None = None,
        timeout_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CommandHandle:
        ...


class SessionHandle:
    def __init__(self, session_id: str, service: SessionService):
        self.session_id = session_id
        self._service = service

    def snapshot(self) -> SessionSnapshot:
        return self._service.session_snapshot(self.session_id)

    poll = snapshot

    def close(self) -> SessionSnapshot:
        return self._service.close_session(self.session_id)

    def start_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CommandHandle:
        return self._service.start_command(
            self.session_id,
            command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            metadata=metadata,
        )

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
        poll_interval: float = 0.1,
    ) -> CommandResult:
        return self.start_command(
            command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            metadata=metadata,
        ).wait(poll_interval=poll_interval)

    async def snapshot_async(self) -> SessionSnapshot:
        return await asyncio.to_thread(self.snapshot)

    poll_async = snapshot_async

    async def close_async(self) -> SessionSnapshot:
        return await asyncio.to_thread(self.close)

    async def start_command_async(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CommandHandle:
        return await asyncio.to_thread(
            self.start_command,
            command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            metadata=metadata,
        )

    async def run_async(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
        poll_interval: float = 0.1,
    ) -> CommandResult:
        handle = await self.start_command_async(
            command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            metadata=metadata,
        )
        return await handle.wait_async(poll_interval=poll_interval)
