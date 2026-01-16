import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import curses

from cursor_cli_manager.agent_paths import CursorAgentDirs
from cursor_cli_manager.models import AgentWorkspace


class TestCmdTuiTerminalError(unittest.TestCase):
    def test_cmd_tui_handles_setupterm_error(self) -> None:
        agent_dirs = CursorAgentDirs(config_dir=Path("/tmp/ccm-test-config"))
        ws = AgentWorkspace(cwd_hash="h", workspace_path=Path("/tmp/ws"), chats_root=Path("/tmp/chats/h"))

        buf = io.StringIO()
        with patch("cursor_cli_manager.cli.discover_agent_workspaces", return_value=[ws]), patch(
            "cursor_cli_manager.cli._pin_cwd_workspace", return_value=[ws]
        ), patch(
            "cursor_cli_manager.cli._run_tui", side_effect=curses.error("setupterm: could not find terminal")
        ), patch("cursor_cli_manager.cli.start_cursor_agent_flag_probe"), redirect_stderr(buf):
            from cursor_cli_manager.cli import cmd_tui

            rc = cmd_tui(agent_dirs)

        self.assertEqual(rc, 2)
        txt = buf.getvalue()
        self.assertIn("setupterm", txt)
        self.assertIn("TERM=", txt)


if __name__ == "__main__":
    unittest.main()

