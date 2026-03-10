import builtins
import hashlib
import os
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cursor_cli_manager.github_release as gr
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

    def test_atomic_symlink_ignores_self_reference_for_existing_link(self) -> None:
        """
        If an existing symlink already resolves to the target, we must not
        treat it as a self-referential link even when readlink fails.
        """
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            target = base / "target"
            target.write_text("x", encoding="utf-8")
            link = base / "link"
            link.symlink_to(target)

            with patch("cursor_cli_manager.github_release.os.readlink", side_effect=OSError("boom")):
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

    def test_bundled_cafile_prefers_certifi_when_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cert = Path(td) / "certifi.pem"
            cert.write_text("pem", encoding="utf-8")
            fake_certifi = SimpleNamespace(where=lambda: str(cert))

            with patch.object(gr, "is_frozen_binary", return_value=True), patch.dict(
                os.environ, {}, clear=True
            ), patch.dict(sys.modules, {"certifi": fake_certifi}, clear=False):
                self.assertEqual(gr._bundled_cafile(), str(cert))

    def test_bundled_cafile_falls_back_to_meipass_then_executable(self) -> None:
        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "certifi":
                raise ImportError("missing certifi")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            meipass = root / "bundle"
            meipass.mkdir(parents=True, exist_ok=True)
            meipass_cert = meipass / "cacert.pem"
            meipass_cert.write_text("pem", encoding="utf-8")
            exe = root / "bin" / "ccm"
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_text("", encoding="utf-8")
            exe_cert = exe.parent / "cacert.pem"
            exe_cert.write_text("pem", encoding="utf-8")

            with patch.object(gr, "is_frozen_binary", return_value=True), patch.dict(
                os.environ, {}, clear=True
            ), patch(
                "builtins.__import__", side_effect=fake_import
            ), patch.object(
                gr.sys, "_MEIPASS", str(meipass), create=True
            ), patch.object(
                gr.sys, "executable", str(exe)
            ):
                self.assertEqual(gr._bundled_cafile(), str(meipass_cert))

            meipass_cert.unlink()

            with patch.object(gr, "is_frozen_binary", return_value=True), patch.dict(
                os.environ, {}, clear=True
            ), patch(
                "builtins.__import__", side_effect=fake_import
            ), patch.object(
                gr.sys, "_MEIPASS", str(meipass), create=True
            ), patch.object(
                gr.sys, "executable", str(exe)
            ):
                self.assertEqual(gr._bundled_cafile(), str(exe_cert))

    def test_detect_linux_ncurses_variant_honors_env_override(self) -> None:
        with patch("cursor_cli_manager.github_release.platform.system", return_value="Linux"), patch(
            "cursor_cli_manager.github_release._can_load_shared_lib", return_value=False
        ):
            self.assertIsNone(gr.detect_linux_ncurses_variant(env={gr.ENV_CCM_NCURSES_VARIANT: "common"}))
            self.assertEqual(gr.detect_linux_ncurses_variant(env={gr.ENV_CCM_NCURSES_VARIANT: "6"}), "nc6")
            self.assertEqual(gr.detect_linux_ncurses_variant(env={gr.ENV_CCM_NCURSES_VARIANT: "nc5"}), "nc5")

    def test_detect_frozen_binary_ncurses_variant_from_internal_libs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            exe = root / "bin" / "ccm"
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_text("", encoding="utf-8")
            internal = exe.parent / "_internal"
            internal.mkdir(parents=True, exist_ok=True)
            (internal / "libtinfo.so.5").write_text("", encoding="utf-8")

            with patch.object(gr, "is_frozen_binary", return_value=True), patch(
                "cursor_cli_manager.github_release.platform.system", return_value="Linux"
            ), patch.object(gr.sys, "executable", str(exe)):
                self.assertEqual(gr.detect_frozen_binary_ncurses_variant(), "nc5")


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
            if os.name != "nt":
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

    def test_download_and_install_release_bundle_rejects_non_tar_asset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError):
                download_and_install_release_bundle(
                    repo="baaaaaaaka/cursor_cli_manager",
                    tag="v0.5.7",
                    asset_name="ccm-macos-arm64.zip",
                    install_root=Path(td) / "root",
                    bin_dir=Path(td) / "bin",
                    timeout_s=0.1,
                    fetch=lambda *_a, **_k: b"",
                    verify_checksums=False,
                )

    def test_download_and_install_release_bundle_rejects_bin_dir_inside_bundle(self) -> None:
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

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            install_root = base / "root"
            bad_bin_dir = install_root / "current" / "ccm"

            with self.assertRaises(RuntimeError) as ctx:
                download_and_install_release_bundle(
                    repo="baaaaaaaka/cursor_cli_manager",
                    tag="v0.5.7",
                    asset_name=asset,
                    install_root=install_root,
                    bin_dir=bad_bin_dir,
                    timeout_s=0.1,
                    fetch=lambda *_a, **_k: data,
                    verify_checksums=False,
                )

            self.assertIn("refusing to install", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
