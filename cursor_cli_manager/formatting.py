from __future__ import annotations

import datetime as _dt
import unicodedata
from typing import Iterable, List, Optional


def _char_width(ch: str) -> int:
    """
    Best-effort terminal column width calculation without third-party deps.

    - Combining marks: width 0
    - East Asian Wide/Fullwidth: width 2
    - Everything else: width 1
    """
    if not ch:
        return 0
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def display_width(s: str) -> int:
    return sum(_char_width(ch) for ch in s)


def truncate_to_width(s: str, max_width: int, *, ellipsis: str = "…") -> str:
    if max_width <= 0:
        return ""
    if display_width(s) <= max_width:
        return s

    ell_w = display_width(ellipsis)
    if ell_w >= max_width:
        # Can't fit anything meaningful.
        return ellipsis[:1]

    out: List[str] = []
    used = 0
    for ch in s:
        w = _char_width(ch)
        if used + w > max_width - ell_w:
            break
        out.append(ch)
        used += w
    return "".join(out) + ellipsis


def wrap_text(s: str, width: int) -> List[str]:
    """
    Wrap text by display width (best-effort). Preserves existing newlines.
    """
    if width <= 0:
        return [""]

    lines: List[str] = []
    for raw_line in s.splitlines() or [""]:
        cur: List[str] = []
        cur_w = 0
        for ch in raw_line:
            w = _char_width(ch)
            if cur_w + w > width and cur:
                lines.append("".join(cur))
                cur = []
                cur_w = 0
            cur.append(ch)
            cur_w += w
        lines.append("".join(cur))
    return lines


def iso_to_epoch_ms(iso: str) -> Optional[int]:
    """
    Parse ISO timestamp like '2026-01-05T09:51:12.981Z' into epoch milliseconds.
    """
    try:
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        dt = _dt.datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def format_epoch_ms(ms: Optional[int]) -> str:
    if ms is None:
        return "Unknown"
    dt = _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def clamp(n: int, lo: int, hi: int) -> int:
    return lo if n < lo else hi if n > hi else n


def chunks(seq: Iterable[str], size: int) -> List[List[str]]:
    out: List[List[str]] = []
    cur: List[str] = []
    for item in seq:
        cur.append(item)
        if len(cur) >= size:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out

