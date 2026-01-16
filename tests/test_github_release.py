import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_cli_manager.github_release import (
    ReleaseInfo,
    download_and_install_release_binary,
    fetch_latest_release,
    is_version_newer,
    parse_checksums_txt,
    select_release_asset_name,
    split_repo,
)


class TestGithubReleaseHelpers(unittest.TestCase):
    def test_split_repo(self) -> None:
        self.assertEqual(split_repo("a/b"), ("a", "b"))
        with self.assertRaises(ValueError):
            split_repo("")
        with self.assertRaises(ValueError):
            split_repo("nope")

    def test_is_version_newer(self) -> None:
        self.assertEqual(is_version_newer("0.5.6", "0.5.5"), True)
        self.assertEqual(is_version_newer("v0.5.6", "0.5.6"), False)
        self.assertEqual(is_version_newer("0.5.6", "0.5.6"), False)
        self.assertIsNone(is_version_newer("not-a-version", "0.5.6"))

    def test_parse_checksums_txt(self) -> None:
        txt = """
        # comment
        deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef  ccm-linux-x86_64-glibc217
        abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd  other
        """
        m = parse_checksums_txt(txt)
        self.assertEqual(
            m["ccm-linux-x86_64-glibc217"],
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        )

    def test_select_release_asset_name_linux_glibc(self) -> None:
        with patch("cursor_cli_manager.github_release._glibc_version", return_value=(2, 17)):
            self.assertEqual(select_release_asset_name(system="Linux", machine="x86_64"), "ccm-linux-x86_64-glibc217")
        with patch("cursor_cli_manager.github_release._glibc_version", return_value=(2, 16)):
            with self.assertRaises(RuntimeError):
                select_release_asset_name(system="Linux", machine="x86_64")

    def test_select_release_asset_name_macos(self) -> None:
        self.assertEqual(select_release_asset_name(system="Darwin", machine="x86_64"), "ccm-macos-x86_64")
        self.assertEqual(select_release_asset_name(system="Darwin", machine="arm64"), "ccm-macos-arm64")


class TestGithubReleaseFetchAndInstall(unittest.TestCase):
    def test_fetch_latest_release_parses_tag(self) -> None:
        def fake_fetch(url: str, _timeout_s: float, _headers: dict) -> bytes:
            self.assertIn("/releases/latest", url)
            return b'{"tag_name":"v0.5.7"}'

        rel = fetch_latest_release("baaaaaaaka/cursor_cli_manager", timeout_s=0.1, fetch=fake_fetch)
        self.assertEqual(rel, ReleaseInfo(tag="v0.5.7", version="0.5.7"))

    def test_download_and_install_release_binary_verifies_checksum(self) -> None:
        asset = "ccm-linux-x86_64-glibc217"
        content = b"hello\n"
        sha = hashlib.sha256(content).hexdigest()
        checksums = f"{sha}  {asset}\n"

        def fake_fetch(url: str, _timeout_s: float, _headers: dict) -> bytes:
            if url.endswith("/" + asset):
                return content
            if url.endswith("/checksums.txt"):
                return checksums.encode("utf-8")
            raise AssertionError(f"unexpected url: {url}")

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "ccm"
            download_and_install_release_binary(
                repo="baaaaaaaka/cursor_cli_manager",
                tag="v0.5.7",
                asset_name=asset,
                dest_path=dest,
                timeout_s=0.1,
                fetch=fake_fetch,
                verify_checksums=True,
            )
            self.assertTrue(dest.exists())
            self.assertEqual(dest.read_bytes(), content)
            mode = dest.stat().st_mode
            self.assertTrue(mode & stat.S_IXUSR)

    def test_download_and_install_release_binary_fails_on_checksum_mismatch(self) -> None:
        asset = "ccm-linux-x86_64-glibc217"
        content = b"hello\n"
        checksums = f"{'0'*64}  {asset}\n"

        def fake_fetch(url: str, _timeout_s: float, _headers: dict) -> bytes:
            if url.endswith("/" + asset):
                return content
            if url.endswith("/checksums.txt"):
                return checksums.encode("utf-8")
            raise AssertionError(f"unexpected url: {url}")

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "ccm"
            with self.assertRaises(RuntimeError):
                download_and_install_release_binary(
                    repo="baaaaaaaka/cursor_cli_manager",
                    tag="v0.5.7",
                    asset_name=asset,
                    dest_path=dest,
                    timeout_s=0.1,
                    fetch=fake_fetch,
                    verify_checksums=True,
                )
            self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()

