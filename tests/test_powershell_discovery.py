from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from liveshell.powershell import PowerShell  # noqa: E402


class PowerShellDiscoveryTests(unittest.TestCase):
    def test_version_sort_prefers_highest_stable_version(self) -> None:
        names = ["7", "7-preview", "7.5.1", "10", "6.2"]
        paths = [Path(name) for name in names]

        sorted_names = [
            path.name
            for path in sorted(paths, key=PowerShell._version_sort_key, reverse=True)
        ]

        self.assertEqual(sorted_names, ["10", "7.5.1", "7", "7-preview", "6.2"])

    def test_find_installation_is_safe_probe(self) -> None:
        installation = PowerShell.find_installation()

        self.assertTrue(installation is None or installation.home.exists())


if __name__ == "__main__":
    unittest.main()
