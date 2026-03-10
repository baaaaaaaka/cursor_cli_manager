#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cursor_cli_manager.agent_paths import CursorAgentDirs
from cursor_cli_manager.ccm_config import CcmConfig, LEGACY_VERSION, save_ccm_config
from cursor_cli_manager.cursor_agent_install import (
    ENV_CCM_CURSOR_AGENT_BIN_DIR,
    ENV_CCM_CURSOR_AGENT_INSTALL_ROOT,
    ENV_CCM_CURSOR_AGENT_POSTINSTALL_PATCH,
    CursorAgentInstallResult,
    apply_verified_cursor_agent_patch,
    fetch_official_installer_metadata,
    install_cursor_agent_from_spec,
    select_cursor_agent_install_spec,
)


@contextmanager
def _temp_env(overrides: Dict[str, str]) -> Iterator[None]:
    old: Dict[str, Optional[str]] = {}
    for key, value in overrides.items():
        old[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        yield
    finally:
        for key, value in overrides.items():
            prev = old.get(key)
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev


def _print_install_summary(result: CursorAgentInstallResult) -> None:
    print(f"installed_path={result.installed_path}")
    print(f"version={result.version}")
    print(f"performed_download={result.performed_download}")
    print(f"repaired_launchers={result.repaired_launchers}")
    if result.notes:
        print("notes=" + " | ".join(result.notes))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ccm-upstream-canary.") as td:
        base = Path(td)
        install_root = base / "install-root"
        bin_dir = base / "bin"
        cfg_dir = base / "cfg"
        agent_dirs = CursorAgentDirs(cfg_dir)
        save_ccm_config(agent_dirs, CcmConfig(installed_versions=[LEGACY_VERSION]))

        with _temp_env(
            {
                ENV_CCM_CURSOR_AGENT_INSTALL_ROOT: str(install_root),
                ENV_CCM_CURSOR_AGENT_BIN_DIR: str(bin_dir),
                ENV_CCM_CURSOR_AGENT_POSTINSTALL_PATCH: "off",
            }
        ):
            meta = fetch_official_installer_metadata()
            spec = select_cursor_agent_install_spec(
                meta,
                install_root=install_root,
                bin_dir=bin_dir,
            )
            print(f"upstream_version={meta.version}")
            print(f"download_url={spec.download_url}")
            result = install_cursor_agent_from_spec(spec)
            _print_install_summary(result)
            rep = apply_verified_cursor_agent_patch(
                versions_dir=install_root / "versions",
                cursor_agent_path=result.installed_path,
                agent_dirs=agent_dirs,
                force=True,
                require_changes=True,
            )
            print(
                "verified_patch="
                f"patched:{len(rep.patched_files)} repaired:{len(rep.repaired_files)} "
                f"already_patched:{rep.skipped_already_patched} not_applicable:{rep.skipped_not_applicable}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
