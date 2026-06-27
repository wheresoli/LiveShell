from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from liveshell import Bash, Cmd


def show_backend(name: str, session_type, setup: str, check: str) -> None:
    if not session_type.is_available():
        print(f"{name}: unavailable")
        return

    with session_type() as session:
        session.run(setup)
        print(f"{name}: {session.text(check)}")


show_backend("cmd", Cmd, "set LIVESHELL_TEST_VALUE=40", "echo %LIVESHELL_TEST_VALUE%")
show_backend("bash", Bash, "LIVESHELL_TEST_VALUE=40", "printf '%s' \"$LIVESHELL_TEST_VALUE\"")
