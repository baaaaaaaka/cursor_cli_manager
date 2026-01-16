import hashlib
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
            "cursor_cli_manager.update.select_release_asset_name", return_value="ccm-linux-x86_64-glibc217"
        ):
            st = check_for_update(timeout_s=0.1, fetch=fake_fetch)
        self.assertEqual(st.method, "github_release")
        self.assertTrue(st.supported)
        self.assertTrue(st.update_available)
        self.assertEqual(st.remote_version, "9.9.9")

    def test_perform_update_replaces_executable_when_frozen(self) -> None:
        asset = "ccm-linux-x86_64-glibc217"
        new_bytes = b"new-binary\n"
        sha = hashlib.sha256(new_bytes).hexdigest()
        checksums = f"{sha}  {asset}\n".encode("utf-8")

        def fake_fetch(url: str, _timeout_s: float, _headers: dict) -> bytes:
            if "/releases/latest" in url:
                return b'{"tag_name":"v9.9.9"}'
            if url.endswith("/" + asset):
                return new_bytes
            if url.endswith("/checksums.txt"):
                return checksums
            raise AssertionError(f"unexpected url: {url}")

        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / "ccm"
            exe.write_bytes(b"old\n")

            with patch.object(sys, "frozen", True, create=True), patch.object(
                sys, "executable", str(exe), create=True
            ), patch("cursor_cli_manager.update.select_release_asset_name", return_value=asset):
                ok, out = perform_update(timeout_s=0.1, fetch=fake_fetch)

            self.assertTrue(ok)
            self.assertIn("updated", out)
            self.assertEqual(exe.read_bytes(), new_bytes)


if __name__ == "__main__":
    unittest.main()

