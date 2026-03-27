import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

from cursor_cli_manager.agent_paths import CursorAgentDirs
from cursor_cli_manager.cursor_agent_canary import run_cursor_agent_patch_canary
from cursor_cli_manager.cursor_agent_install import (
    CursorAgentInstallResult,
    CursorAgentInstallSpec,
    InstallerMetadata,
)


class TestCursorAgentCanary(unittest.TestCase):
    def _spec(self, *, version: str, install_root: Path, bin_dir: Path) -> CursorAgentInstallSpec:
        return CursorAgentInstallSpec(
            version=version,
            system="linux",
            arch="x64",
            archive_kind="tar.gz",
            download_url=f"https://downloads.cursor.com/lab/{version}/linux/x64/agent-cli-package.tar.gz",
            install_root=install_root,
            bin_dir=bin_dir,
        )

    def test_canary_retries_transient_download_failure_and_refetches_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            install_root = root / "install-root"
            bin_dir = root / "bin"
            agent_dirs = CursorAgentDirs(root / "cfg")
            fetch_versions = iter(("2026.03.24-933d5a6", "2026.03.25-933d5a6"))
            seen_versions = []
            sleep_calls = []
            logs = []

            def fake_fetch_metadata(*, timeout_s=None):
                del timeout_s
                version = next(fetch_versions)
                return InstallerMetadata(version=version, installer_url="https://cursor.com/install")

            def fake_select_spec(meta, *, install_root, bin_dir):
                return self._spec(version=meta.version, install_root=install_root, bin_dir=bin_dir)

            def fake_install_from_spec(spec, *, timeout_s=None):
                del timeout_s
                seen_versions.append(spec.version)
                if spec.version == "2026.03.24-933d5a6":
                    raise HTTPError(spec.download_url, 403, "Forbidden", hdrs=None, fp=None)
                return CursorAgentInstallResult(
                    installed_path=str(bin_dir / "cursor-agent"),
                    version=spec.version,
                    performed_download=True,
                    repaired_launchers=False,
                    applied_compat_patch=False,
                    notes=(f"installed {spec.version}",),
                )

            result = run_cursor_agent_patch_canary(
                install_root=install_root,
                bin_dir=bin_dir,
                agent_dirs=agent_dirs,
                retry_delays_s=(7.0,),
                sleep=sleep_calls.append,
                log=logs.append,
                fetch_metadata=fake_fetch_metadata,
                select_spec=fake_select_spec,
                install_from_spec=fake_install_from_spec,
                apply_patch=lambda **_kwargs: SimpleNamespace(
                    patched_files=["a", "b", "c"],
                    repaired_files=[],
                    skipped_already_patched=0,
                    skipped_not_applicable=44,
                ),
            )

            self.assertEqual(result.version, "2026.03.25-933d5a6")
            self.assertEqual(seen_versions, ["2026.03.24-933d5a6", "2026.03.25-933d5a6"])
            self.assertEqual(sleep_calls, [7.0])
            self.assertTrue(any("transient_error=HTTP 403 Forbidden" in line for line in logs))
            self.assertTrue(any("upstream_version=2026.03.25-933d5a6" in line for line in logs))

    def test_canary_does_not_retry_non_retryable_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            install_root = root / "install-root"
            bin_dir = root / "bin"
            agent_dirs = CursorAgentDirs(root / "cfg")
            metadata_calls = []
            sleep_calls = []

            def fake_fetch_metadata(*, timeout_s=None):
                del timeout_s
                metadata_calls.append("called")
                return InstallerMetadata(version="2026.03.25-933d5a6", installer_url="https://cursor.com/install")

            def fake_select_spec(meta, *, install_root, bin_dir):
                return self._spec(version=meta.version, install_root=install_root, bin_dir=bin_dir)

            err = HTTPError("https://downloads.cursor.com/forbidden", 401, "Unauthorized", hdrs=None, fp=None)
            with self.assertRaises(HTTPError):
                run_cursor_agent_patch_canary(
                    install_root=install_root,
                    bin_dir=bin_dir,
                    agent_dirs=agent_dirs,
                    retry_delays_s=(7.0, 14.0),
                    sleep=sleep_calls.append,
                    log=lambda _msg: None,
                    fetch_metadata=fake_fetch_metadata,
                    select_spec=fake_select_spec,
                    install_from_spec=lambda _spec, *, timeout_s=None: (_ for _ in ()).throw(err),
                    apply_patch=lambda **_kwargs: self.fail("apply_patch should not run"),
                )

            self.assertEqual(metadata_calls, ["called"])
            self.assertEqual(sleep_calls, [])


if __name__ == "__main__":
    unittest.main()
