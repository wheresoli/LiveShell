from __future__ import annotations

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from liveshell import AsyncBash, AsyncCmd  # noqa: E402


async def show_backend(name: str, session_type, setup: str, check: str) -> None:
    if not session_type.is_available():
        print(f"{name}: unavailable")
        return

    async with session_type() as session:
        await session.run(setup)
        print(f"{name}: {await session.text(check)}")


async def main() -> None:
    await show_backend(
        "cmd",
        AsyncCmd,
        "set LIVESHELL_TEST_VALUE=40",
        "echo %LIVESHELL_TEST_VALUE%",
    )
    await show_backend(
        "bash",
        AsyncBash,
        "LIVESHELL_TEST_VALUE=40",
        "printf '%s' \"$LIVESHELL_TEST_VALUE\"",
    )


if __name__ == "__main__":
    asyncio.run(main())
