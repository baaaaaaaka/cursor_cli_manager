import unittest
from pathlib import Path


class TestReleaseAssetsConsistency(unittest.TestCase):
    def test_install_script_mentions_expected_asset_names(self) -> None:
        root = Path(__file__).resolve().parent.parent
        txt = (root / "scripts" / "install_ccm.sh").read_text(encoding="utf-8")
        for name in (
            "ccm-linux-x86_64-glibc217.tar.gz",
            "ccm-macos-x86_64.tar.gz",
            "ccm-macos-arm64.tar.gz",
        ):
            self.assertIn(name, txt)
        # Ensure the installer can infer the effective tag from the download redirect.
        self.assertIn("url_effective", txt)

    def test_release_workflow_mentions_expected_asset_names(self) -> None:
        root = Path(__file__).resolve().parent.parent
        txt = (root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        for name in (
            "ccm-linux-x86_64-glibc217.tar.gz",
            "ccm-macos-x86_64.tar.gz",
            "ccm-macos-arm64.tar.gz",
            "checksums.txt",
        ):
            self.assertIn(name, txt)
        # CA bundle must be included in binary releases for SSL fallback.
        self.assertIn("certifi", txt)
        self.assertIn("--collect-data certifi", txt)

    def test_linux_build_script_mentions_asset_and_terminfo(self) -> None:
        root = Path(__file__).resolve().parent.parent
        txt = (root / "scripts" / "build_linux_binary_docker.sh").read_text(encoding="utf-8")
        self.assertIn("ccm-linux-x86_64-glibc217.tar.gz", txt)
        self.assertIn("--add-data", txt)
        self.assertIn("/opt/terminfo:terminfo", txt)
        self.assertIn("--collect-data certifi", txt)
        self.assertIn("tar -C", txt)

    def test_linux_builder_installs_certifi(self) -> None:
        root = Path(__file__).resolve().parent.parent
        txt = (root / "docker" / "linux-builder" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("certifi", txt)


if __name__ == "__main__":
    unittest.main()

