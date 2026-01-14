from __future__ import annotations

import datetime as _dt
import os
import re
from pathlib import Path
from typing import Optional, Tuple


_INVALID_CHARS_RE = re.compile(r'[\/\\\:\*\?\"\<\>\|\x00-\x1f]')
_WS_RE = re.compile(r"\s+")
_UNDERSCORE_RUN_RE = re.compile(r"_+")


def sanitize_filename_component(s: str, *, max_len: int = 80) -> str:
    """
    Sanitize a string to be safe as a filename component across common platforms.

    - Replaces reserved characters (/, \\, :, *, ?, ", <, >, |) and control chars with "_"
    - Collapses whitespace
    - Trims leading/trailing spaces and dots
    - Enforces max_len (by character count, not display width)
    """
    txt = (s or "").strip()
    if not txt:
        return ""
    txt = _INVALID_CHARS_RE.sub("_", txt)
    # Prefer underscores over spaces in filenames.
    txt = _WS_RE.sub("_", txt)
    # Collapse common separator runs.
    txt = _UNDERSCORE_RUN_RE.sub("_", txt)
    # Trim separators that are awkward at ends.
    txt = txt.strip(" ._")
    if not txt:
        return ""
    if max_len > 0 and len(txt) > max_len:
        txt = txt[:max_len].rstrip(" ._")
    return txt


def build_export_filename(
    *,
    title: str,
    when: Optional[_dt.datetime] = None,
    ext: str = ".md",
    max_title_len: int = 80,
) -> str:
    when = when or _dt.datetime.now()
    ts = when.strftime("%Y-%m-%d_%H-%M-%S")
    safe_title = sanitize_filename_component(title, max_len=max_title_len) or "chat"
    e = ext if ext.startswith(".") else ("." + ext)
    return f"{ts}_{safe_title}{e}"


def choose_nonconflicting_path(dir_path: Path, filename: str, *, max_tries: int = 999) -> Path:
    """
    If `<dir>/<filename>` exists, append `-2`, `-3`, ... before the extension.
    """
    base = dir_path / filename
    if not base.exists():
        return base
    stem = base.stem
    suffix = base.suffix
    for i in range(2, max_tries + 1):
        cand = dir_path / f"{stem}-{i}{suffix}"
        if not cand.exists():
            return cand
    # Last resort: still return something deterministic.
    return dir_path / f"{stem}-{max_tries}{suffix}"


def write_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (text or "").rstrip() + "\n"
    path.write_text(data, encoding="utf-8")


def tab_complete_path(text: str, cursor: int, *, cwd: Optional[Path] = None) -> Tuple[str, int]:
    """
    Basic filesystem tab completion for a path input field.

    Only completes based on the prefix before `cursor`.
    """
    cwd = cwd or Path.cwd()
    cursor = max(0, min(cursor, len(text)))
    prefix = text[:cursor]
    suffix = text[cursor:]

    # Determine which separator is in use.
    sep = "/" if "/" in prefix else ("\\" if "\\" in prefix else os.sep)
    last_sep = max(prefix.rfind("/"), prefix.rfind("\\"))
    if last_sep >= 0:
        dir_part = prefix[: last_sep + 1]
        frag = prefix[last_sep + 1 :]
    else:
        dir_part = ""
        frag = prefix

    # Resolve base directory for listing.
    if dir_part:
        base = Path(dir_part).expanduser()
        if not base.is_absolute():
            base = (cwd / base).absolute()
    else:
        base = cwd

    try:
        entries = sorted(p.name for p in base.iterdir())
    except Exception:
        return text, cursor

    matches = [n for n in entries if n.startswith(frag)]
    if not matches:
        return text, cursor

    def _common_prefix(items: list[str]) -> str:
        if not items:
            return ""
        s = items[0]
        for it in items[1:]:
            j = 0
            while j < len(s) and j < len(it) and s[j] == it[j]:
                j += 1
            s = s[:j]
            if not s:
                break
        return s

    insert = ""
    if len(matches) == 1:
        insert = matches[0]
    else:
        cp = _common_prefix(matches)
        if len(cp) > len(frag):
            insert = cp
        else:
            return text, cursor

    new_prefix = dir_part + insert
    try:
        full = Path(new_prefix).expanduser()
        if not full.is_absolute():
            full = (cwd / full).absolute()
        if full.exists() and full.is_dir():
            new_prefix = new_prefix + sep
    except Exception:
        pass

    new_text = new_prefix + suffix
    new_cursor = len(new_prefix)
    return new_text, new_cursor

