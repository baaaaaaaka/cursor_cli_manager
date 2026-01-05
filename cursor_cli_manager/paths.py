from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


ENV_CURSOR_USER_DATA_DIR = "CURSOR_USER_DATA_DIR"


@dataclass(frozen=True)
class CursorUserDirs:
    user_dir: Path

    @property
    def global_storage_dir(self) -> Path:
        return self.user_dir / "globalStorage"

    @property
    def global_state_vscdb(self) -> Path:
        return self.global_storage_dir / "state.vscdb"

    @property
    def workspace_storage_dir(self) -> Path:
        return self.user_dir / "workspaceStorage"


def _candidate_user_dirs_for_platform(system: str) -> Iterable[Path]:
    home = Path.home()

    if system == "Darwin":
        yield home / "Library" / "Application Support" / "Cursor" / "User"
        return

    if system == "Linux":
        # Cursor uses VS Code-style layout on Linux.
        yield home / ".config" / "Cursor" / "User"
        yield home / ".config" / "cursor" / "User"
        return

    # Unsupported OS (still return something deterministic for doctor output).
    yield home / ".config" / "Cursor" / "User"


def get_cursor_user_dirs() -> CursorUserDirs:
    """
    Resolve Cursor's `User` directory.

    Resolution order:
    - $CURSOR_USER_DATA_DIR (explicit override)
    - OS-specific default location(s)
    """
    override = os.environ.get(ENV_CURSOR_USER_DATA_DIR)
    if override:
        return CursorUserDirs(Path(override).expanduser())

    system = platform.system()
    for p in _candidate_user_dirs_for_platform(system):
        if p.exists():
            return CursorUserDirs(p)

    # Fall back to the first candidate (even if missing) so doctor can explain why.
    first = next(iter(_candidate_user_dirs_for_platform(system)))
    return CursorUserDirs(first)


def first_existing(paths: Iterable[Path]) -> Optional[Path]:
    for p in paths:
        if p.exists():
            return p
    return None

