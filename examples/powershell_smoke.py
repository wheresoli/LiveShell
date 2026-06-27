from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from liveshell.powershell import PowerShell

ps = PowerShell()

print(ps.text("$x = 40; $x + 2"))
print(ps.text("$x"))  # proves state persists

items = ps.run("Get-ChildItem | Select-Object -First 3")
for item in items:
    print(item)

ps.close()
