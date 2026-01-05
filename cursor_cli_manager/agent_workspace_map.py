from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from cursor_cli_manager.agent_paths import CursorAgentDirs, workspace_hash_candidates


WORKSPACE_MAP_FILENAME = "ccm-workspaces.json"


@dataclass(frozen=True)
class WorkspaceMap:
    version: int
    workspaces: Dict[str, Dict[str, Any]]  # hash -> {"path": str, "last_seen_ms": int}


def workspace_map_path(agent_dirs: CursorAgentDirs) -> Path:
    return agent_dirs.config_dir / WORKSPACE_MAP_FILENAME


def load_workspace_map(agent_dirs: CursorAgentDirs) -> WorkspaceMap:
    p = workspace_map_path(agent_dirs)
    if not p.exists():
        return WorkspaceMap(version=1, workspaces={})
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return WorkspaceMap(version=1, workspaces={})

    # Back-compat: allow plain mapping {hash: "/path"}.
    if isinstance(obj, dict) and "workspaces" not in obj and all(isinstance(k, str) for k in obj.keys()):
        workspaces: Dict[str, Dict[str, Any]] = {}
        for h, v in obj.items():
            if isinstance(v, str):
                workspaces[h] = {"path": v, "last_seen_ms": 0}
        return WorkspaceMap(version=1, workspaces=workspaces)

    if not isinstance(obj, dict):
        return WorkspaceMap(version=1, workspaces={})

    ver = obj.get("version", 1)
    if not isinstance(ver, int):
        ver = 1

    ws = obj.get("workspaces", {})
    if not isinstance(ws, dict):
        return WorkspaceMap(version=ver, workspaces={})

    out: Dict[str, Dict[str, Any]] = {}
    for h, v in ws.items():
        if not isinstance(h, str) or not isinstance(v, dict):
            continue
        path = v.get("path")
        if not isinstance(path, str) or not path:
            continue
        last_seen = v.get("last_seen_ms", 0)
        if not isinstance(last_seen, int):
            last_seen = 0
        out[h] = {"path": path, "last_seen_ms": last_seen}

    return WorkspaceMap(version=ver, workspaces=out)


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def save_workspace_map(agent_dirs: CursorAgentDirs, ws_map: WorkspaceMap) -> None:
    p = workspace_map_path(agent_dirs)
    payload = {"version": ws_map.version, "workspaces": ws_map.workspaces}
    _atomic_write_json(p, payload)


def learn_workspace_path(agent_dirs: CursorAgentDirs, workspace_path: Path) -> None:
    """
    Persist hash->path mapping for the given workspace path.

    We store all hash candidates (logical + resolved) to improve match rate.
    """
    # Important: cursor-agent buckets by md5(cwd) and we can't reliably know
    # whether cwd is returned as a symlinked path or a "physical" resolved path
    # (e.g. /var vs /private/var on macOS). So we hash the *logical* absolute
    # path and let workspace_hash_candidates add the resolved variant too.
    workspace_path_in = workspace_path.expanduser()
    try:
        ws_abs = workspace_path_in if workspace_path_in.is_absolute() else (Path.cwd() / workspace_path_in).absolute()
    except Exception:
        ws_abs = workspace_path_in

    # Store canonical path value (prefer resolved) but keep hash candidates broad.
    try:
        ws_value = ws_abs.resolve()
    except Exception:
        ws_value = ws_abs

    ws_map = load_workspace_map(agent_dirs)
    now_ms = int(time.time() * 1000)

    for h in workspace_hash_candidates(ws_abs):
        ws_map.workspaces[h] = {"path": str(ws_value), "last_seen_ms": now_ms}

    save_workspace_map(agent_dirs, ws_map)


def try_learn_current_cwd(agent_dirs: CursorAgentDirs) -> None:
    """
    Best-effort auto-learning hook: record md5(cwd) -> cwd path when running ccm.
    """
    try:
        learn_workspace_path(agent_dirs, Path.cwd())
    except Exception:
        # Never fail the app because learning couldn't write to disk.
        return

