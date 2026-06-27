from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from liveshell.powershell import AsyncPowerShell, PowerShell, PowerShellResult  # noqa: E402


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

    def test_async_powershell_uses_same_discovery(self) -> None:
        installation = AsyncPowerShell.find_installation()

        self.assertTrue(installation is None or installation.home.exists())

    def test_powershell_string_quoting_escapes_single_quotes(self) -> None:
        self.assertEqual(
            PowerShell._quote_string(r"C:\Temp\Project's Folder"),
            r"'C:\Temp\Project''s Folder'",
        )

    def test_powershell_result_exposes_stdout_stderr_and_check(self) -> None:
        result = PowerShellResult(
            command="Write-Error broken",
            output=["ok"],
            errors=["broken"],
            had_errors=True,
            exit_code=1,
        )

        self.assertEqual(result.stdout, "ok")
        self.assertEqual(result.stderr, "broken")
        with self.assertRaisesRegex(RuntimeError, "broken"):
            result.check_returncode()

    def test_powershell_exit_code_prefers_native_last_exit_code(self) -> None:
        self.assertEqual(
            PowerShell._command_exit_code(had_errors=False, last_exit_code=7),
            7,
        )
        self.assertEqual(
            PowerShell._command_exit_code(had_errors=True, last_exit_code=7),
            7,
        )
        self.assertEqual(
            PowerShell._command_exit_code(had_errors=True, last_exit_code=0),
            1,
        )
        self.assertEqual(
            PowerShell._command_exit_code(had_errors=False, last_exit_code=None),
            0,
        )

    def test_powershell_last_exit_code_coercion(self) -> None:
        self.assertEqual(PowerShell._coerce_last_exit_code("7"), 7)
        self.assertEqual(PowerShell._coerce_last_exit_code("-1"), -1)
        self.assertIsNone(PowerShell._coerce_last_exit_code(""))
        self.assertIsNone(PowerShell._coerce_last_exit_code("not-a-number"))


if __name__ == "__main__":
    unittest.main()
