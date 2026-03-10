import io
import os
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cursor_cli_manager.cursor_agent_install as cai
from cursor_cli_manager.agent_paths import CursorAgentDirs
from cursor_cli_manager.ccm_config import CcmConfig, LEGACY_VERSION, save_ccm_config
from cursor_cli_manager.opening import LaunchSmokeResult


def _tar_bytes(entries: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, payload in entries.items():
            data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _zip_bytes(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, payload in entries.items():
            data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
            zf.writestr(name, data)
    return buf.getvalue()


SAMPLE_PATCHABLE_JS = """
var __awaiter = (this && this.__awaiter) || function () {};

function fetchUsableModels(aiServerClient) {
    return __awaiter(this, void 0, void 0, function* () {
        const { models } = yield aiServerClient.getUsableModels(new KD({}));
        return models.length > 0 ? models : undefined;
    });
}
function fetchDefaultModel(aiServerClient) {
    return __awaiter(this, void 0, void 0, function* () {
        return null;
    });
}
"""


class TestCursorAgentInstall(unittest.TestCase):
    def test_default_install_dirs_posix(self) -> None:
        with patch("cursor_cli_manager.cursor_agent_install.sys.platform", "linux"), patch(
            "pathlib.Path.home", return_value=Path("/home/test")
        ):
            self.assertEqual(cai.default_install_root_dir(), Path("/home/test/.local/share/cursor-agent"))
            self.assertEqual(cai.default_install_bin_dir(), Path("/home/test/.local/bin"))

    def test_default_install_dirs_windows(self) -> None:
        with patch("cursor_cli_manager.cursor_agent_install.sys.platform", "win32"), patch.dict(
            os.environ, {"LOCALAPPDATA": r"C:\Users\baka\AppData\Local"}, clear=False
        ):
            root = cai.default_install_root_dir()
            self.assertEqual(root, Path(r"C:\Users\baka\AppData\Local") / "cursor-agent")
            self.assertEqual(cai.default_install_bin_dir(install_root=root), root / "bin")

    def test_fetch_official_installer_metadata_parses_version(self) -> None:
        script = (
            '#!/usr/bin/env bash\n'
            'DOWNLOAD_URL="https://downloads.cursor.com/lab/2026.02.27-e7d2ef6/${OS}/${ARCH}/agent-cli-package.tar.gz"\n'
        ).encode("utf-8")

        def fake_fetch(url: str, _timeout_s: float, _headers: dict) -> bytes:
            self.assertEqual(url, cai.DEFAULT_INSTALLER_URL)
            return script

        meta = cai.fetch_official_installer_metadata(fetch=fake_fetch)
        self.assertEqual(meta.version, "2026.02.27-e7d2ef6")

    def test_fetch_official_installer_metadata_fails_when_script_shape_changes(self) -> None:
        with self.assertRaises(RuntimeError):
            cai.fetch_official_installer_metadata(fetch=lambda *_a, **_k: b"echo nope\n")

    def test_select_cursor_agent_install_spec_rejects_unsupported_os(self) -> None:
        meta = cai.InstallerMetadata(version="2026.02.27-e7d2ef6", installer_url="https://cursor.com/install")
        with self.assertRaises(RuntimeError):
            cai.select_cursor_agent_install_spec(meta, system="FreeBSD", machine="x86_64")

    def test_select_cursor_agent_install_spec_windows(self) -> None:
        meta = cai.InstallerMetadata(version="2026.02.27-e7d2ef6", installer_url="https://cursor.com/install")
        spec = cai.select_cursor_agent_install_spec(
            meta,
            system="Windows",
            machine="AMD64",
            install_root=Path(r"C:\Temp\cursor-agent"),
            bin_dir=Path(r"C:\Temp\cursor-agent\bin"),
        )
        self.assertEqual(spec.archive_kind, "zip")
        self.assertEqual(spec.arch, "x64")
        self.assertEqual(
            spec.download_url,
            "https://downloads.cursor.com/lab/2026.02.27-e7d2ef6/windows/x64/agent-cli-package.zip",
        )

    def test_select_cursor_agent_install_spec_rejects_unsupported_arch(self) -> None:
        meta = cai.InstallerMetadata(version="2026.02.27-e7d2ef6", installer_url="https://cursor.com/install")
        with self.assertRaises(RuntimeError):
            cai.select_cursor_agent_install_spec(meta, system="Linux", machine="mips")

    def test_select_cursor_agent_install_spec_honors_download_base_override(self) -> None:
        meta = cai.InstallerMetadata(version="2026.02.27-e7d2ef6", installer_url="https://cursor.com/install")
        with patch.dict(os.environ, {cai.ENV_CCM_CURSOR_AGENT_DOWNLOAD_BASE_URL: "http://127.0.0.1:8000"}, clear=False):
            spec = cai.select_cursor_agent_install_spec(meta, system="Linux", machine="x86_64")
        self.assertEqual(
            spec.download_url,
            "http://127.0.0.1:8000/lab/2026.02.27-e7d2ef6/linux/x64/agent-cli-package.tar.gz",
        )

    def test_resolve_cursor_agent_installation_accepts_agent_on_path_only_when_it_looks_like_cursor_agent(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ,
            {
                cai.ENV_CCM_CURSOR_AGENT_INSTALL_ROOT: str(Path(td) / "root"),
                cai.ENV_CCM_CURSOR_AGENT_BIN_DIR: str(Path(td) / "empty-bin"),
            },
            clear=False,
        ):
            bindir = Path(td)
            agent = bindir / "agent"
            cursor_agent = bindir / "cursor-agent"
            agent.write_text("#!/bin/sh\n", encoding="utf-8")
            cursor_agent.write_text("#!/bin/sh\n", encoding="utf-8")
            with patch("cursor_cli_manager.cursor_agent_install.shutil.which", side_effect=[None, str(agent)]):
                res = cai.resolve_cursor_agent_installation()
        self.assertEqual(res.path, str(agent))
        self.assertEqual(res.source, "PATH:agent")

    def test_resolve_cursor_agent_installation_ignores_unrelated_agent_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ,
            {
                cai.ENV_CCM_CURSOR_AGENT_INSTALL_ROOT: str(Path(td) / "root"),
                cai.ENV_CCM_CURSOR_AGENT_BIN_DIR: str(Path(td) / "empty-bin"),
            },
            clear=False,
        ), patch(
            "cursor_cli_manager.cursor_agent_install.shutil.which",
            side_effect=[None, "/tmp/unrelated-agent"],
        ), patch("cursor_cli_manager.cursor_agent_install._is_valid_cursor_agent_alias", return_value=False):
            res = cai.resolve_cursor_agent_installation()
        self.assertIsNone(res.path)
        self.assertEqual(res.source, "missing")

    def test_resolve_cursor_agent_installation_prefers_default_bin_over_unrelated_agent_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ,
            {
                cai.ENV_CCM_CURSOR_AGENT_INSTALL_ROOT: str(Path(td) / "root"),
                cai.ENV_CCM_CURSOR_AGENT_BIN_DIR: str(Path(td) / "bin"),
            },
            clear=False,
        ):
            p = Path(td) / "bin" / "cursor-agent"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("#!/bin/sh\n", encoding="utf-8")
            with patch(
                "cursor_cli_manager.cursor_agent_install.shutil.which",
                side_effect=[None, "/tmp/unrelated-agent"],
            ), patch("cursor_cli_manager.cursor_agent_install._is_valid_cursor_agent_alias", return_value=False):
                res = cai.resolve_cursor_agent_installation()
        self.assertEqual(res.path, str(p))
        self.assertEqual(res.source, "default-bin:cursor-agent")

    def test_resolve_cursor_agent_installation_uses_default_bin_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ,
            {
                cai.ENV_CCM_CURSOR_AGENT_INSTALL_ROOT: str(Path(td) / "root"),
                cai.ENV_CCM_CURSOR_AGENT_BIN_DIR: str(Path(td) / "bin"),
            },
            clear=False,
        ), patch("cursor_cli_manager.cursor_agent_install.shutil.which", side_effect=[None, None]):
            p = Path(td) / "bin" / "agent"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("#!/bin/sh\n", encoding="utf-8")
            res = cai.resolve_cursor_agent_installation()
            self.assertEqual(res.path, str(p))
            self.assertEqual(res.source, "default-bin:agent")

    def test_resolve_cursor_agent_installation_marks_repairable_install(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ,
            {
                cai.ENV_CCM_CURSOR_AGENT_INSTALL_ROOT: str(Path(td) / "root"),
                cai.ENV_CCM_CURSOR_AGENT_BIN_DIR: str(Path(td) / "bin"),
            },
            clear=False,
        ), patch("cursor_cli_manager.cursor_agent_install.shutil.which", return_value=None), patch(
            "cursor_cli_manager.cursor_agent_install.sys.platform", "linux"
        ):
            version_dir = Path(td) / "root" / "versions" / "2026.02.27-e7d2ef6"
            version_dir.mkdir(parents=True, exist_ok=True)
            exe = version_dir / "cursor-agent"
            exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(exe, 0o755)
            res = cai.resolve_cursor_agent_installation()
            self.assertTrue(res.repairable_default_install)

    def test_install_cursor_agent_from_tar_installs_launchers(self) -> None:
        data = _tar_bytes(
            {
                "dist-package/cursor-agent": "#!/bin/sh\nexit 0\n",
                "dist-package/index.js": "console.log('ok')\n",
                "dist-package/package.json": "{}\n",
            }
        )
        spec = cai.CursorAgentInstallSpec(
            version="2026.02.27-e7d2ef6",
            system="linux",
            arch="x64",
            archive_kind="tar.gz",
            download_url="https://downloads.cursor.com/lab/v/linux/x64/agent-cli-package.tar.gz",
            install_root=Path("/unused"),
            bin_dir=Path("/unused"),
        )

        def fake_fetch(url: str, _timeout_s: float, _headers: dict) -> bytes:
            self.assertIn("downloads.cursor.com", url)
            return data

        with tempfile.TemporaryDirectory() as td, patch(
            "cursor_cli_manager.cursor_agent_install._verify_cursor_agent_command"
        ) as verify, patch(
            "cursor_cli_manager.cursor_agent_install.maybe_apply_postinstall_compat_patch",
            return_value=False,
        ):
            spec = cai.CursorAgentInstallSpec(
                version=spec.version,
                system=spec.system,
                arch=spec.arch,
                archive_kind=spec.archive_kind,
                download_url=spec.download_url,
                install_root=Path(td) / "root",
                bin_dir=Path(td) / "bin",
            )
            res = cai.install_cursor_agent_from_spec(spec, fetch=fake_fetch)

            exe = spec.install_root / "versions" / spec.version / "cursor-agent"
            self.assertTrue(exe.exists())
            self.assertTrue((spec.bin_dir / "cursor-agent").exists())
            self.assertTrue((spec.bin_dir / "agent").exists())
            verify.assert_called_once()
            self.assertEqual(res.installed_path, str(spec.bin_dir / "cursor-agent"))
            self.assertTrue(res.performed_download)

    def test_install_cursor_agent_noops_when_launcher_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            bindir = Path(td) / "bin"
            bindir.mkdir(parents=True, exist_ok=True)
            launcher = bindir / "cursor-agent"
            launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            spec = cai.CursorAgentInstallSpec(
                version="2026.02.27-e7d2ef6",
                system="linux",
                arch="x64",
                archive_kind="tar.gz",
                download_url="unused",
                install_root=root,
                bin_dir=bindir,
            )
            res = cai.install_cursor_agent_from_spec(spec, fetch=lambda *_a, **_k: self.fail("fetch should not run"))
            self.assertEqual(res.installed_path, str(launcher))
            self.assertFalse(res.performed_download)

    def test_install_cursor_agent_from_zip_installs_windows_wrappers(self) -> None:
        data = _zip_bytes(
            {
                "dist-package/cursor-agent.cmd": "@echo off\r\nexit /b 0\r\n",
                "dist-package/cursor-agent.ps1": "exit 0\n",
                "dist-package/index.js": "console.log('ok')\n",
                "dist-package/package.json": "{}\n",
                "dist-package/node.exe": b"PE",
            }
        )

        def fake_fetch(_url: str, _timeout_s: float, _headers: dict) -> bytes:
            return data

        with tempfile.TemporaryDirectory() as td, patch(
            "cursor_cli_manager.cursor_agent_install.sys.platform", "win32"
        ), patch(
            "cursor_cli_manager.cursor_agent_install.shutil.which", return_value=None
        ), patch(
            "cursor_cli_manager.cursor_agent_install._verify_cursor_agent_command"
        ) as verify, patch(
            "cursor_cli_manager.cursor_agent_install.maybe_apply_postinstall_compat_patch",
            return_value=False,
        ):
            spec = cai.CursorAgentInstallSpec(
                version="2026.02.27-e7d2ef6",
                system="windows",
                arch="x64",
                archive_kind="zip",
                download_url="https://downloads.cursor.com/lab/v/windows/x64/agent-cli-package.zip",
                install_root=Path(td) / "root",
                bin_dir=Path(td) / "bin",
            )
            res = cai.install_cursor_agent_from_spec(spec, fetch=fake_fetch)

            self.assertEqual(res.installed_path, str(spec.bin_dir / "cursor-agent.cmd"))
            self.assertTrue((spec.bin_dir / "cursor-agent.cmd").exists())
            self.assertTrue((spec.bin_dir / "agent.cmd").exists())
            verify.assert_called_once()

    def test_safe_extract_zip_rejects_unsafe_member_paths(self) -> None:
        samples = (
            _zip_bytes({"/etc/passwd": "x"}),
            _zip_bytes({"../evil.txt": "x"}),
        )
        for payload in samples:
            with self.subTest(payload=payload[:16]), tempfile.TemporaryDirectory() as td:
                with self.assertRaises(RuntimeError):
                    cai._safe_extract_zip(payload, dest_dir=Path(td))

    def test_install_posix_launcher_falls_back_to_wrapper_when_symlink_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch(
            "cursor_cli_manager.cursor_agent_install._atomic_symlink", side_effect=OSError("no symlink")
        ):
            target = Path(td) / "versions" / "2026.02.27-e7d2ef6" / "cursor-agent"
            link = Path(td) / "bin" / "cursor-agent"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            used_symlink = cai._install_posix_launcher(target, link)
            self.assertFalse(used_symlink)
            self.assertIn("exec", link.read_text(encoding="utf-8"))
            self.assertIn("CURSOR_AGENT_VERSIONS_DIR", link.read_text(encoding="utf-8"))

    def test_ensure_cursor_agent_available_rejects_invalid_explicit_override(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            cai.ensure_cursor_agent_available(explicit="/tmp/definitely-missing-cursor-agent", auto_install=True)
        self.assertIn("Fix the override", str(ctx.exception))

    def test_ensure_cursor_agent_available_obeys_disable_flag(self) -> None:
        with patch.dict(os.environ, {cai.ENV_CCM_AUTO_INSTALL_CURSOR_AGENT: "0"}, clear=False), patch(
            "cursor_cli_manager.cursor_agent_install.resolve_cursor_agent_installation",
            return_value=cai.CursorAgentResolveResult(path=None, error="cursor-agent not found"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                cai.ensure_cursor_agent_available(auto_install=True)
        self.assertIn("Install Cursor Agent manually", str(ctx.exception))

    def test_ensure_cursor_agent_available_wraps_install_failures(self) -> None:
        with patch(
            "cursor_cli_manager.cursor_agent_install.resolve_cursor_agent_installation",
            return_value=cai.CursorAgentResolveResult(path=None, error="cursor-agent not found"),
        ), patch(
            "cursor_cli_manager.cursor_agent_install.fetch_official_installer_metadata",
            side_effect=RuntimeError("metadata boom"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                cai.ensure_cursor_agent_available(auto_install=True)
        msg = str(ctx.exception)
        self.assertIn("automatic Cursor Agent install failed: metadata boom", msg)
        self.assertIn(cai.DEFAULT_INSTALLER_URL, msg)

    def test_install_cursor_agent_repairs_existing_launchers_without_download(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch(
            "cursor_cli_manager.cursor_agent_install._verify_cursor_agent_command"
        ) as verify, patch(
            "cursor_cli_manager.cursor_agent_install.maybe_apply_postinstall_compat_patch",
            return_value=False,
        ), patch(
            "cursor_cli_manager.cursor_agent_install.sys.platform", "linux"
        ):
            root = Path(td) / "root"
            bindir = Path(td) / "bin"
            version_dir = root / "versions" / "2026.02.27-e7d2ef6"
            version_dir.mkdir(parents=True, exist_ok=True)
            exe = version_dir / "cursor-agent"
            exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(exe, 0o755)
            spec = cai.CursorAgentInstallSpec(
                version="2026.02.27-e7d2ef6",
                system="linux",
                arch="x64",
                archive_kind="tar.gz",
                download_url="unused",
                install_root=root,
                bin_dir=bindir,
            )
            res = cai.install_cursor_agent_from_spec(spec, fetch=lambda *_a, **_k: self.fail("fetch should not run"))
            self.assertTrue(res.repaired_launchers)
            self.assertTrue((bindir / "cursor-agent").exists())
            verify.assert_called_once()

    def test_install_cursor_agent_applies_forced_compat_patch(self) -> None:
        data = _tar_bytes(
            {
                "dist-package/cursor-agent": "#!/bin/sh\nexit 0\n",
                "dist-package/index.js": "console.log('ok')\n",
                "dist-package/package.json": "{}\n",
            }
        )
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {cai.ENV_CCM_CURSOR_AGENT_POSTINSTALL_PATCH: "force"}, clear=False
        ), patch(
            "cursor_cli_manager.cursor_agent_install._verify_cursor_agent_command"
        ), patch(
            "cursor_cli_manager.cursor_agent_install.apply_verified_cursor_agent_patch"
        ) as apply_patch:
            apply_patch.return_value = type(
                "PatchReport",
                (),
                {"patched_files": [Path("x")], "repaired_files": [], "errors": [], "ok": True},
            )()
            spec = cai.CursorAgentInstallSpec(
                version="2026.02.27-e7d2ef6",
                system="linux",
                arch="x64",
                archive_kind="tar.gz",
                download_url="https://downloads.cursor.com/lab/v/linux/x64/agent-cli-package.tar.gz",
                install_root=Path(td) / "root",
                bin_dir=Path(td) / "bin",
            )
            res = cai.install_cursor_agent_from_spec(spec, fetch=lambda *_a, **_k: data)
            self.assertTrue(res.applied_compat_patch)
            apply_patch.assert_called_once()

    def test_apply_verified_cursor_agent_patch_verifies_launch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            versions_dir = root / "versions"
            vdir = versions_dir / "2026.02.27-e7d2ef6"
            vdir.mkdir(parents=True, exist_ok=True)
            js = vdir / "1234.index.js"
            js.write_text(SAMPLE_PATCHABLE_JS, encoding="utf-8")
            agent = root / "bin" / "cursor-agent"
            agent.parent.mkdir(parents=True, exist_ok=True)
            agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            agent_dirs = CursorAgentDirs(root / "cfg")
            save_ccm_config(agent_dirs, CcmConfig(installed_versions=[LEGACY_VERSION]))

            with patch("cursor_cli_manager.cursor_agent_install._verify_cursor_agent_command") as verify, patch(
                "cursor_cli_manager.opening.run_cursor_agent_launch_smoke",
                return_value=LaunchSmokeResult(ok=True, exit_code=0, elapsed_s=0.2, output="", launch_sustained=True),
            ) as smoke:
                rep = cai.apply_verified_cursor_agent_patch(
                    versions_dir=versions_dir,
                    cursor_agent_path=str(agent),
                    agent_dirs=agent_dirs,
                )

            self.assertEqual(len(rep.patched_files), 1)
            self.assertIn("CCM_PATCH_AVAILABLE_MODELS_NORMALIZED", js.read_text(encoding="utf-8"))
            verify.assert_called_once_with(str(agent), timeout_s=5.0)
            smoke.assert_called_once()

    def test_apply_verified_cursor_agent_patch_ignores_unrelated_scan_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            versions_dir = root / "versions"
            target_vdir = versions_dir / "2026.02.27-e7d2ef6"
            other_vdir = versions_dir / "2025.12.01-deadbeef"
            target_vdir.mkdir(parents=True, exist_ok=True)
            other_vdir.mkdir(parents=True, exist_ok=True)
            js = target_vdir / "1234.index.js"
            js.write_text(SAMPLE_PATCHABLE_JS, encoding="utf-8")
            agent = target_vdir / "cursor-agent"
            agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            rep = SimpleNamespace(
                patched_files=[js],
                repaired_files=[],
                errors=[(other_vdir / "broken.index.js", "read failed")],
                ok=False,
            )

            with patch("cursor_cli_manager.cursor_agent_install.patch_cursor_agent_models", return_value=rep), patch(
                "cursor_cli_manager.cursor_agent_install._verify_cursor_agent_command"
            ) as verify, patch(
                "cursor_cli_manager.opening.run_cursor_agent_launch_smoke",
                return_value=LaunchSmokeResult(ok=True, exit_code=0, elapsed_s=0.5, output="", launch_sustained=True),
            ) as smoke:
                out = cai.apply_verified_cursor_agent_patch(
                    versions_dir=versions_dir,
                    cursor_agent_path=str(agent),
                )

            self.assertIs(out, rep)
            verify.assert_called_once_with(str(agent), timeout_s=5.0)
            smoke.assert_called_once()

    def test_apply_verified_cursor_agent_patch_rejects_quick_clean_exit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            versions_dir = root / "versions"
            vdir = versions_dir / "2026.02.27-e7d2ef6"
            vdir.mkdir(parents=True, exist_ok=True)
            js = vdir / "1234.index.js"
            original = SAMPLE_PATCHABLE_JS
            js.write_text(original, encoding="utf-8")
            agent = root / "bin" / "cursor-agent"
            agent.parent.mkdir(parents=True, exist_ok=True)
            agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            with patch("cursor_cli_manager.cursor_agent_install._verify_cursor_agent_command") as verify, patch(
                "cursor_cli_manager.opening.run_cursor_agent_launch_smoke",
                return_value=LaunchSmokeResult(ok=True, exit_code=0, elapsed_s=0.1, output="ok", launch_sustained=False),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    cai.apply_verified_cursor_agent_patch(
                        versions_dir=versions_dir,
                        cursor_agent_path=str(agent),
                    )

            self.assertIn("exited before launch verification completed", str(ctx.exception))
            self.assertEqual(js.read_text(encoding="utf-8"), original)
            self.assertGreaterEqual(verify.call_count, 2)

    def test_apply_verified_cursor_agent_patch_rolls_back_on_launch_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            versions_dir = root / "versions"
            vdir = versions_dir / "2026.02.27-e7d2ef6"
            vdir.mkdir(parents=True, exist_ok=True)
            js = vdir / "1234.index.js"
            original = SAMPLE_PATCHABLE_JS
            js.write_text(original, encoding="utf-8")
            agent = root / "bin" / "cursor-agent"
            agent.parent.mkdir(parents=True, exist_ok=True)
            agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            def fail_smoke(*_args, **_kwargs):  # noqa: ANN001
                bak = js.with_suffix(js.suffix + ".ccm.bak")
                try:
                    bak.unlink()
                except FileNotFoundError:
                    pass
                return LaunchSmokeResult(ok=False, exit_code=2, elapsed_s=0.1, output="boom")

            with patch("cursor_cli_manager.cursor_agent_install._verify_cursor_agent_command") as verify, patch(
                "cursor_cli_manager.opening.run_cursor_agent_launch_smoke",
                side_effect=fail_smoke,
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    cai.apply_verified_cursor_agent_patch(
                        versions_dir=versions_dir,
                        cursor_agent_path=str(agent),
                    )

            self.assertIn("patch rolled back", str(ctx.exception))
            self.assertEqual(js.read_text(encoding="utf-8"), original)
            self.assertFalse((versions_dir / ".ccm-patch-cache.json").exists())
            self.assertGreaterEqual(verify.call_count, 2)

    def test_install_cursor_agent_rejects_missing_payload_files(self) -> None:
        data = _tar_bytes(
            {
                "dist-package/cursor-agent": "#!/bin/sh\nexit 0\n",
                "dist-package/index.js": "console.log('ok')\n",
            }
        )
        with tempfile.TemporaryDirectory() as td, patch(
            "cursor_cli_manager.cursor_agent_install.maybe_apply_postinstall_compat_patch",
            return_value=False,
        ):
            spec = cai.CursorAgentInstallSpec(
                version="2026.02.27-e7d2ef6",
                system="linux",
                arch="x64",
                archive_kind="tar.gz",
                download_url="https://downloads.cursor.com/lab/v/linux/x64/agent-cli-package.tar.gz",
                install_root=Path(td) / "root",
                bin_dir=Path(td) / "bin",
            )
            with self.assertRaises(RuntimeError) as ctx:
                cai.install_cursor_agent_from_spec(spec, fetch=lambda *_a, **_k: data)
        self.assertIn("missing package.json", str(ctx.exception))

    def test_should_apply_compat_patch_detects_el7_auto_mode(self) -> None:
        with patch.dict(os.environ, {cai.ENV_CCM_CURSOR_AGENT_POSTINSTALL_PATCH: "auto"}, clear=False), patch(
            "cursor_cli_manager.cursor_agent_install.platform.system", return_value="Linux"
        ), patch(
            "cursor_cli_manager.cursor_agent_install._load_os_release",
            return_value={"ID": "centos", "VERSION_ID": "7.5"},
        ):
            self.assertTrue(cai._should_apply_compat_patch())

    def test_verify_cursor_agent_command_windows_cmd_uses_cmd_exe(self) -> None:
        calls = []

        def fake_runner(cmd, _timeout_s):  # noqa: ANN001
            calls.append(list(cmd))
            return 0, "ok", ""

        with tempfile.TemporaryDirectory() as td, patch(
            "cursor_cli_manager.cursor_agent_install.sys.platform", "win32"
        ), patch("cursor_cli_manager.cursor_agent_install._default_runner", side_effect=fake_runner):
            exe = Path(td) / "cursor-agent.cmd"
            exe.write_text("@echo off\r\n", encoding="utf-8")
            cai._verify_cursor_agent_command(str(exe))
        self.assertEqual(calls[0][:3], ["cmd.exe", "/d", "/s"])

    def test_verify_cursor_agent_command_raises_with_command_output(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch(
            "cursor_cli_manager.cursor_agent_install._default_runner", return_value=(2, "", "bad flag")
        ):
            exe = Path(td) / "cursor-agent"
            exe.write_text("#!/bin/sh\n", encoding="utf-8")
            with self.assertRaises(RuntimeError) as ctx:
                cai._verify_cursor_agent_command(str(exe))
        self.assertIn("exit 2", str(ctx.exception))
        self.assertIn("bad flag", str(ctx.exception))

    def test_install_cursor_agent_waits_for_lock(self) -> None:
        spec = cai.CursorAgentInstallSpec(
            version="2026.02.27-e7d2ef6",
            system="linux",
            arch="x64",
            archive_kind="tar.gz",
            download_url="unused",
            install_root=Path("/tmp/root"),
            bin_dir=Path("/tmp/bin"),
        )
        seen = {}

        @cai.contextmanager
        def fake_lock(*, install_root: Path, wait_s: float = 0.0):
            seen["wait_s"] = wait_s
            yield object()

        with patch("cursor_cli_manager.cursor_agent_install._install_lock", side_effect=fake_lock), patch(
            "cursor_cli_manager.cursor_agent_install._bin_candidates", return_value=[Path("/tmp/missing")]
        ), patch(
            "cursor_cli_manager.cursor_agent_install._latest_installed_executable", return_value=None
        ):
            with self.assertRaises(RuntimeError):
                cai.install_cursor_agent_from_spec(spec, fetch=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("stop")))
        self.assertGreater(seen["wait_s"], 0.0)

    def test_install_lock_times_out_when_already_held(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            (root / cai._INSTALL_LOCK_DIRNAME).mkdir(parents=True, exist_ok=True)
            with self.assertRaises(RuntimeError) as ctx:
                with cai._install_lock(install_root=root, wait_s=0.0):
                    self.fail("lock acquisition should not succeed")
        self.assertIn("install already in progress", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
