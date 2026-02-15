import os
import shutil
import sys
import unittest
from pathlib import Path

from cursor_cli_manager import windows_deps as wd


@unittest.skipUnless(sys.platform.startswith("win"), "Windows-only integration test.")
@unittest.skipUnless(
    os.environ.get("CCM_WINDOWS_DEPS_INTEGRATION") == "1",
    "CCM_WINDOWS_DEPS_INTEGRATION not enabled.",
)
class TestWindowsDepsIntegration(unittest.TestCase):
    def test_ensure_windows_deps_real(self) -> None:
        wd.ensure_windows_deps()

        import curses  # noqa: F401

        rg = shutil.which("rg") or shutil.which("rg.exe")
        if rg is None:
            rg_path = wd._default_windows_bin_dir() / "rg.exe"
            if not rg_path.exists():
                self.skipTest("ripgrep download failed (network/rate-limit); skipping")
            rg = str(rg_path)
        self.assertTrue(Path(rg).exists())
