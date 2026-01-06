from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import List, Optional


ENV_CURSOR_AGENT_PATH = "CURSOR_AGENT_PATH"


def resolve_cursor_agent_path(explicit: Optional[str] = None) -> Optional[str]:
    """
    Resolve the `cursor-agent` executable.

    Priority:
    - explicit arg
    - $CURSOR_AGENT_PATH
    - PATH lookup
    - ~/.local/bin/cursor-agent
    """
    if explicit:
        p = Path(explicit).expanduser()
        return str(p) if p.exists() else None

    env = os.environ.get(ENV_CURSOR_AGENT_PATH)
    if env:
        p = Path(env).expanduser()
        return str(p) if p.exists() else None

    found = shutil.which("cursor-agent")
    if found:
        return found

    default = Path.home() / ".local" / "bin" / "cursor-agent"
    if default.exists():
        return str(default)

    return None


def build_resume_command(
    chat_id: str,
    *,
    workspace_path: Optional[Path] = None,
    cursor_agent_path: Optional[str] = None,
) -> List[str]:
    agent = resolve_cursor_agent_path(cursor_agent_path)
    if not agent:
        raise RuntimeError("cursor-agent not found. Install it or set CURSOR_AGENT_PATH.")

    cmd: List[str] = [agent]
    if workspace_path is not None:
        cmd.extend(["--workspace", str(workspace_path)])
    cmd.extend(["--resume", chat_id])
    return cmd


def build_new_command(
    *,
    workspace_path: Optional[Path] = None,
    cursor_agent_path: Optional[str] = None,
) -> List[str]:
    """
    Build a command that starts a new cursor-agent chat session.
    """
    agent = resolve_cursor_agent_path(cursor_agent_path)
    if not agent:
        raise RuntimeError("cursor-agent not found. Install it or set CURSOR_AGENT_PATH.")

    cmd: List[str] = [agent]
    if workspace_path is not None:
        cmd.extend(["--workspace", str(workspace_path)])
    return cmd


def exec_resume_command(cmd: List[str]) -> "os.NoReturn":
    os.execvp(cmd[0], cmd)


def exec_resume_chat(
    chat_id: str,
    *,
    workspace_path: Optional[Path],
    cursor_agent_path: Optional[str] = None,
) -> "os.NoReturn":
    """
    Exec into cursor-agent and resume a chat session.

    Important: cursor-agent stores chats under ~/.cursor/chats/<md5(cwd)>,
    so we `chdir()` into the workspace path (when available) to ensure the
    correct chat store is used.
    """
    if workspace_path is not None:
        os.chdir(workspace_path)
    cmd = build_resume_command(chat_id, workspace_path=workspace_path, cursor_agent_path=cursor_agent_path)
    os.execvp(cmd[0], cmd)


def exec_new_chat(
    *,
    workspace_path: Optional[Path],
    cursor_agent_path: Optional[str] = None,
) -> "os.NoReturn":
    """
    Exec into cursor-agent and start a new chat session.

    Similar to `exec_resume_chat`, we `chdir()` into the workspace to ensure the
    correct ~/.cursor/chats/<md5(cwd)> bucket is used.
    """
    if workspace_path is not None:
        os.chdir(workspace_path)
    cmd = build_new_command(workspace_path=workspace_path, cursor_agent_path=cursor_agent_path)
    os.execvp(cmd[0], cmd)

