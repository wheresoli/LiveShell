# LiveShell Release

## Gates

Run from the repository root:

```powershell
python -m unittest discover -s tests
python -m build
```

When practical, verify the wheel in a temporary virtual environment:

```powershell
python -m venv C:\tmp\liveshell-release-venv
C:\tmp\liveshell-release-venv\Scripts\python.exe -m pip install --upgrade pip
C:\tmp\liveshell-release-venv\Scripts\python.exe -m pip install dist\liveshell-*.whl
C:\tmp\liveshell-release-venv\Scripts\python.exe -c "import liveshell; print(liveshell.PROTOCOL_VERSION)"
```

## Version And Tag

Update `pyproject.toml` only for an actual release. For the current package version `0.2.0`, use:

```powershell
git tag v0.2.0
```

## Install Commands

Local editable development:

```powershell
pip install -e ".[dev]"
```

Optional hosted PowerShell support:

```powershell
pip install ".[powershell]"
```

Git tag install:

```powershell
pip install "liveshell @ git+https://github.com/wheresoli/LiveShell.git@v0.2.0"
```
