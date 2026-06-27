from __future__ import annotations

import importlib.util

from .bash import Bash
from .cmd import Cmd
from .models import PROTOCOL_VERSION, Capability
from .powershell import PowerShell


def discover_capabilities() -> list[Capability]:
    cmd_available = Cmd.is_available()
    bash_available = Bash.is_available()
    powershell_installation = PowerShell.find_installation()
    powershell_available = powershell_installation is not None
    pythonnet_available = importlib.util.find_spec("pythonnet") is not None

    capabilities = [
        Capability("protocol.version", True, {"version": PROTOCOL_VERSION}),
        Capability("session.persistent_env", True),
        Capability("command.blocking", True),
        Capability("command.async", True),
        Capability("command.poll", True),
        Capability("command.timeout", True),
        Capability("command.exit_code.native", True),
        Capability("daemon.protocol", True, {"transport": "stdio", "network": False}),
        Capability("command.events.replay", True),
        Capability("command.events.chunking", True),
        Capability("command.stdout.streaming", True, {"scope": "process_backed"}),
        Capability("command.stderr.separate", True),
        Capability(
            "command.events.streaming.best_effort",
            True,
            {
                "process_backed_stdout": True,
                "hosted_powershell_stdout": False,
                "process_backed_stderr": "captured_at_completion",
                "hosted_powershell_stderr": "captured_at_completion",
                "hosted_powershell_native_exit_code": True,
                "stderr_separation": True,
            },
        ),
        Capability(
            "command.cancel.best_effort",
            True,
            {"strategy": "terminate_session_when_needed"},
        ),
        Capability("shell.cmd.available", cmd_available),
        Capability("shell.bash.available", bash_available),
        Capability(
            "shell.powershell.available",
            powershell_available,
            {
                "edition": powershell_installation.edition if powershell_installation else None,
                "home": str(powershell_installation.home) if powershell_installation else None,
            },
        ),
        Capability(
            "shell.powershell.hosted",
            powershell_available and pythonnet_available,
            {
                "pythonnet_available": pythonnet_available,
                "requires_pythonnet": True,
            },
        ),
    ]
    return capabilities
