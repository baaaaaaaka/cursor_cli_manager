from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from cursor_cli_manager.agent_paths import CursorAgentDirs, get_cursor_agent_dirs
from cursor_cli_manager.agent_workspace_map import workspace_map_path


CCM_CONFIG_FILENAME = "ccm-config.json"
LEGACY_VERSION = "legacy"


@dataclass(frozen=True)
class CcmConfig:
    installed_versions: List[str]


def ccm_config_path(agent_dirs: CursorAgentDirs) -> Path:
    return agent_dirs.config_dir / CCM_CONFIG_FILENAME


def _dedupe_versions(values: Iterable[object]) -> List[str]:
    out: List[str] = []
    seen = set()
    for v in values:
        if not isinstance(v, str):
            continue
        t = v.strip()
        if not t or t in seen:
            continue
        out.append(t)
        seen.add(t)
    return out


def _coerce_versions(value: object) -> List[str]:
    if not isinstance(value, list):
        return []
    return _dedupe_versions(value)


def load_ccm_config(agent_dirs: CursorAgentDirs) -> CcmConfig:
    p = ccm_config_path(agent_dirs)
    try:
        exists = p.exists()
    except Exception:
        return CcmConfig(installed_versions=[])
    if not exists:
        return CcmConfig(installed_versions=[])
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return CcmConfig(installed_versions=[])
    if not isinstance(obj, dict):
        return CcmConfig(installed_versions=[])
    versions = _coerce_versions(obj.get("installed_versions"))
    return CcmConfig(installed_versions=versions)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def save_ccm_config(agent_dirs: CursorAgentDirs, config: CcmConfig) -> None:
    payload = {"installed_versions": _dedupe_versions(config.installed_versions)}
    _atomic_write_json(ccm_config_path(agent_dirs), payload)


def record_installed_version(agent_dirs: CursorAgentDirs, version: str) -> None:
    v = (version or "").strip()
    if not v:
        return

    config_path = ccm_config_path(agent_dirs)
    try:
        config_exists = config_path.exists()
    except Exception:
        config_exists = False

    try:
        config = load_ccm_config(agent_dirs)
        versions = list(config.installed_versions)
    except Exception:
        versions = []

    if not config_exists:
        try:
            if workspace_map_path(agent_dirs).exists():
                versions.append(LEGACY_VERSION)
        except Exception:
            pass
        versions.append(v)
        versions = _dedupe_versions(versions)
        try:
            save_ccm_config(agent_dirs, CcmConfig(installed_versions=versions))
        except Exception:
            return
        return

    if v in versions:
        return
    versions.append(v)
    versions = _dedupe_versions(versions)
    try:
        save_ccm_config(agent_dirs, CcmConfig(installed_versions=versions))
    except Exception:
        return


def has_legacy_install(agent_dirs: Optional[CursorAgentDirs] = None) -> bool:
    dirs = agent_dirs or get_cursor_agent_dirs()
    try:
        config = load_ccm_config(dirs)
    except Exception:
        return False
    return LEGACY_VERSION in config.installed_versions
