import json
import tempfile
import unittest
from pathlib import Path

from cursor_cli_manager.agent_discovery import discover_agent_workspaces
from cursor_cli_manager.agent_paths import CursorAgentDirs, workspace_hash_candidates
from cursor_cli_manager.agent_workspace_map import (
    learn_workspace_path,
    load_workspace_map,
    workspace_map_path,
)


class TestWorkspaceMap(unittest.TestCase):
    def test_learn_workspace_path_writes_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # Use a non-dot directory name; some sandboxed environments restrict creating dot dirs.
            config_dir = Path(td) / "cursor_config"
            agent_dirs = CursorAgentDirs(config_dir)
            ws_path = Path(td) / "my_workspace"
            ws_path.mkdir(parents=True, exist_ok=True)

            learn_workspace_path(agent_dirs, ws_path)

            p = workspace_map_path(agent_dirs)
            self.assertTrue(p.exists())

            # Ensure JSON is readable and contains our entries.
            raw = json.loads(p.read_text(encoding="utf-8"))
            self.assertIn("workspaces", raw)

            ws_map = load_workspace_map(agent_dirs)
            self.assertGreaterEqual(len(ws_map.workspaces), 1)

            expected_hashes = set(workspace_hash_candidates(ws_path))
            self.assertTrue(expected_hashes.intersection(set(ws_map.workspaces.keys())))

            # Stored path should be absolute/resolved.
            any_hash = next(iter(expected_hashes.intersection(set(ws_map.workspaces.keys()))))
            self.assertEqual(ws_map.workspaces[any_hash]["path"], str(ws_path.resolve()))

    def test_discover_agent_workspaces_uses_persisted_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # Use a non-dot directory name; some sandboxed environments restrict creating dot dirs.
            config_dir = Path(td) / "cursor_config"
            agent_dirs = CursorAgentDirs(config_dir)
            ws_path = Path(td) / "ws"
            ws_path.mkdir(parents=True, exist_ok=True)

            # Persist mapping first.
            learn_workspace_path(agent_dirs, ws_path)

            # Create chats bucket dir for one of the hashes.
            h = next(iter(workspace_hash_candidates(ws_path)))
            (agent_dirs.chats_dir / h).mkdir(parents=True, exist_ok=True)

            workspaces = discover_agent_workspaces(agent_dirs, workspace_candidates=[])
            by_hash = {w.cwd_hash: w for w in workspaces}
            self.assertIn(h, by_hash)
            self.assertEqual(by_hash[h].workspace_path, ws_path.resolve())

