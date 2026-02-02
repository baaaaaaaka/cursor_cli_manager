import tempfile
import unittest
from pathlib import Path

from cursor_cli_manager.agent_paths import CursorAgentDirs
from cursor_cli_manager.agent_workspace_map import workspace_map_path
from cursor_cli_manager.ccm_config import (
    CcmConfig,
    LEGACY_VERSION,
    has_legacy_install,
    load_ccm_config,
    record_installed_version,
    save_ccm_config,
)


class TestCcmConfig(unittest.TestCase):
    def test_record_installed_version_creates_with_legacy_when_workspace_exists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent_dirs = CursorAgentDirs(Path(td) / "cursor_config")
            ws_path = workspace_map_path(agent_dirs)
            ws_path.parent.mkdir(parents=True, exist_ok=True)
            ws_path.write_text("{}", encoding="utf-8")

            record_installed_version(agent_dirs, "0.7.0")
            cfg = load_ccm_config(agent_dirs)
            self.assertEqual(cfg.installed_versions, [LEGACY_VERSION, "0.7.0"])

    def test_record_installed_version_creates_without_legacy_when_no_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent_dirs = CursorAgentDirs(Path(td) / "cursor_config")
            record_installed_version(agent_dirs, "0.7.0")
            cfg = load_ccm_config(agent_dirs)
            self.assertEqual(cfg.installed_versions, ["0.7.0"])

    def test_record_installed_version_appends_only_once_when_config_exists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent_dirs = CursorAgentDirs(Path(td) / "cursor_config")
            save_ccm_config(agent_dirs, CcmConfig(installed_versions=[LEGACY_VERSION, "0.6.0"]))

            record_installed_version(agent_dirs, "0.6.0")
            cfg = load_ccm_config(agent_dirs)
            self.assertEqual(cfg.installed_versions, [LEGACY_VERSION, "0.6.0"])

            record_installed_version(agent_dirs, "0.7.0")
            cfg2 = load_ccm_config(agent_dirs)
            self.assertEqual(cfg2.installed_versions, [LEGACY_VERSION, "0.6.0", "0.7.0"])

    def test_has_legacy_install_requires_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent_dirs = CursorAgentDirs(Path(td) / "cursor_config")
            save_ccm_config(agent_dirs, CcmConfig(installed_versions=["0.6.0"]))
            self.assertFalse(has_legacy_install(agent_dirs))
            save_ccm_config(agent_dirs, CcmConfig(installed_versions=[LEGACY_VERSION]))
            self.assertTrue(has_legacy_install(agent_dirs))


if __name__ == "__main__":
    unittest.main()
