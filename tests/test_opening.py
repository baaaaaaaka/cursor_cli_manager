import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_cli_manager.opening import (
    ENV_CURSOR_AGENT_PATH,
    build_new_command,
    build_resume_command,
    resolve_cursor_agent_path,
)


class TestOpening(unittest.TestCase):
    def test_resolve_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cursor-agent"
            p.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
            self.assertEqual(resolve_cursor_agent_path(str(p)), str(p))

    def test_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cursor-agent"
            p.write_text("x", encoding="utf-8")
            with patch("shutil.which", return_value=None):
                old = os.environ.get(ENV_CURSOR_AGENT_PATH)
                try:
                    os.environ[ENV_CURSOR_AGENT_PATH] = str(p)
                    self.assertEqual(resolve_cursor_agent_path(), str(p))
                finally:
                    if old is None:
                        os.environ.pop(ENV_CURSOR_AGENT_PATH, None)
                    else:
                        os.environ[ENV_CURSOR_AGENT_PATH] = old

    def test_build_resume_command(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = Path(td) / "cursor-agent"
            agent.write_text("x", encoding="utf-8")
            cmd = build_resume_command(
                "abc123",
                workspace_path=Path("/tmp/ws"),
                cursor_agent_path=str(agent),
            )
            self.assertEqual(cmd[0], str(agent))
            self.assertIn("--resume", cmd)
            self.assertIn("abc123", cmd)

    def test_build_new_command(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = Path(td) / "cursor-agent"
            agent.write_text("x", encoding="utf-8")
            cmd = build_new_command(workspace_path=Path("/tmp/ws"), cursor_agent_path=str(agent))
            self.assertEqual(cmd[0], str(agent))
            self.assertNotIn("--resume", cmd)
            self.assertIn("--workspace", cmd)


if __name__ == "__main__":
    unittest.main()

