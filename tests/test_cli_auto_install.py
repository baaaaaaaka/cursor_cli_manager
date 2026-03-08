import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cursor_cli_manager.agent_paths import CursorAgentDirs
from cursor_cli_manager.cli import cmd_tui, main
from cursor_cli_manager.models import AgentWorkspace


class TestCliAutoInstall(unittest.TestCase):
    def test_cmd_tui_ensures_cursor_agent_before_loading_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent_dirs = CursorAgentDirs(Path(td) / "cfg")
            with patch("cursor_cli_manager.cli._ensure_cursor_agent_for_command", return_value="/tmp/cursor-agent") as ensure, patch(
                "cursor_cli_manager.cli.discover_agent_workspaces", return_value=[]
            ), patch("cursor_cli_manager.cli._pin_cwd_workspace", return_value=[]):
                rc = cmd_tui(agent_dirs)
            self.assertEqual(rc, 1)
            ensure.assert_called_once_with(allow_install=True)

    def test_cmd_tui_returns_1_when_auto_install_fails(self) -> None:
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            agent_dirs = CursorAgentDirs(Path(td) / "cfg")
            with patch("cursor_cli_manager.cli._ensure_cursor_agent_for_command", return_value=None), redirect_stderr(err):
                rc = cmd_tui(agent_dirs)
        self.assertEqual(rc, 1)

    def test_list_does_not_trigger_auto_install(self) -> None:
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            with patch("cursor_cli_manager.cli._ensure_cursor_agent_for_command") as ensure, redirect_stdout(out):
                rc = main(["--config-dir", td, "list"])
        self.assertEqual(rc, 0)
        ensure.assert_not_called()

    def test_doctor_reports_install_resolution_without_auto_install(self) -> None:
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            resolved = SimpleNamespace(path=None, error="cursor-agent not found")
            with patch("cursor_cli_manager.cli.resolve_cursor_agent_installation", return_value=resolved), patch(
                "cursor_cli_manager.cli.get_cursor_agent_install_root", return_value=Path(td) / "install-root"
            ), patch(
                "cursor_cli_manager.cli.get_cursor_agent_bin_dir", return_value=Path(td) / "bin"
            ), patch(
                "cursor_cli_manager.cli.auto_install_enabled", return_value=False
            ), patch(
                "cursor_cli_manager.cli.get_cursor_agent_installer_url", return_value="https://cursor.example/install"
            ), patch(
                "cursor_cli_manager.cli.resolve_cursor_agent_versions_dir", return_value=None
            ), patch(
                "cursor_cli_manager.cli.discover_agent_workspaces", return_value=[]
            ), redirect_stdout(out):
                rc = main(["--config-dir", td, "doctor"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("- cursor-agent: NOT FOUND", text)
        self.assertIn("- auto-install enabled: False", text)
        self.assertIn("- installer url: https://cursor.example/install", text)
        self.assertIn("- resolution note: cursor-agent not found", text)
        self.assertIn("- versions dir: NOT FOUND", text)

    def test_doctor_reports_patch_dry_run_for_legacy_install(self) -> None:
        out = io.StringIO()
        report = SimpleNamespace(
            patched_files=[Path("a.js")],
            repaired_files=[Path("b.js")],
            skipped_already_patched=2,
            skipped_not_applicable=3,
            errors=[(Path("c.js"), "boom")],
        )
        ws = AgentWorkspace(cwd_hash="abc123", workspace_path=Path("/tmp/ws"), chats_root=Path("/tmp/chats"))
        with tempfile.TemporaryDirectory() as td:
            resolved = SimpleNamespace(path="/tmp/cursor-agent", error=None)
            with patch("cursor_cli_manager.cli.resolve_cursor_agent_installation", return_value=resolved), patch(
                "cursor_cli_manager.cli.get_cursor_agent_install_root", return_value=Path(td) / "install-root"
            ), patch(
                "cursor_cli_manager.cli.get_cursor_agent_bin_dir", return_value=Path(td) / "bin"
            ), patch(
                "cursor_cli_manager.cli.auto_install_enabled", return_value=True
            ), patch(
                "cursor_cli_manager.cli.get_cursor_agent_installer_url", return_value="https://cursor.example/install"
            ), patch(
                "cursor_cli_manager.cli.resolve_cursor_agent_versions_dir", return_value=Path(td) / "versions"
            ), patch(
                "cursor_cli_manager.cli.has_legacy_install", return_value=True
            ), patch(
                "cursor_cli_manager.cli.patch_cursor_agent_models", return_value=report
            ) as patch_models, patch(
                "cursor_cli_manager.cli.discover_agent_workspaces", return_value=[ws]
            ), patch(
                "cursor_cli_manager.cli.discover_agent_chats", return_value=[]
            ), redirect_stdout(out):
                rc = main(["--config-dir", td, "doctor"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("- versions dir:", text)
        self.assertIn("- model patch dry-run: would_patch=1 would_repair=1 already_patched=2 not_applicable=3 errors=1", text)
        self.assertIn("Discovered workspaces: 1", text)
        self.assertIn("- ws (/tmp/ws)", text)
        patch_models.assert_called_once()

    def test_doctor_reports_discovery_failure(self) -> None:
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            resolved = SimpleNamespace(path="/tmp/cursor-agent", error=None)
            with patch("cursor_cli_manager.cli.resolve_cursor_agent_installation", return_value=resolved), patch(
                "cursor_cli_manager.cli.get_cursor_agent_install_root", return_value=Path(td) / "install-root"
            ), patch(
                "cursor_cli_manager.cli.get_cursor_agent_bin_dir", return_value=Path(td) / "bin"
            ), patch(
                "cursor_cli_manager.cli.auto_install_enabled", return_value=True
            ), patch(
                "cursor_cli_manager.cli.get_cursor_agent_installer_url", return_value="https://cursor.example/install"
            ), patch(
                "cursor_cli_manager.cli.resolve_cursor_agent_versions_dir", return_value=None
            ), patch(
                "cursor_cli_manager.cli.discover_agent_workspaces", side_effect=RuntimeError("discovery boom")
            ), redirect_stdout(out):
                rc = main(["--config-dir", td, "doctor"])
        self.assertEqual(rc, 0)
        self.assertIn("Discovery failed: discovery boom", out.getvalue())

    def test_open_dry_run_does_not_trigger_auto_install(self) -> None:
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            with patch("cursor_cli_manager.cli._ensure_cursor_agent_for_command") as ensure, patch(
                "cursor_cli_manager.cli.build_resume_command",
                return_value=["/tmp/cursor-agent", "--workspace", "/tmp/ws", "--resume", "abc123"],
            ), redirect_stdout(out):
                rc = main(["--config-dir", td, "open", "abc123", "--workspace", "/tmp/ws", "--dry-run"])
        self.assertEqual(rc, 0)
        ensure.assert_not_called()
        self.assertIn("cd /tmp/ws", out.getvalue())

    def test_open_exec_triggers_auto_install(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch("cursor_cli_manager.cli._ensure_cursor_agent_for_command", return_value="/tmp/cursor-agent") as ensure, patch(
                "cursor_cli_manager.cli.exec_resume_chat", side_effect=SystemExit(0)
            ):
                with self.assertRaises(SystemExit):
                    main(["--config-dir", td, "open", "abc123", "--workspace", "/tmp/ws"])
        ensure.assert_called_once_with(allow_install=True)

    def test_open_exec_returns_1_when_auto_install_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch("cursor_cli_manager.cli._ensure_cursor_agent_for_command", return_value=None) as ensure, patch(
                "cursor_cli_manager.cli.exec_resume_chat"
            ) as exec_resume:
                rc = main(["--config-dir", td, "open", "abc123", "--workspace", "/tmp/ws"])
        self.assertEqual(rc, 1)
        ensure.assert_called_once_with(allow_install=True)
        exec_resume.assert_not_called()

    def test_ensure_cursor_agent_for_command_reports_error(self) -> None:
        err = io.StringIO()
        with patch("cursor_cli_manager.cli.ensure_cursor_agent_available", side_effect=RuntimeError("boom")), redirect_stderr(err):
            from cursor_cli_manager.cli import _ensure_cursor_agent_for_command

            result = _ensure_cursor_agent_for_command(allow_install=True)
        self.assertIsNone(result)
        self.assertIn("boom", err.getvalue())


if __name__ == "__main__":
    unittest.main()
