from __future__ import annotations

import argparse
import importlib.util
import platform
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from liveshell.powershell import PowerShell  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose the local LiveShell environment.")
    parser.add_argument(
        "--preload",
        action="store_true",
        help="Also try loading the PowerShell engine through pythonnet.",
    )
    args = parser.parse_args()

    print(f"Python: {sys.executable}")
    print(f"Python version: {sys.version.split()[0]}")
    print(f"Platform: {platform.platform()}")

    pythonnet_found = importlib.util.find_spec("pythonnet") is not None
    print(f"pythonnet installed: {pythonnet_found}")
    if not pythonnet_found:
        print('Install with: python -m pip install ".[powershell]"')

    installation = PowerShell.find_installation()
    print(f"PowerShell available: {installation is not None}")
    print(f"PowerShell installation: {installation}")

    if args.preload:
        print("Preloading PowerShell...")
        PowerShell.preload()
        print(f"PowerShell preloaded: {PowerShell.is_preloaded()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
