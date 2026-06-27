from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VENV = ROOT / ".venv"


def run(command: list[object]) -> None:
    printable = " ".join(str(part) for part in command)
    print(f"+ {printable}", flush=True)
    subprocess.run([str(part) for part in command], cwd=ROOT, check=True)


def resolve_venv(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


def venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def assert_recreatable(path: Path) -> None:
    root = ROOT.resolve()
    resolved = path.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise SystemExit(f"Refusing to remove venv outside the project: {resolved}")


def activation_hint(venv: Path) -> str:
    try:
        display_path = venv.relative_to(ROOT)
    except ValueError:
        display_path = venv

    if os.name == "nt":
        return rf".\{display_path}\Scripts\Activate.ps1"
    return f"source {display_path}/bin/activate"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or update a local LiveShell test venv.")
    parser.add_argument(
        "--venv",
        type=Path,
        default=DEFAULT_VENV,
        help="Virtual environment path. Defaults to .venv in the project root.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable used to create the venv. Defaults to the current Python.",
    )
    parser.add_argument(
        "--runtime-only",
        action="store_true",
        help="Install LiveShell without development/test extras.",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Remove the existing project-local venv before creating it again.",
    )
    args = parser.parse_args(argv)

    if sys.version_info < (3, 10):
        raise SystemExit("LiveShell requires Python 3.10 or newer.")

    venv = resolve_venv(args.venv)
    python = venv_python(venv)

    if args.recreate and venv.exists():
        assert_recreatable(venv)
        shutil.rmtree(venv)

    if not python.exists():
        run([args.python, "-m", "venv", venv])

    target = ".[dev]" if not args.runtime_only else "."
    run([python, "-m", "pip", "install", "--upgrade", "pip"])
    run([python, "-m", "pip", "install", "-e", target])

    print()
    print(f"Ready: {venv}")
    print(f"Activate with: {activation_hint(venv)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
