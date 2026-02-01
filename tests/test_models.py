import unittest
from pathlib import Path

from cursor_cli_manager.models import AgentWorkspace


class TestModels(unittest.TestCase):
    def test_agent_workspace_display_name_unknown(self) -> None:
        ws = AgentWorkspace(cwd_hash="abc", workspace_path=None, chats_root=Path("/tmp/chats/abc"))
        self.assertIn("Unknown", ws.display_name)
        self.assertIn("abc", ws.display_name)

    def test_agent_workspace_display_name_from_path_name(self) -> None:
        ws = AgentWorkspace(cwd_hash="abc", workspace_path=Path("/tmp/myrepo"), chats_root=Path("/tmp/chats/abc"))
        self.assertEqual(ws.display_name, "myrepo")

    def test_agent_workspace_display_name_root_path(self) -> None:
        # Root has empty .name on POSIX; should fall back to the full string path.
        ws = AgentWorkspace(cwd_hash="abc", workspace_path=Path("/"), chats_root=Path("/tmp/chats/abc"))
        self.assertEqual(ws.display_name, str(Path("/")))


if __name__ == "__main__":
    unittest.main()

