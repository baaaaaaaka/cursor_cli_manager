from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


ENV_CURSOR_AGENT_CONFIG_DIR = "CURSOR_AGENT_CONFIG_DIR"
ENV_CURSOR_CONFIG_DIR = "CURSOR_CONFIG_DIR"


@dataclass(frozen=True)
class CursorAgentDirs:
    config_dir: Path

    @property
    def chats_dir(self) -> Path:
        return self.config_dir / "chats"


def get_cursor_agent_dirs() -> CursorAgentDirs:
    for name in (ENV_CURSOR_AGENT_CONFIG_DIR, ENV_CURSOR_CONFIG_DIR):
        override = os.environ.get(name)
        if override and override.strip():
            return CursorAgentDirs(Path(override).expanduser().absolute())
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config and xdg_config.strip():
        return CursorAgentDirs(Path(xdg_config).expanduser().absolute() / "cursor")
    return CursorAgentDirs(Path.home() / ".cursor")


def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def workspace_hash_candidates(workspace_path: Path) -> Iterable[str]:
    """
    cursor-agent groups chats by md5(process.cwd()).

    process.cwd() is typically an absolute path, but can vary with symlinks.
    We return a few common candidates to increase match rate.
    """
    p = workspace_path.expanduser()
    # Preserve the original string form (absolute if possible).
    yield md5_hex(str(p))
    try:
        yield md5_hex(str(p.resolve()))
    except Exception:
        return


def is_md5_hex(s: str) -> bool:
    return len(s) == 32 and all(c in "0123456789abcdef" for c in s.lower())

