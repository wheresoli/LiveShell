from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from liveshell.capabilities import discover_capabilities  # noqa: E402


class CapabilityTests(unittest.TestCase):
    def test_discovery_returns_json_serializable_capabilities(self) -> None:
        capabilities = discover_capabilities()

        payload = [capability.to_dict() for capability in capabilities]
        encoded = json.dumps(payload)

        self.assertIn("session.persistent_env", {item["name"] for item in payload})
        self.assertIn(
            "command.events.streaming.best_effort",
            {item["name"] for item in payload},
        )
        streaming = next(
            item
            for item in payload
            if item["name"] == "command.events.streaming.best_effort"
        )
        self.assertTrue(streaming["details"]["stderr_separation"])
        self.assertEqual(
            streaming["details"]["process_backed_stderr"],
            "captured_at_completion",
        )
        self.assertEqual(
            streaming["details"]["hosted_powershell_stderr"],
            "captured_at_completion",
        )
        self.assertTrue(streaming["details"]["hosted_powershell_native_exit_code"])
        self.assertIsInstance(encoded, str)

    def test_daemon_protocol_advertises_both_transports(self) -> None:
        payload = [capability.to_dict() for capability in discover_capabilities()]
        daemon_protocol = next(
            item for item in payload if item["name"] == "daemon.protocol"
        )
        details = daemon_protocol["details"]
        # The package ships both a stdio and a loopback-socket transport
        # (serve_socket / daemon serve / LiveShellClient.connect), so discovery
        # must advertise both rather than claiming stdio-only.
        self.assertEqual(details["transports"], ["stdio", "socket"])
        self.assertEqual(details["socket_scope"], "loopback")
        # There is still no remote/auto-started network server.
        self.assertFalse(details["remote_network"])

    def test_exit_code_capability_distinguishes_native_from_sentinel(self) -> None:
        payload = [capability.to_dict() for capability in discover_capabilities()]
        names = {item["name"] for item in payload}
        # The misleading unconditional `command.exit_code.native` is gone; the
        # capability now states the mechanism per backend.
        self.assertNotIn("command.exit_code.native", names)
        exit_code = next(item for item in payload if item["name"] == "command.exit_code")
        self.assertTrue(exit_code["available"])
        details = exit_code["details"]
        # cmd/bash scrape the exit status from a stdout sentinel; only hosted
        # PowerShell reads it natively in-process.
        self.assertEqual(details["cmd"], "sentinel_parsed")
        self.assertEqual(details["bash"], "sentinel_parsed")
        self.assertEqual(details["powershell_hosted"], "native")


if __name__ == "__main__":
    unittest.main()
