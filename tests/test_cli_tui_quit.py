import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_cli_manager.agent_paths import CursorAgentDirs
from cursor_cli_manager.models import AgentWorkspace


class TestCmdTuiQuit(unittest.TestCase):
    def test_quit_does_not_restart_tui_loop(self) -> None:
        """
        Regression test:
        A bug in cmd_tui introduced an infinite loop that re-opened the TUI after quit,
        causing the UI to flash and immediately return after pressing 'q'.
        """
        agent_dirs = CursorAgentDirs(config_dir=Path("/tmp/ccm-test-config"))
        ws = AgentWorkspace(cwd_hash="h", workspace_path=Path("/tmp/ws"), chats_root=Path("/tmp/chats/h"))

        calls = {"n": 0}

        def fake_run_tui(_agent_dirs, _workspaces):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # simulate user quitting
            raise AssertionError("TUI was restarted after quit")

        with patch("cursor_cli_manager.cli.discover_agent_workspaces", return_value=[ws]), patch(
            "cursor_cli_manager.cli._pin_cwd_workspace", return_value=[ws]
        ), patch("cursor_cli_manager.cli._run_tui", side_effect=fake_run_tui), patch(
            "cursor_cli_manager.cli.start_cursor_agent_flag_probe"
        ), patch(
            "cursor_cli_manager.cli._ensure_cursor_agent_for_command", return_value="/tmp/cursor-agent"
        ):
            from cursor_cli_manager.cli import cmd_tui

            rc = cmd_tui(agent_dirs)
        self.assertEqual(rc, 0)
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
