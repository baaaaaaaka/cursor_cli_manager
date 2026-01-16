import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import sys

from cursor_cli_manager.agent_paths import CursorAgentDirs
from cursor_cli_manager.models import AgentWorkspace
from cursor_cli_manager.tui import UpdateRequested


class TestCmdTuiUpgradeRestartFallback(unittest.TestCase):
    def test_cmd_tui_does_not_crash_if_restart_fails_after_upgrade(self) -> None:
        agent_dirs = CursorAgentDirs(config_dir=Path("/tmp/ccm-test-config"))
        ws = AgentWorkspace(cwd_hash="h", workspace_path=Path("/tmp/ws"), chats_root=Path("/tmp/chats/h"))

        out_buf = io.StringIO()
        err_buf = io.StringIO()

        def fake_execvp(_file, _args):  # noqa: ANN001
            raise OSError(40, "Too many levels of symbolic links")

        def must_not_execv(_path, _args):  # noqa: ANN001
            raise AssertionError("os.execv should not be used for frozen binaries")

        with patch("cursor_cli_manager.cli.discover_agent_workspaces", return_value=[ws]), patch(
            "cursor_cli_manager.cli._pin_cwd_workspace", return_value=[ws]
        ), patch("cursor_cli_manager.cli._run_tui", side_effect=UpdateRequested()), patch(
            "cursor_cli_manager.cli.start_cursor_agent_flag_probe"
        ), patch(
            "cursor_cli_manager.cli.perform_update", return_value=(True, "updated to 0.5.8")
        ), patch.object(
            sys, "frozen", True, create=True
        ), patch(
            "cursor_cli_manager.cli.os.execvp", side_effect=fake_execvp
        ), patch(
            "cursor_cli_manager.cli.os.execv", side_effect=must_not_execv
        ), redirect_stdout(out_buf), redirect_stderr(err_buf):
            from cursor_cli_manager.cli import cmd_tui

            rc = cmd_tui(agent_dirs)

        self.assertEqual(rc, 0)
        self.assertIn("updated to 0.5.8", out_buf.getvalue())
        self.assertIn("failed to restart", err_buf.getvalue())


if __name__ == "__main__":
    unittest.main()

