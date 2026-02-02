from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if sys.platform.startswith("win"):
    from cursor_cli_manager.windows_deps import ensure_windows_deps

    ensure_windows_deps()

import curses

from cursor_cli_manager import __version__
from cursor_cli_manager.agent_discovery import discover_agent_chats, discover_agent_workspaces
from cursor_cli_manager.agent_paths import (
    ENV_CURSOR_AGENT_CONFIG_DIR,
    CursorAgentDirs,
    get_cursor_agent_dirs,
    workspace_hash_candidates,
)
from cursor_cli_manager.agent_store import extract_recent_messages, format_messages_preview
from cursor_cli_manager.models import AgentChat, AgentWorkspace
from cursor_cli_manager.agent_store import extract_initial_messages
from cursor_cli_manager.ccm_config import has_legacy_install, record_installed_version
from cursor_cli_manager.opening import (
    build_resume_command,
    exec_new_chat,
    exec_resume_chat,
    resolve_cursor_agent_path,
    start_cursor_agent_flag_probe,
)
from cursor_cli_manager.agent_patching import (
    ENV_CCM_CURSOR_AGENT_VERSIONS_DIR,
    ENV_CCM_PATCH_CURSOR_AGENT_MODELS,
    ENV_CURSOR_AGENT_VERSIONS_DIR,
    patch_cursor_agent_models,
    resolve_cursor_agent_versions_dir,
    should_patch_models,
)
from cursor_cli_manager.agent_workspace_map import (
    learn_workspace_path,
    load_workspace_map,
    try_learn_current_cwd,
    workspace_map_path,
)
from cursor_cli_manager.exporting import write_text_file
from cursor_cli_manager.tui import (
    ExportPendingExit,
    UpdateRequested,
    disable_xon_xoff_flow_control,
    force_exit_alternate_screen,
    probe_synchronized_output_support,
    restore_termios,
    select_chat,
)
from cursor_cli_manager.update import perform_update
from cursor_cli_manager.update import preferred_linux_asset_switch


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

    vdir = resolve_cursor_agent_versions_dir(cursor_agent_path=agent)
    print(f"- versions dir: {vdir or 'NOT FOUND'}")
    if vdir is not None:
        if has_legacy_install(agent_dirs):
            # Doctor should not modify anything; just report best-effort patchability.
            rep = patch_cursor_agent_models(versions_dir=vdir, dry_run=True)
            print(
                f"- model patch dry-run: would_patch={len(rep.patched_files)} already_patched={rep.skipped_already_patched} "
                f"not_applicable={rep.skipped_not_applicable} errors={len(rep.errors)}"
            )
        else:
            print("- model patch dry-run: disabled (no legacy history)")

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


def cmd_upgrade(*, python: str) -> int:
    ok, out = perform_update(python=python)
    if out:
        print(out)
    return 0 if ok else 1


def cmd_open(
    agent_dirs: CursorAgentDirs,
    chat_id: str,
    *,
    workspace_path: Optional[Path],
    dry_run: bool,
    patch_models: bool = False,
    cursor_agent_versions_dir: Optional[str] = None,
    force_patch_models: bool = False,
) -> int:
    if workspace_path is None:
        print("Error: --workspace is required (cursor-agent chats are grouped by cwd).")
        return 2
    # Learning from explicit workspace path improves mapping without requiring user to cd first.
    learn_workspace_path(agent_dirs, workspace_path)
    if patch_models and not dry_run:
        vdir = resolve_cursor_agent_versions_dir(
            explicit=cursor_agent_versions_dir,
            cursor_agent_path=resolve_cursor_agent_path(),
        )
        if vdir is not None:
            patch_cursor_agent_models(versions_dir=vdir, dry_run=False, force=force_patch_models)
    cmd = build_resume_command(chat_id, workspace_path=workspace_path, agent_dirs=agent_dirs)
    if dry_run:
        # Display as a shell-friendly snippet.
        print(f"cd {workspace_path} && " + " ".join(cmd))
        return 0
    exec_resume_chat(chat_id, workspace_path=workspace_path, agent_dirs=agent_dirs)
    return 0  # unreachable


def _run_tui(
    agent_dirs: CursorAgentDirs,
    workspaces: List[AgentWorkspace],
) -> Optional[Tuple[AgentWorkspace, Optional[AgentChat]]]:
    _prepare_curses_term_for_tui()

    # Best-effort: enable xterm synchronized output only when we can confirm support.
    # This can reduce flicker on some terminals, and is a no-op when disabled.
    sync_output = False
    try:
        sync_output = probe_synchronized_output_support(timeout_s=0.05)
    except Exception:
        sync_output = False

    def _inner(stdscr: "curses.window") -> Optional[Tuple[AgentWorkspace, Optional[AgentChat]]]:
        return select_chat(
            stdscr,
            workspaces=workspaces,
            load_chats=lambda ws: discover_agent_chats(ws, with_preview=False),
            load_preview_snippet=lambda chat, max_messages: (
                (
                    "history",
                    format_messages_preview(
                        extract_initial_messages(chat.store_db_path, max_messages=max_messages, max_blobs=None),
                        max_chars_per_message=0,
                    ),
                )
                if chat.latest_root_blob_id
                else (None, None)
            ),
            load_preview_full=lambda chat: (
                (
                    "history",
                    format_messages_preview(
                        extract_recent_messages(chat.store_db_path, max_messages=None, max_blobs=None),
                        max_chars_per_message=0,
                    ),
                )
                if chat.latest_root_blob_id
                else (None, None)
            ),
            sync_output=sync_output,
        )

    flow_saved = disable_xon_xoff_flow_control()
    try:
        return curses.wrapper(_inner)
    finally:
        restore_termios(flow_saved)
        force_exit_alternate_screen()


def _restart_self(argv: List[str]) -> int:
    argv = argv[:] or ["ccm"]
    restart_errors: List[BaseException] = []
    try:
        os.execvp(argv[0], argv)
    except BaseException as e:
        restart_errors.append(e)

    # Fallback: run via python -m for editable/dev installs only.
    if not getattr(sys, "frozen", False):
        try:
            os.execv(sys.executable, [sys.executable, "-m", "cursor_cli_manager"] + argv[1:])
        except BaseException as e:
            restart_errors.append(e)

    # If we couldn't restart, don't crash. Tell the user to restart manually.
    print("Upgrade installed, but failed to restart automatically.", file=sys.stderr)
    if restart_errors:
        last = restart_errors[-1]
        try:
            eno = getattr(last, "errno", None)
        except Exception:
            eno = None
        if eno == 40 or "Too many levels of symbolic links" in str(last):
            print("It looks like your `ccm` symlink is broken (symbolic link loop).", file=sys.stderr)
            print("Fix: re-run the installer or remove the broken symlink and reinstall.", file=sys.stderr)
    print("Please re-run `ccm` manually.", file=sys.stderr)
    return 0


def _prepare_curses_term_for_tui() -> None:
    """
    Best-effort terminal preflight before starting curses.

    Motivation:
    - Some remote systems (HPC/login nodes) don't have terminfo entries for modern
      $TERM values (e.g. "xterm-kitty", "wezterm").
    - PyInstaller one-file binaries may run on systems without a usable terminfo DB.

    We:
    - Try the current $TERM and a few common fallbacks.
    - If all fail and we are running as a frozen binary with bundled terminfo,
      set TERMINFO to the bundled database and retry.
    """
    try:
        current_term = (os.environ.get("TERM") or "").strip()
        candidates: List[str] = []
        if current_term:
            candidates.append(current_term)
        candidates.extend(["xterm-256color", "xterm", "screen-256color", "screen", "vt100", "linux"])

        def _try_terms() -> bool:
            seen = set()
            for t in candidates:
                if not t or t in seen:
                    continue
                seen.add(t)
                try:
                    curses.setupterm(term=t, fd=1)
                    if t != current_term:
                        os.environ["TERM"] = t
                    return True
                except Exception:
                    continue
            return False

        if _try_terms():
            return

        # If we have a bundled terminfo DB (PyInstaller), try again using it.
        bundled = None
        try:
            if getattr(sys, "frozen", False):
                mp = getattr(sys, "_MEIPASS", None)
                if isinstance(mp, str) and mp:
                    p = Path(mp) / "terminfo"
                    if p.is_dir():
                        bundled = str(p)
                if bundled is None:
                    try:
                        p2 = Path(sys.executable).resolve().parent / "terminfo"
                        if p2.is_dir():
                            bundled = str(p2)
                    except Exception:
                        bundled = None
        except Exception:
            bundled = None

        if bundled and not os.environ.get("TERMINFO"):
            os.environ["TERMINFO"] = bundled
            _try_terms()
    except Exception:
        # If this preflight fails, curses.wrapper() will surface the error.
        return


def _pin_cwd_workspace(agent_dirs: CursorAgentDirs, workspaces: List[AgentWorkspace]) -> List[AgentWorkspace]:
    """
    Ensure the current working directory is always the first workspace in the TUI,
    even if it has no chats yet.
    """
    try:
        cwd = Path.cwd()
    except Exception:
        return workspaces

    candidates = list(workspace_hash_candidates(cwd))
    chosen_hash: Optional[str] = None
    existing = {w.cwd_hash: w for w in workspaces}
    for h in candidates:
        if h in existing:
            chosen_hash = h
            break
    if chosen_hash is None:
        chosen_hash = candidates[0] if candidates else ""

    if not chosen_hash:
        return workspaces

    cwd_ws = AgentWorkspace(
        cwd_hash=chosen_hash,
        workspace_path=cwd,
        chats_root=agent_dirs.chats_dir / chosen_hash,
    )
    rest = [w for w in workspaces if w.cwd_hash != chosen_hash]
    return [cwd_ws] + rest


def cmd_tui(
    agent_dirs: CursorAgentDirs,
    *,
    patch_models: bool = False,
    cursor_agent_versions_dir: Optional[str] = None,
    force_patch_models: bool = False,
) -> int:
    preferred_asset = preferred_linux_asset_switch()
    if preferred_asset:
        ok, out = perform_update(python=sys.executable, asset_name=preferred_asset)
        if out:
            print(out)
        if ok:
            return _restart_self(sys.argv[:] or ["ccm"])
        print(f"Auto-upgrade to {preferred_asset} failed; continuing.", file=sys.stderr)

    # Non-blocking: probe cursor-agent optional flags in background while the user browses the TUI.
    if has_legacy_install(agent_dirs):
        start_cursor_agent_flag_probe()
    if patch_models:
        vdir = resolve_cursor_agent_versions_dir(
            explicit=cursor_agent_versions_dir,
            cursor_agent_path=resolve_cursor_agent_path(),
        )
        if vdir is not None:
            patch_cursor_agent_models(versions_dir=vdir, dry_run=False, force=force_patch_models)

    # Hide chats whose original workspace folder no longer exists.
    workspaces = _pin_cwd_workspace(
        agent_dirs,
        discover_agent_workspaces(agent_dirs, exclude_missing_paths=True),
    )
    if not workspaces:
        print("No workspaces available.")
        return 1

    while True:
        try:
            selection = _run_tui(agent_dirs, workspaces)
            break
        except curses.error as e:
            term = os.environ.get("TERM")
            msg = str(e) or "curses error"
            print(f"Error: failed to initialize terminal UI: {msg}", file=sys.stderr)
            if term:
                print(f"Tip: your TERM is {term!r}. If this system lacks terminfo for it, try:", file=sys.stderr)
            else:
                print("Tip: TERM is not set. Try:", file=sys.stderr)
            print("  TERM=xterm-256color ccm", file=sys.stderr)
            print("  TERM=xterm ccm", file=sys.stderr)
            print("Tip: if you are running without a TTY, use `ccm list` or `ccm doctor`.", file=sys.stderr)
            return 2
        except ExportPendingExit as e:
            # Finish saving after curses exits so the user sees progress in the normal terminal.
            out_path = e.out_path
            print(f"Saving {out_path} ...")
            try:
                text = format_messages_preview(
                    extract_recent_messages(e.store_db_path, max_messages=None, max_blobs=None),
                    max_chars_per_message=0,
                )
                write_text_file(out_path, text)
                print(f"Saved {out_path}")
                return 0
            except Exception as ex:
                print(f"Save failed: {ex}")
                return 1
        except UpdateRequested:
            ok, out = perform_update(python=sys.executable)
            if out:
                print(out)
            if ok:
                # Restart the current command so the updated code is loaded.
                return _restart_self(sys.argv[:] or ["ccm"])
            # Pull failed; return to TUI.
            continue

    if not selection:
        return 0
    ws, chat = selection
    if ws.workspace_path is None:
        print("Error: selected workspace has unknown path; cannot safely resume.")
        print("Tip: run the manager from that folder so the workspace can be identified.")
        return 1
    if chat is None:
        exec_new_chat(workspace_path=ws.workspace_path, agent_dirs=agent_dirs)
        return 0  # unreachable
    exec_resume_chat(chat.chat_id, workspace_path=ws.workspace_path, agent_dirs=agent_dirs)
    return 0  # unreachable


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="ccm", description="Cursor Agent Chat Manager (terminal-only)")
    parser.add_argument(
        "--config-dir",
        dest="config_dir",
        default=None,
        help=f"Override cursor-agent config dir (or set ${ENV_CURSOR_AGENT_CONFIG_DIR}).",
    )
    p_patch_flag = parser.add_mutually_exclusive_group()
    p_patch_flag.add_argument(
        "--patch-models",
        dest="patch_models",
        action="store_true",
        default=None,
        help=f"Enable cursor-agent model patching (default; can also use ${ENV_CCM_PATCH_CURSOR_AGENT_MODELS}).",
    )
    p_patch_flag.add_argument(
        "--no-patch-models",
        dest="patch_models",
        action="store_false",
        default=None,
        help=f"Disable cursor-agent model patching (or set {ENV_CCM_PATCH_CURSOR_AGENT_MODELS}=0).",
    )
    parser.add_argument(
        "--cursor-agent-versions-dir",
        dest="cursor_agent_versions_dir",
        default=None,
        help=f"Override cursor-agent versions dir (or set ${ENV_CCM_CURSOR_AGENT_VERSIONS_DIR} / ${ENV_CURSOR_AGENT_VERSIONS_DIR}).",
    )
    parser.add_argument(
        "--force-patch-models",
        dest="force_patch_models",
        action="store_true",
        default=False,
        help="Force a full rescan of cursor-agent bundles (ignore patch cache).",
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

    p_patch = sub.add_parser("patch-models", help="Patch cursor-agent bundles to prefer AvailableModels.")
    p_patch.add_argument("--dry-run", action="store_true", help="Scan and report without writing.")
    p_patch.add_argument(
        "--force",
        dest="force_patch_models",
        action="store_true",
        default=False,
        help="Force a full rescan of cursor-agent bundles (ignore patch cache).",
    )

    sub.add_parser("upgrade", help="Upgrade ccm (VCS pip install or GitHub-release binary).")

    args = parser.parse_args(argv)

    if args.config_dir:
        agent_dirs = CursorAgentDirs(Path(args.config_dir).expanduser())
    else:
        agent_dirs = get_cursor_agent_dirs()

    record_installed_version(agent_dirs, __version__)

    # Auto-learn mapping from md5(cwd) -> cwd path for better workspace naming.
    try_learn_current_cwd(agent_dirs)

    cmd = args.command or "tui"
    if cmd == "tui":
        return cmd_tui(
            agent_dirs,
            patch_models=should_patch_models(
                agent_dirs=agent_dirs,
                explicit=getattr(args, "patch_models", None),
            ),
            cursor_agent_versions_dir=getattr(args, "cursor_agent_versions_dir", None),
            force_patch_models=bool(getattr(args, "force_patch_models", False)),
        )
    if cmd == "list":
        return cmd_list(agent_dirs, pretty=bool(args.pretty), with_preview=bool(args.with_preview))
    if cmd == "doctor":
        return cmd_doctor(agent_dirs)
    if cmd == "open":
        ws_path = Path(args.workspace).expanduser() if args.workspace else None
        return cmd_open(
            agent_dirs,
            args.chat_id,
            workspace_path=ws_path,
            dry_run=bool(args.dry_run),
            patch_models=should_patch_models(
                agent_dirs=agent_dirs,
                explicit=getattr(args, "patch_models", None),
            ),
            cursor_agent_versions_dir=getattr(args, "cursor_agent_versions_dir", None),
            force_patch_models=bool(getattr(args, "force_patch_models", False)),
        )
    if cmd == "patch-models":
        if not has_legacy_install(agent_dirs):
            return 0
        vdir = resolve_cursor_agent_versions_dir(
            explicit=getattr(args, "cursor_agent_versions_dir", None),
            cursor_agent_path=resolve_cursor_agent_path(),
        )
        if vdir is None:
            print("cursor-agent versions dir not found. Set --cursor-agent-versions-dir or $CCM_CURSOR_AGENT_VERSIONS_DIR.")
            return 2
        rep = patch_cursor_agent_models(
            versions_dir=vdir,
            dry_run=bool(getattr(args, "dry_run", False)),
            force=bool(getattr(args, "force_patch_models", False)),
        )
        print(f"versions dir: {vdir}")
        print(f"scanned_files: {rep.scanned_files}")
        print(f"patched_files: {len(rep.patched_files)}")
        print(f"already_patched: {rep.skipped_already_patched}")
        print(f"not_applicable: {rep.skipped_not_applicable}")
        print(f"cached_skips: {rep.skipped_cached}")
        if rep.errors:
            print(f"errors: {len(rep.errors)}")
            for p, e in rep.errors[:10]:
                print(f"- {p}: {e}")
        return 0 if rep.ok else 1
    if cmd == "upgrade":
        return cmd_upgrade(python=sys.executable)

    parser.print_help()
    return 2

