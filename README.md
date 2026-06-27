# LiveShell

LiveShell provides persistent command runtime sessions from Python, using hosted runtimes where available and process-backed sessions where needed.

The PowerShell backend currently hosts the PowerShell engine directly through `pythonnet` instead of shelling out through `subprocess.Popen`.

## Setup

Create or update the local development environment:

```powershell
py -3.10 scripts\setup_venv.py
.\.venv\Scripts\Activate.ps1
```

If `python` is already on your PATH, `python scripts\setup_venv.py` works too.

On macOS/Linux:

```bash
python3 scripts/setup_venv.py
source .venv/bin/activate
```

This creates `.venv/`, installs LiveShell in editable mode, and includes the test dependencies. The virtual environment is ignored by git.

To rebuild it from scratch:

```powershell
python scripts/setup_venv.py --recreate
```

For runtime dependencies only:

```powershell
python scripts/setup_venv.py --runtime-only
```

## Diagnose PowerShell

Discovery does not require `pythonnet`:

```powershell
python scripts/diagnose.py
```

To also try loading the PowerShell engine:

```powershell
python scripts/diagnose.py --preload
```

## Smoke Test

```powershell
python examples/powershell_smoke.py
python examples/process_smoke.py
```

The first two lines should prove session state persists:

```text
42
40
```

## Notes

PowerShell Core installations use `pwsh.runtimeconfig.json` and load with CoreCLR. Windows PowerShell 5.1 uses the .NET Framework GAC assembly and loads with `netfx`. A single Python process can only host one .NET runtime mode.

Process-backed sessions such as `Cmd` and `Bash` keep one child process alive and send commands through it. They preserve shell state, but they are not full terminal/PTY emulators yet.

## Tests

Run the no-extra-dependencies tests with:

```powershell
python -m unittest discover -s tests
```

After installing development dependencies, you can also run:

```powershell
pytest
```
