from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from cursor_cli_manager.agent_paths import CursorAgentDirs


CHAT_TITLE_CACHE_FILENAME = "ccm-chat-titles.json"


def is_generic_chat_name(name: str) -> bool:
    n = (name or "").strip().lower()
    return n in ("new agent", "untitled")


@dataclass(frozen=True)
class ChatTitleCache:
    version: int
    # workspaces[cwd_hash][chat_id] = {"title": str, "updated_ms": int}
    workspaces: Dict[str, Dict[str, Dict[str, Any]]]


def chat_title_cache_path(agent_dirs: CursorAgentDirs) -> Path:
    return agent_dirs.config_dir / CHAT_TITLE_CACHE_FILENAME


def chat_title_cache_path_from_config_dir(config_dir: Path) -> Path:
    return config_dir / CHAT_TITLE_CACHE_FILENAME


def load_chat_title_cache(config_dir: Path) -> ChatTitleCache:
    p = chat_title_cache_path_from_config_dir(config_dir)
    if not p.exists():
        return ChatTitleCache(version=1, workspaces={})
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return ChatTitleCache(version=1, workspaces={})
    if not isinstance(obj, dict):
        return ChatTitleCache(version=1, workspaces={})

    ver = obj.get("version", 1)
    if not isinstance(ver, int):
        ver = 1

    ws_obj = obj.get("workspaces", {})
    if not isinstance(ws_obj, dict):
        return ChatTitleCache(version=ver, workspaces={})

    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for ws_hash, chats in ws_obj.items():
        if not isinstance(ws_hash, str) or not isinstance(chats, dict):
            continue
        m: Dict[str, Dict[str, Any]] = {}
        for chat_id, entry in chats.items():
            if not isinstance(chat_id, str) or not isinstance(entry, dict):
                continue
            title = entry.get("title")
            if not isinstance(title, str) or not title.strip():
                continue
            updated_ms = entry.get("updated_ms", 0)
            if not isinstance(updated_ms, int):
                updated_ms = 0
            m[chat_id] = {"title": title, "updated_ms": updated_ms}
        if m:
            out[ws_hash] = m

    return ChatTitleCache(version=ver, workspaces=out)


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def save_chat_title_cache(config_dir: Path, cache: ChatTitleCache) -> None:
    p = chat_title_cache_path_from_config_dir(config_dir)
    payload = {"version": cache.version, "workspaces": cache.workspaces}
    _atomic_write_json(p, payload)


def get_cached_title(cache: ChatTitleCache, *, cwd_hash: str, chat_id: str) -> Optional[str]:
    try:
        entry = cache.workspaces.get(cwd_hash, {}).get(chat_id)
        if not isinstance(entry, dict):
            return None
        title = entry.get("title")
        return title if isinstance(title, str) and title.strip() else None
    except Exception:
        return None


def set_cached_title(cache: ChatTitleCache, *, cwd_hash: str, chat_id: str, title: str) -> None:
    t = (title or "").strip()
    if not t:
        return
    now_ms = int(time.time() * 1000)
    ws = cache.workspaces.get(cwd_hash)
    if ws is None:
        ws = {}
        cache.workspaces[cwd_hash] = ws
    ws[chat_id] = {"title": t, "updated_ms": now_ms}

