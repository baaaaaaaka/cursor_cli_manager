import unittest
from pathlib import Path


class TestReleaseAssetsConsistency(unittest.TestCase):
    def test_install_script_mentions_expected_asset_names(self) -> None:
        root = Path(__file__).resolve().parent.parent
        txt = (root / "scripts" / "install_ccm.sh").read_text(encoding="utf-8")
        for name in ("ccm-linux-x86_64-glibc217", "ccm-macos-x86_64", "ccm-macos-arm64"):
            self.assertIn(name, txt)

    def test_release_workflow_mentions_expected_asset_names(self) -> None:
        root = Path(__file__).resolve().parent.parent
        txt = (root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        for name in ("ccm-linux-x86_64-glibc217", "ccm-macos-x86_64", "ccm-macos-arm64", "checksums.txt"):
            self.assertIn(name, txt)


if __name__ == "__main__":
    unittest.main()

