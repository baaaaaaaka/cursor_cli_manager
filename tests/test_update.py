import json
import tempfile
import unittest
from pathlib import Path

from cursor_cli_manager.update import build_vcs_requirement, check_for_update, perform_update, read_pep610_install_info


class _FakeRun:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def __call__(self, cmd, timeout_s):
        self.calls.append((tuple(cmd), timeout_s))
        key = tuple(cmd)
        if key not in self.mapping:
            return (1, "", "unexpected command")
        return self.mapping[key]


class TestUpdatePep610(unittest.TestCase):
    def _make_pep610_layout(self, base: Path, *, direct_url: dict) -> Path:
        # Simulate site-packages layout:
        #   base/cursor_cli_manager/ (package_dir)
        #   base/cursor_cli_manager-0.1.0.dist-info/direct_url.json
        pkg = base / "cursor_cli_manager"
        pkg.mkdir(parents=True, exist_ok=True)
        dist = base / "cursor_cli_manager-0.1.0.dist-info"
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "direct_url.json").write_text(json.dumps(direct_url), encoding="utf-8")
        return pkg

    def test_read_pep610_install_info_parses_git_commit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pkg = self._make_pep610_layout(
                Path(td),
                direct_url={
                    "url": "https://example.com/repo.git",
                    "vcs_info": {"vcs": "git", "commit_id": "abc123", "requested_revision": "main"},
                },
            )
            info = read_pep610_install_info(package_dir=pkg)
            assert info is not None
            self.assertEqual(info.url, "https://example.com/repo.git")
            self.assertEqual(info.commit_id, "abc123")
            self.assertEqual(info.requested_revision, "main")

    def test_check_for_update_compares_remote_tip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pkg = self._make_pep610_layout(
                Path(td),
                direct_url={
                    "url": "https://example.com/repo.git",
                    "vcs_info": {"vcs": "git", "commit_id": "old", "requested_revision": "main"},
                },
            )
            run = _FakeRun(
                {
                    ("git", "ls-remote", "https://example.com/repo.git", "main"): (0, "deadbeef\trefs/heads/main\n", ""),
                }
            )
            st = check_for_update(package_dir=pkg, run=run, timeout_s=0.1)
            self.assertTrue(st.supported)
            self.assertTrue(st.update_available)
            self.assertEqual(st.installed_commit, "old")
            self.assertEqual(st.remote_commit, "deadbeef")

    def test_build_vcs_requirement_supports_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pkg = self._make_pep610_layout(
                Path(td),
                direct_url={
                    "url": "https://example.com/repo.git",
                    "subdirectory": "pkg",
                    "vcs_info": {"vcs": "git", "commit_id": "abc", "requested_revision": "main"},
                },
            )
            info = read_pep610_install_info(package_dir=pkg)
            assert info is not None
            req = build_vcs_requirement(info)
            self.assertIn("git+https://example.com/repo.git@main", req)
            self.assertIn("subdirectory=pkg", req)

    def test_perform_update_runs_pip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pkg = self._make_pep610_layout(
                Path(td),
                direct_url={
                    "url": "https://example.com/repo.git",
                    "vcs_info": {"vcs": "git", "commit_id": "abc", "requested_revision": "main"},
                },
            )
            expected_req = "git+https://example.com/repo.git@main"
            run = _FakeRun(
                {
                    ("python", "-m", "pip", "install", "--upgrade", "--no-deps", "--force-reinstall", expected_req): (0, "ok\n", ""),
                }
            )
            ok, out = perform_update(package_dir=pkg, python="python", run=run, timeout_s=0.1)
            self.assertTrue(ok)
            self.assertIn("ok", out)


if __name__ == "__main__":
    unittest.main()

