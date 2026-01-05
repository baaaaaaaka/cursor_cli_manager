from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import curses

from cursor_cli_manager.agent_discovery import discover_agent_chats, discover_agent_workspaces
from cursor_cli_manager.agent_paths import ENV_CURSOR_AGENT_CONFIG_DIR, CursorAgentDirs, get_cursor_agent_dirs
from cursor_cli_manager.agent_store import extract_recent_messages, format_messages_preview
from cursor_cli_manager.models import AgentChat, AgentWorkspace
from cursor_cli_manager.opening import build_resume_command, exec_resume_chat, resolve_cursor_agent_path
from cursor_cli_manager.agent_workspace_map import (
    learn_workspace_path,
    load_workspace_map,
    try_learn_current_cwd,
    workspace_map_path,
)
from cursor_cli_manager.tui import select_chat


def _workspace_to_json(ws: AgentWorkspace) -> Dict[str, Any]:
    return {
        "cwd_hash": ws.cwd_hash,
        "workspace_path": str(ws.workspace_path) if ws.workspace_path else None,
        "display_name": ws.display_name,
        "chats_root": str(ws.chats_root),
    }


def _chat_to_json(c: AgentChat) -> Dict[str, Any]:
    d = asdict(c)
    # Normalize pathlib.Path for JSON output.
    if "store_db_path" in d and d["store_db_path"] is not None:
        d["store_db_path"] = str(d["store_db_path"])
    return d


def cmd_list(agent_dirs: CursorAgentDirs, *, pretty: bool, with_preview: bool) -> int:
    workspaces = discover_agent_workspaces(agent_dirs)
    payload: List[Dict[str, Any]] = []
    for ws in workspaces:
        chats = discover_agent_chats(ws, with_preview=with_preview)
        payload.append({"workspace": _workspace_to_json(ws), "chats": [_chat_to_json(c) for c in chats]})
    txt = json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None)
    sys.stdout.write(txt + "\n")
    return 0


def cmd_doctor(agent_dirs: CursorAgentDirs) -> int:
    print("Cursor Agent Chat Manager doctor")
    print("")
    print(f"Config dir: {agent_dirs.config_dir}")
    print(f"- Exists: {agent_dirs.config_dir.exists()}")
    print(f"Chats dir: {agent_dirs.chats_dir}")
    print(f"- Exists: {agent_dirs.chats_dir.exists()}")
    ws_map = load_workspace_map(agent_dirs)
    ws_map_file = workspace_map_path(agent_dirs)
    print(f"Workspace map: {ws_map_file}")
    print(f"- Exists: {ws_map_file.exists()}")
    print(f"- Entries: {len(ws_map.workspaces)}")

    agent = resolve_cursor_agent_path()
    print("")
    print("Cursor Agent:")
    print(f"- cursor-agent: {agent or 'NOT FOUND'}")
    print(f"- Tip: set ${ENV_CURSOR_AGENT_CONFIG_DIR} to override config dir")

    try:
        workspaces = discover_agent_workspaces(agent_dirs)
        total_chats = sum(len(discover_agent_chats(ws)) for ws in workspaces)
        print("")
        print(f"Discovered workspaces: {len(workspaces)}")
        print(f"Discovered chats: {total_chats}")
        for ws in workspaces[:20]:
            extra = f" ({ws.workspace_path})" if ws.workspace_path else ""
            print(f"- {ws.display_name}{extra}")
        if len(workspaces) > 20:
            print(f"  ... and {len(workspaces) - 20} more")
    except Exception as e:
        print("")
        print(f"Discovery failed: {e}")

    return 0


def cmd_open(agent_dirs: CursorAgentDirs, chat_id: str, *, workspace_path: Optional[Path], dry_run: bool) -> int:
    if workspace_path is None:
        print("Error: --workspace is required (cursor-agent chats are grouped by cwd).")
        return 2
    # Learning from explicit workspace path improves mapping without requiring user to cd first.
    learn_workspace_path(agent_dirs, workspace_path)
    cmd = build_resume_command(chat_id, workspace_path=workspace_path)
    if dry_run:
        # Display as a shell-friendly snippet.
        print(f"cd {workspace_path} && " + " ".join(cmd))
        return 0
    exec_resume_chat(chat_id, workspace_path=workspace_path)
    return 0  # unreachable


def _run_tui(
    agent_dirs: CursorAgentDirs,
    workspaces: List[AgentWorkspace],
) -> Optional[Tuple[AgentWorkspace, AgentChat]]:
    def _inner(stdscr: "curses.window") -> Optional[Tuple[AgentWorkspace, AgentChat]]:
        return select_chat(
            stdscr,
            workspaces=workspaces,
            load_chats=lambda ws: discover_agent_chats(ws, with_preview=False),
            load_preview=lambda chat: (
                (
                    "history",
                    format_messages_preview(extract_recent_messages(chat.store_db_path, max_messages=8)),
                )
                if chat.latest_root_blob_id
                else (None, None)
            ),
        )

    return curses.wrapper(_inner)


def cmd_tui(agent_dirs: CursorAgentDirs) -> int:
    workspaces = discover_agent_workspaces(agent_dirs)
    if not workspaces:
        print("No cursor-agent chats found.")
        print("Tip: run cursor-agent inside a folder to create chats, then re-run this manager.")
        return 1

    selection = _run_tui(agent_dirs, workspaces)
    if not selection:
        return 0
    ws, chat = selection
    if ws.workspace_path is None:
        print("Error: selected workspace has unknown path; cannot safely resume.")
        print("Tip: run the manager from that folder so the workspace can be identified.")
        return 1
    exec_resume_chat(chat.chat_id, workspace_path=ws.workspace_path)
    return 0  # unreachable


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="ccm", description="Cursor Agent Chat Manager (terminal-only)")
    parser.add_argument(
        "--config-dir",
        dest="config_dir",
        default=None,
        help=f"Override cursor-agent config dir (or set ${ENV_CURSOR_AGENT_CONFIG_DIR}).",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("tui", help="Start the interactive TUI (default).")

    p_list = sub.add_parser("list", help="Print workspaces and chats as JSON.")
    p_list.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    p_list.add_argument("--with-preview", action="store_true", help="Include last message preview (slower).")

    sub.add_parser("doctor", help="Print diagnostics.")

    p_open = sub.add_parser("open", help="Resume a cursor-agent chat session by chatId.")
    p_open.add_argument("chat_id")
    p_open.add_argument("--workspace", dest="workspace", default=None, help="Workspace folder path (required).")
    p_open.add_argument("--dry-run", action="store_true", help="Print command instead of executing.")

    args = parser.parse_args(argv)

    if args.config_dir:
        agent_dirs = CursorAgentDirs(Path(args.config_dir).expanduser())
    else:
        agent_dirs = get_cursor_agent_dirs()

    # Auto-learn mapping from md5(cwd) -> cwd path for better workspace naming.
    try_learn_current_cwd(agent_dirs)

    cmd = args.command or "tui"
    if cmd == "tui":
        return cmd_tui(agent_dirs)
    if cmd == "list":
        return cmd_list(agent_dirs, pretty=bool(args.pretty), with_preview=bool(args.with_preview))
    if cmd == "doctor":
        return cmd_doctor(agent_dirs)
    if cmd == "open":
        ws_path = Path(args.workspace).expanduser() if args.workspace else None
        return cmd_open(agent_dirs, args.chat_id, workspace_path=ws_path, dry_run=bool(args.dry_run))

    parser.print_help()
    return 2

