"""Tests for the persistent (socket-transport) daemon.

Unlike the stdio daemon — whose lifetime is bound to the launching process's pipes —
a socket daemon keeps running, and its commands keep executing, after any client
disconnects. This is what enables true detached supervision from a separate process.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from liveshell import Bash, Cmd  # noqa: E402
from liveshell.client import LiveShellClient, LiveShellClientError  # noqa: E402
from liveshell.daemon import read_daemon_metadata, serve_socket  # noqa: E402
from liveshell.models import TERMINAL_COMMAND_STATUSES  # noqa: E402


def available_shell() -> str | None:
    if Cmd.is_available():
        return "cmd"
    if Bash.is_available():
        return "bash"
    return None


def sleep_then_echo(token: str, seconds: float = 1.0) -> str:
    return subprocess.list2cmdline(
        [sys.executable, "-c", f"import time; time.sleep({seconds}); print('{token}')"]
    )


def rmtree_retry(path: Path, *, attempts: int = 10) -> None:
    # Windows cannot delete files held by a daemon that has not yet fully exited;
    # retry briefly, then give up quietly (the temp dir is the OS's to reclaim).
    for _ in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.1)


def wait_for_socket(state_dir: Path, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = read_daemon_metadata(state_dir)
        if (meta.get("metadata") or {}).get("socket_port"):
            return meta
        time.sleep(0.02)
    raise AssertionError("socket daemon did not publish an address in time")


def wait_for_terminal(client: LiveShellClient, command_id: str, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = client.poll_command(command_id)
        if snapshot.status in TERMINAL_COMMAND_STATUSES:
            return snapshot
        time.sleep(0.05)
    raise AssertionError(f"command {command_id} did not reach a terminal status in time")


def wait_until_unreachable(state_dir: Path, timeout: float = 10.0) -> bool:
    # PID liveness is unreliable on Windows (PID reuse), so prove the daemon is
    # gone by the only thing that matters: its socket no longer accepts clients.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with LiveShellClient.connect(state_dir) as client:
                client.daemon_status()
        except LiveShellClientError:
            return True
        time.sleep(0.1)
    return False


class InProcessSocketDaemonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp(prefix="liveshell-socket-")) / "state"
        self.state.mkdir()
        self.addCleanup(rmtree_retry, self.state.parent)

    def test_socket_daemon_serves_and_client_close_keeps_it_running(self) -> None:
        shell = available_shell()
        if shell is None:
            self.skipTest("no cmd/bash shell available")
        errors: list[BaseException] = []

        def serve() -> None:
            try:
                serve_socket(self.state, host="127.0.0.1", port=0)
            except BaseException as exc:  # noqa: BLE001 - surface to the test thread
                errors.append(exc)

        server_thread = threading.Thread(target=serve, daemon=True)
        server_thread.start()
        try:
            wait_for_socket(self.state)
            # First client runs a command, then fully closes its connection.
            with LiveShellClient.connect(self.state) as client_a:
                status = client_a.daemon_status()
                self.assertEqual(status["transport"], "tcp")
                session = client_a.create_session(shell)
                handle = client_a.start_command(session.session_id, sleep_then_echo("alpha", 0.2))
                command_id = handle.command_id
            # client_a is closed; the daemon must still serve a fresh client.
            with LiveShellClient.connect(self.state) as client_b:
                snapshot = wait_for_terminal(client_b, command_id)
                self.assertEqual(snapshot.status, "completed")
                result = client_b.command_result(command_id)
                self.assertIn("alpha", result.stdout)
                client_b.daemon_shutdown(reason="test-done")
        finally:
            server_thread.join(timeout=5.0)
        self.assertFalse(errors, f"serve_socket raised: {errors}")
        self.assertFalse(server_thread.is_alive())


class DetachedDaemonSurvivalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp(prefix="liveshell-detached-")) / "state"
        self.state.mkdir()
        self.addCleanup(rmtree_retry, self.state.parent)

    def _cli(self, *args: str) -> dict:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        proc = subprocess.run(
            [sys.executable, "-m", "liveshell.cli", *args],
            capture_output=True, text=True, env=env,
        )
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        return {"returncode": proc.returncode, "payload": payload, "stderr": proc.stderr}

    def test_command_survives_the_launching_process_exit(self) -> None:
        shell = available_shell()
        if shell is None:
            self.skipTest("no cmd/bash shell available")
        started = self._cli("daemon", "start", "--state-dir", str(self.state))
        self.assertEqual(started["returncode"], 0, started)
        self.assertTrue(started["payload"]["ok"], started)
        self.addCleanup(self._cli, "daemon", "stop", "--state-dir", str(self.state))

        # The daemon is a separate, detached process from this test process.
        with LiveShellClient.connect(self.state) as launcher:
            session = launcher.create_session(shell)
            handle = launcher.start_command(session.session_id, sleep_then_echo("survived", 1.0))
            command_id = handle.command_id
            self.assertNotIn(launcher.poll_command(command_id).status, TERMINAL_COMMAND_STATUSES)
        # The launcher client is now closed (its connection is gone), yet the detached
        # daemon keeps executing the command to completion.
        with LiveShellClient.connect(self.state) as observer:
            snapshot = wait_for_terminal(observer, command_id)
            self.assertEqual(snapshot.status, "completed")
            result = observer.command_result(command_id)
            self.assertIn("survived", result.stdout)

        stop = self._cli("daemon", "stop", "--state-dir", str(self.state))
        self.assertEqual(stop["returncode"], 0, stop)
        self.assertTrue(stop["payload"]["ok"], stop)
        self.assertTrue(wait_until_unreachable(self.state), "detached daemon still reachable after stop")


if __name__ == "__main__":
    unittest.main()
