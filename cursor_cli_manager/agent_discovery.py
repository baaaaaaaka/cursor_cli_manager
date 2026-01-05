from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

from cursor_cli_manager.agent_paths import CursorAgentDirs, get_cursor_agent_dirs, is_md5_hex, workspace_hash_candidates
from cursor_cli_manager.agent_workspace_map import load_workspace_map
from cursor_cli_manager.agent_store import extract_last_message_preview, read_chat_meta
from cursor_cli_manager.models import AgentChat, AgentWorkspace
from cursor_cli_manager.paths import CursorUserDirs, get_cursor_user_dirs
from cursor_cli_manager.vscdb import read_json


RECENTLY_OPENED_KEY = "history.recentlyOpenedPathsList"


def _file_uri_to_path(uri: str) -> Optional[Path]:
    try:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return None
        return Path(unquote(parsed.path))
    except Exception:
        return None


def discover_recent_folders_from_cursor_gui(user_dirs: CursorUserDirs) -> List[Path]:
    """
    Best-effort: use Cursor GUI's recent folders list as workspace candidates.
    """
    data = read_json(user_dirs.global_state_vscdb, RECENTLY_OPENED_KEY)
    if not isinstance(data, dict):
        return []
    entries = data.get("entries")
    if not isinstance(entries, list):
        return []

    out: List[Path] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        folder_uri = e.get("folderUri")
        if not isinstance(folder_uri, str):
            continue
        p = _file_uri_to_path(folder_uri)
        if p is None:
            continue
        out.append(p)
    return out


def discover_agent_workspaces(
    agent_dirs: Optional[CursorAgentDirs] = None,
    *,
    include_unknown_hashes: bool = True,
    workspace_candidates: Optional[Sequence[Path]] = None,
) -> List[AgentWorkspace]:
    """
    Discover workspaces that have cursor-agent chats.

    Workspaces are buckets under:
      ~/.cursor/chats/<md5(cwd)>

    We try to map hashes back to real paths using candidate folders
    (Cursor GUI recents by default). Unknown hashes remain selectable but
    cannot be resumed safely without the original workspace path.
    """
    agent_dirs = agent_dirs or get_cursor_agent_dirs()
    chats_dir = agent_dirs.chats_dir
    if not chats_dir.exists():
        return []

    hash_dirs = [d for d in chats_dir.iterdir() if d.is_dir() and is_md5_hex(d.name)]
    hash_set = {d.name for d in hash_dirs}

    # Load persisted hash->path map (auto-learned).
    persisted = load_workspace_map(agent_dirs)
    persisted_paths: Dict[str, Path] = {}
    for h, v in persisted.workspaces.items():
        if h not in hash_set:
            continue
        p = v.get("path")
        if not isinstance(p, str):
            continue
        pp = Path(p).expanduser()
        # Only trust existing directories.
        if pp.exists() and pp.is_dir():
            persisted_paths[h] = pp

    # Default candidates: Cursor GUI recents + current working dir.
    candidates: List[Path] = []
    if workspace_candidates is not None:
        candidates.extend(list(workspace_candidates))
    else:
        try:
            user_dirs = get_cursor_user_dirs()
            candidates.extend(discover_recent_folders_from_cursor_gui(user_dirs))
        except Exception:
            pass
        try:
            candidates.append(Path.cwd())
        except Exception:
            pass

    # Map hashes to paths.
    hash_to_path: Dict[str, Path] = {}
    ordered_hashes: List[str] = []
    seen = set()

    # First apply persisted mappings.
    for h, p in persisted_paths.items():
        hash_to_path[h] = p

    for p in candidates:
        for h in workspace_hash_candidates(p):
            if h in hash_set and h not in hash_to_path:
                hash_to_path[h] = p
            if h in hash_set and h not in seen:
                ordered_hashes.append(h)
                seen.add(h)

    # Add remaining hashes.
    remaining = [h for h in hash_set if h not in seen]
    # Sort remaining by mtime of the hash dir (recent first).
    remaining.sort(
        key=lambda h: (chats_dir / h).stat().st_mtime if (chats_dir / h).exists() else 0.0,
        reverse=True,
    )
    ordered_hashes.extend(remaining)

    out: List[AgentWorkspace] = []
    for h in ordered_hashes:
        p = hash_to_path.get(h)
        out.append(AgentWorkspace(cwd_hash=h, workspace_path=p, chats_root=chats_dir / h))

    if not include_unknown_hashes:
        out = [w for w in out if w.workspace_path is not None]
    return out


def discover_agent_chats(workspace: AgentWorkspace, *, with_preview: bool = False) -> List[AgentChat]:
    """
    List cursor-agent chats for a workspace hash directory.
    """
    root = workspace.chats_root
    if not root.exists():
        return []

    out: List[AgentChat] = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        store_db = d / "store.db"
        meta = read_chat_meta(store_db)
        if meta is None:
            continue

        chat = AgentChat(
            chat_id=meta.agent_id,
            name=meta.name,
            created_at_ms=meta.created_at_ms,
            mode=meta.mode,
            latest_root_blob_id=meta.latest_root_blob_id,
            store_db_path=store_db,
        )
        if with_preview and meta.latest_root_blob_id:
            role, text = extract_last_message_preview(store_db, meta.latest_root_blob_id)
            chat = AgentChat(
                **{**chat.__dict__, "last_role": role, "last_text": text}  # type: ignore[arg-type]
            )
        out.append(chat)

    def sort_key(c: AgentChat) -> Tuple[int, float]:
        created = c.created_at_ms or 0
        try:
            mtime = c.store_db_path.stat().st_mtime
        except Exception:
            mtime = 0.0
        return (created, mtime)

    out.sort(key=sort_key, reverse=True)
    return out

