from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cursor_cli_manager.update import _default_runner


ENV_CURSOR_AGENT_PATH = "CURSOR_AGENT_PATH"

# Default cursor-agent flags we want enabled for interactive runs.
DEFAULT_CURSOR_AGENT_FLAGS = ["--approve-mcps", "--browser", "--force"]

_FORCE_DISABLED_RETRY_MESSAGE = "Detected 'Run Everything' restriction; retrying without '--force'."
_UNKNOWN_OPTION_RETRY_MESSAGE = "Detected unsupported cursor-agent option {flag!r}; retrying without it."

_FLAG_TOKEN_RE = re.compile(r"-{1,2}[A-Za-z][\w-]*")
_UNRECOGNIZED_ARGUMENTS_RE = re.compile(
    r"unrecognized (?:arguments?|options?)\s*:?\s*([^\n\r]+)", re.IGNORECASE
)
_OPTION_ERROR_PATTERNS = (
    re.compile(
        r"(?:unknown|unrecognized|invalid|unsupported|unexpected)\s+option(?:\s*[:])?\s*['\"]?(-{1,2}[A-Za-z][\w-]*)['\"]?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:unknown|unrecognized|invalid|unsupported|unexpected)\s+argument(?:\s*[:])?\s*['\"]?(-{1,2}[A-Za-z][\w-]*)['\"]?",
        re.IGNORECASE,
    ),
)


_PROBE_LOCK = threading.Lock()
_PROBE_STARTED = False
_PROBED_CURSOR_AGENT_FLAGS: Optional[List[str]] = None

_OPTION_SUPPORT_LOCK = threading.Lock()
_OPTION_SUPPORT_CACHE: Dict[Tuple[str, str], bool] = {}


def _help_supports_flag(help_text: str, flag: str) -> bool:
    # Match "--flag" as a standalone token in help output.
    # Allow common separators after a flag: whitespace, comma, "=", or "[".
    pat = r"(^|\s)" + re.escape(flag) + r"(\s|,|=|\[|$)"
    return bool(re.search(pat, help_text or "", flags=re.MULTILINE))


def _extract_unknown_option(stderr_text: str) -> Optional[str]:
    if not stderr_text:
        return None
    match = _UNRECOGNIZED_ARGUMENTS_RE.search(stderr_text)
    if match:
        segment = match.group(1)
        flag_match = _FLAG_TOKEN_RE.search(segment)
        if flag_match:
            return flag_match.group(0)
    for pat in _OPTION_ERROR_PATTERNS:
        match = pat.search(stderr_text)
        if match:
            return match.group(1)
    return None


def _command_contains_flag(cmd: List[str], flag: str) -> bool:
    if flag in cmd:
        return True
    if flag.startswith("--"):
        prefix = f"{flag}="
        return any(arg.startswith(prefix) for arg in cmd)
    return False


def _remove_flag_from_cmd(cmd: List[str], flag: str) -> List[str]:
    if not cmd:
        return []
    prefix = f"{flag}=" if flag.startswith("--") else None
    out: List[str] = []
    skip_next = False
    for idx, arg in enumerate(cmd):
        if skip_next:
            skip_next = False
            continue
        if arg == flag:
            if idx + 1 < len(cmd) and not cmd[idx + 1].startswith("-"):
                skip_next = True
            continue
        if prefix and arg.startswith(prefix):
            continue
        out.append(arg)
    return out


def start_cursor_agent_flag_probe(*, timeout_s: float = 1.0) -> None:
    """
    Best-effort, non-blocking probe to detect which optional flags are supported.

    This is intentionally async: if the probe hasn't finished when the user opens
    a chat, we do NOT block.
    """
    global _PROBE_STARTED
    with _PROBE_LOCK:
        if _PROBE_STARTED:
            return
        _PROBE_STARTED = True

    def _run() -> None:
        global _PROBED_CURSOR_AGENT_FLAGS
        try:
            agent = resolve_cursor_agent_path()
            if not agent:
                return
            rc, out, err = _default_runner([agent, "--help"], timeout_s)
            if rc != 0:
                # Leave as unknown; we will keep using defaults.
                return
            txt = (out or "") + ("\n" if out and err else "") + (err or "")
            supported: List[str] = []
            for flag in DEFAULT_CURSOR_AGENT_FLAGS:
                if _help_supports_flag(txt, flag):
                    supported.append(flag)
            _PROBED_CURSOR_AGENT_FLAGS = supported
        except Exception:
            return

    threading.Thread(target=_run, daemon=True).start()


def get_cursor_agent_flags() -> List[str]:
    """
    Optional flags to pass to cursor-agent (best-effort).
    """
    probed = _PROBED_CURSOR_AGENT_FLAGS
    return list(probed) if probed is not None else list(DEFAULT_CURSOR_AGENT_FLAGS)


def _supports_optional_flag(agent: str, flag: str, *, timeout_s: float = 1.0) -> bool:
    """
    Best-effort check whether the installed cursor-agent supports a flag.
    """
    if not agent or not flag:
        return False
    key = (agent, flag)
    cached = _OPTION_SUPPORT_CACHE.get(key)
    if cached is not None:
        return cached
    with _OPTION_SUPPORT_LOCK:
        cached = _OPTION_SUPPORT_CACHE.get(key)
        if cached is not None:
            return cached
        ok = False
        try:
            rc, _out, _err = _default_runner([agent, flag, "--help"], timeout_s)
            ok = rc == 0
        except Exception:
            ok = False
        _OPTION_SUPPORT_CACHE[key] = ok
        return ok


def _without_force_flag(cmd: List[str]) -> List[str]:
    return [c for c in cmd if c not in ("--force", "-f")]


def _filter_supported_optional_flags(cmd: List[str]) -> List[str]:
    if not cmd:
        return []
    agent = cmd[0]
    if not agent:
        return cmd
    filtered = list(cmd)
    for flag in DEFAULT_CURSOR_AGENT_FLAGS:
        if _command_contains_flag(filtered, flag) and not _supports_optional_flag(agent, flag):
            filtered = _remove_flag_from_cmd(filtered, flag)
            if flag == "--force" and _command_contains_flag(filtered, "-f"):
                filtered = _remove_flag_from_cmd(filtered, "-f")
    return filtered


def _prepare_exec_command(cmd: List[str]) -> List[str]:
    """
    Apply last-mile compatibility tweaks before exec'ing cursor-agent.
    """
    return _filter_supported_optional_flags(cmd)


def _stderr_indicates_force_disabled(stderr_text: str) -> bool:
    text = (stderr_text or "").lower()
    return "run everything" in text and "disabled" in text and "--force" in text


def _run_cursor_agent(cmd: List[str]) -> Tuple[int, str]:
    """
    Run cursor-agent with stderr mirrored to the terminal.
    Returns (exit_code, captured_stderr).
    """
    p = subprocess.Popen(
        list(cmd),
        stdin=None,
        stdout=None,
        stderr=subprocess.PIPE,
        text=True,
    )
    stderr_chunks: List[str] = []

    def _drain_stderr() -> None:
        if p.stderr is None:
            return
        while True:
            chunk = p.stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)
            try:
                sys.stderr.write(chunk)
                sys.stderr.flush()
            except Exception:
                pass

    t = threading.Thread(target=_drain_stderr, daemon=True)
    t.start()
    try:
        rc = p.wait()
    except KeyboardInterrupt:
        try:
            p.send_signal(signal.SIGINT)
        except Exception:
            pass
        rc = p.wait()
    t.join(timeout=1.0)
    return rc or 0, "".join(stderr_chunks)


def _exec_cursor_agent(cmd: List[str]) -> "os.NoReturn":
    if "--force" in cmd or "-f" in cmd:
        retry_cmd = list(cmd)
        while True:
            rc, err = _run_cursor_agent(retry_cmd)
            if rc == 0:
                raise SystemExit(0)
            if _stderr_indicates_force_disabled(err) and ("--force" in retry_cmd or "-f" in retry_cmd):
                retry_cmd = _without_force_flag(retry_cmd)
                try:
                    print(_FORCE_DISABLED_RETRY_MESSAGE, file=sys.stderr, flush=True)
                except Exception:
                    pass
                os.execvp(retry_cmd[0], retry_cmd)
            bad_flag = _extract_unknown_option(err)
            if bad_flag and bad_flag in DEFAULT_CURSOR_AGENT_FLAGS and _command_contains_flag(retry_cmd, bad_flag):
                retry_cmd = _remove_flag_from_cmd(retry_cmd, bad_flag)
                try:
                    print(_UNKNOWN_OPTION_RETRY_MESSAGE.format(flag=bad_flag), file=sys.stderr, flush=True)
                except Exception:
                    pass
                if "--force" not in retry_cmd and "-f" not in retry_cmd:
                    os.execvp(retry_cmd[0], retry_cmd)
                continue
            raise SystemExit(rc)
    os.execvp(cmd[0], cmd)


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
    cmd.extend(get_cursor_agent_flags())
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
    cmd.extend(get_cursor_agent_flags())
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
    cmd = _prepare_exec_command(cmd)
    try:
        ws = f" in {workspace_path}" if workspace_path is not None else ""
        print(f"Launching cursor-agent{ws}… (resume {chat_id})", file=sys.stderr, flush=True)
    except Exception:
        pass
    _exec_cursor_agent(cmd)


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
    cmd = _prepare_exec_command(cmd)
    try:
        ws = f" in {workspace_path}" if workspace_path is not None else ""
        print(f"Launching cursor-agent{ws}…", file=sys.stderr, flush=True)
    except Exception:
        pass
    _exec_cursor_agent(cmd)

