import hashlib
import os
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_cli_manager.github_release import (
    _atomic_symlink,
    LINUX_ASSET_COMMON,
    LINUX_ASSET_NC5,
    LINUX_ASSET_NC6,
    ReleaseInfo,
    download_and_install_release_bundle,
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
        deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef  ccm-linux-x86_64-glibc217.tar.gz
        abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd  other
        """
        m = parse_checksums_txt(txt)
        self.assertEqual(
            m[LINUX_ASSET_COMMON],
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        )

    def test_select_release_asset_name_linux_glibc(self) -> None:
        with patch("cursor_cli_manager.github_release._glibc_version", return_value=(2, 17)), patch(
            "cursor_cli_manager.github_release.detect_linux_ncurses_variant", return_value="nc6"
        ):
            self.assertEqual(
                select_release_asset_name(system="Linux", machine="x86_64"),
                LINUX_ASSET_NC6,
            )
        with patch("cursor_cli_manager.github_release._glibc_version", return_value=(2, 16)):
            with self.assertRaises(RuntimeError):
                select_release_asset_name(system="Linux", machine="x86_64")

    def test_select_release_asset_name_linux_variant_override(self) -> None:
        with patch("cursor_cli_manager.github_release._glibc_version", return_value=(2, 17)):
            self.assertEqual(
                select_release_asset_name(system="Linux", machine="x86_64", linux_variant="nc5"),
                LINUX_ASSET_NC5,
            )
            self.assertEqual(
                select_release_asset_name(system="Linux", machine="x86_64", linux_variant="common"),
                LINUX_ASSET_COMMON,
            )

    def test_atomic_symlink_noop_when_already_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            target = base / "target"
            target.write_text("x", encoding="utf-8")
            link = base / "link"
            link.symlink_to(target)

            _atomic_symlink(target, link)
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), target.resolve())

    def test_atomic_symlink_refuses_self_reference(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            link = base / "link"
            with self.assertRaises(RuntimeError):
                _atomic_symlink(link, link)

    def test_select_release_asset_name_macos(self) -> None:
        self.assertEqual(select_release_asset_name(system="Darwin", machine="x86_64"), "ccm-macos-x86_64.tar.gz")
        self.assertEqual(select_release_asset_name(system="Darwin", machine="arm64"), "ccm-macos-arm64.tar.gz")


class TestGithubReleaseFetchAndInstall(unittest.TestCase):
    def test_fetch_latest_release_parses_tag(self) -> None:
        def fake_fetch(url: str, _timeout_s: float, _headers: dict) -> bytes:
            self.assertIn("/releases/latest", url)
            return b'{"tag_name":"v0.5.7"}'

        rel = fetch_latest_release("baaaaaaaka/cursor_cli_manager", timeout_s=0.1, fetch=fake_fetch)
        self.assertEqual(rel, ReleaseInfo(tag="v0.5.7", version="0.5.7"))

    def test_download_and_install_release_bundle_verifies_checksum(self) -> None:
        asset = LINUX_ASSET_COMMON
        payload = b"hello\n"
        import io

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(name="ccm/ccm")
            info.size = len(payload)
            info.mode = 0o755
            tf.addfile(info, fileobj=io.BytesIO(payload))
        data = buf.getvalue()

        sha = hashlib.sha256(data).hexdigest()
        checksums = f"{sha}  {asset}\n"

        def fake_fetch(url: str, _timeout_s: float, _headers: dict) -> bytes:
            if url.endswith("/" + asset):
                return data
            if url.endswith("/checksums.txt"):
                return checksums.encode("utf-8")
            raise AssertionError(f"unexpected url: {url}")

        with tempfile.TemporaryDirectory() as td2:
            base = Path(td2)
            install_root = base / "root"
            bin_dir = base / "bin"
            download_and_install_release_bundle(
                repo="baaaaaaaka/cursor_cli_manager",
                tag="v0.5.7",
                asset_name=asset,
                install_root=install_root,
                bin_dir=bin_dir,
                timeout_s=0.1,
                fetch=fake_fetch,
                verify_checksums=True,
            )
            exe = (install_root / "current" / "ccm" / "ccm").resolve()
            self.assertTrue(exe.exists())
            self.assertEqual(exe.read_bytes(), payload)
            self.assertTrue(exe.stat().st_mode & stat.S_IXUSR)
            self.assertTrue((bin_dir / "ccm").is_symlink())
            self.assertEqual((bin_dir / "ccm").resolve(), exe)
            self.assertTrue((bin_dir / "cursor-cli-manager").is_symlink())
            self.assertEqual((bin_dir / "cursor-cli-manager").resolve(), exe)

    def test_download_and_install_release_bundle_fails_on_checksum_mismatch(self) -> None:
        asset = LINUX_ASSET_COMMON
        payload = b"hello\n"
        import io

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(name="ccm/ccm")
            info.size = len(payload)
            info.mode = 0o755
            tf.addfile(info, fileobj=io.BytesIO(payload))
        data = buf.getvalue()
        checksums = f"{'0'*64}  {asset}\n"

        def fake_fetch(url: str, _timeout_s: float, _headers: dict) -> bytes:
            if url.endswith("/" + asset):
                return data
            if url.endswith("/checksums.txt"):
                return checksums.encode("utf-8")
            raise AssertionError(f"unexpected url: {url}")

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            install_root = base / "root"
            bin_dir = base / "bin"
            with self.assertRaises(RuntimeError):
                download_and_install_release_bundle(
                    repo="baaaaaaaka/cursor_cli_manager",
                    tag="v0.5.7",
                    asset_name=asset,
                    install_root=install_root,
                    bin_dir=bin_dir,
                    timeout_s=0.1,
                    fetch=fake_fetch,
                    verify_checksums=True,
                )
            self.assertFalse((bin_dir / "ccm").exists())
            self.assertFalse((install_root / "current").exists())


if __name__ == "__main__":
    unittest.main()

