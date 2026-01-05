from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class AgentWorkspace:
    """
    A workspace (folder) bucket for cursor-agent chats.

    cursor-agent stores chats under ~/.cursor/chats/<md5(cwd)>/<chatId>/store.db
    """

    cwd_hash: str
    workspace_path: Optional[Path]
    chats_root: Path

    @property
    def display_name(self) -> str:
        if self.workspace_path is None:
            return f"Unknown ({self.cwd_hash})"
        name = self.workspace_path.name
        return name if name else str(self.workspace_path)


@dataclass(frozen=True)
class AgentChat:
    chat_id: str
    name: str
    created_at_ms: Optional[int]
    mode: Optional[str]
    latest_root_blob_id: Optional[str]
    store_db_path: Path

    # Best-effort preview fields (can be filled lazily).
    last_role: Optional[str] = None
    last_text: Optional[str] = None

