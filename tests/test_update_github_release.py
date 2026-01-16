import hashlib
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

from cursor_cli_manager.update import check_for_update, perform_update


class TestUpdateGithubRelease(unittest.TestCase):
    def test_check_for_update_github_release_when_frozen(self) -> None:
        def fake_fetch(url: str, _timeout_s: float, _headers: dict) -> bytes:
            # Only the GitHub API call should happen for check_for_update.
            self.assertIn("/releases/latest", url)
            return b'{"tag_name":"v9.9.9"}'

        with patch.object(sys, "frozen", True, create=True), patch(
            "cursor_cli_manager.update.select_release_asset_name", return_value="ccm-linux-x86_64-glibc217.tar.gz"
        ):
            st = check_for_update(timeout_s=0.1, fetch=fake_fetch)
        self.assertEqual(st.method, "github_release")
        self.assertTrue(st.supported)
        self.assertTrue(st.update_available)
        self.assertEqual(st.remote_version, "9.9.9")

    def test_perform_update_installs_bundle_when_frozen(self) -> None:
        asset = "ccm-linux-x86_64-glibc217.tar.gz"
        new_bytes = b"new-binary\n"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(name="ccm/ccm")
            info.size = len(new_bytes)
            info.mode = 0o755
            tf.addfile(info, fileobj=io.BytesIO(new_bytes))
        bundle = buf.getvalue()
        sha = hashlib.sha256(bundle).hexdigest()
        checksums = f"{sha}  {asset}\n".encode("utf-8")

        def fake_fetch(url: str, _timeout_s: float, _headers: dict) -> bytes:
            if "/releases/latest" in url:
                return b'{"tag_name":"v9.9.9"}'
            if url.endswith("/" + asset):
                return bundle
            if url.endswith("/checksums.txt"):
                return checksums
            raise AssertionError(f"unexpected url: {url}")

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root_dir = base / "root"
            bin_dir = base / "bin"
            root_dir.mkdir(parents=True, exist_ok=True)
            bin_dir.mkdir(parents=True, exist_ok=True)

            with patch.dict(
                os.environ,
                {"CCM_INSTALL_DEST": str(bin_dir), "CCM_INSTALL_ROOT": str(root_dir)},
                clear=False,
            ), patch.object(sys, "frozen", True, create=True), patch(
                "cursor_cli_manager.update.shutil.which", return_value=None
            ), patch(
                "cursor_cli_manager.update.select_release_asset_name", return_value=asset
            ):
                ok, out = perform_update(timeout_s=0.1, fetch=fake_fetch)

            self.assertTrue(ok)
            self.assertIn("updated", out)
            exe = (root_dir / "current" / "ccm" / "ccm").resolve()
            self.assertTrue(exe.exists())
            self.assertEqual(exe.read_bytes(), new_bytes)
            self.assertTrue((bin_dir / "ccm").is_symlink())
            self.assertEqual((bin_dir / "ccm").resolve(), exe)


if __name__ == "__main__":
    unittest.main()

