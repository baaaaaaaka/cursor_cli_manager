import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_cli_manager.agent_paths import (
    ENV_CURSOR_AGENT_CONFIG_DIR,
    get_cursor_agent_dirs,
    is_md5_hex,
    md5_hex,
    workspace_hash_candidates,
)


class TestAgentPaths(unittest.TestCase):
    def test_md5_hex_and_is_md5_hex(self) -> None:
        h = md5_hex("hello")
        self.assertTrue(isinstance(h, str))
        self.assertEqual(len(h), 32)
        self.assertTrue(is_md5_hex(h))
        self.assertFalse(is_md5_hex("not-md5"))
        self.assertFalse(is_md5_hex(""))

    def test_get_cursor_agent_dirs_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {ENV_CURSOR_AGENT_CONFIG_DIR: td}):
                dirs = get_cursor_agent_dirs()
                self.assertEqual(dirs.config_dir, Path(td))
                self.assertEqual(dirs.chats_dir, Path(td) / "chats")

    def test_cursor_config_directory_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            base = Path(td)
            os.environ["XDG_CONFIG_HOME"] = str(base / "xdg")
            self.assertEqual(get_cursor_agent_dirs().config_dir, base / "xdg" / "cursor")
            os.environ["CURSOR_CONFIG_DIR"] = str(base / "native")
            self.assertEqual(get_cursor_agent_dirs().config_dir, base / "native")
            os.environ[ENV_CURSOR_AGENT_CONFIG_DIR] = str(base / "ccm")
            self.assertEqual(get_cursor_agent_dirs().config_dir, base / "ccm")

    def test_workspace_hash_candidates_includes_resolved_variant_for_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            real = base / "real"
            real.mkdir()
            link = base / "link"

            # Symlink may not be available in some environments; skip if unsupported.
            try:
                link.symlink_to(real, target_is_directory=True)
            except Exception:
                self.skipTest("symlink not supported in this environment")

            cands = list(workspace_hash_candidates(link))
            self.assertGreaterEqual(len(cands), 1)
            self.assertIn(md5_hex(str(link.expanduser())), cands)
            # Resolved variant should usually differ for symlinks.
            self.assertIn(md5_hex(str(link.expanduser().resolve())), cands)

    def test_workspace_hash_candidates_survives_resolve_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            with patch("pathlib.Path.resolve", side_effect=Exception("boom")):
                cands = list(workspace_hash_candidates(p))
            self.assertEqual(cands, [md5_hex(str(p.expanduser()))])


if __name__ == "__main__":
    unittest.main()

