from __future__ import annotations

import binascii
import json
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class AgentChatMeta:
    agent_id: str
    latest_root_blob_id: Optional[str]
    name: str
    mode: Optional[str]
    created_at_ms: Optional[int]


def _connect_ro_uris(db_path: Path) -> List[str]:
    """
    Candidate URIs to open SQLite in read-only mode.
    """
    # `immutable=1` helps in environments where sqlite would otherwise try to
    # create -shm/-wal/-journal files (e.g., read-only sandboxes).
    return [
        f"file:{db_path.as_posix()}?mode=ro",
        f"file:{db_path.as_posix()}?mode=ro&immutable=1",
    ]


def _with_ro_connection(db_path: Path, op) -> Optional[object]:
    """
    Run `op(con)` against a read-only connection with best-effort fallbacks.

    This avoids an extra validation query (sqlite_master) per DB by simply
    attempting the real query and falling back if it fails.
    """
    last_err: Optional[BaseException] = None
    for uri in _connect_ro_uris(db_path):
        con: Optional[sqlite3.Connection] = None
        try:
            con = sqlite3.connect(uri, uri=True, timeout=0.2)
            _tune_readonly_connection(con)
            return op(con)
        except sqlite3.Error as e:
            last_err = e
            continue
        finally:
            try:
                if con is not None:
                    con.close()
            except Exception:
                pass
    return None


def _tune_readonly_connection(con: sqlite3.Connection) -> None:
    """
    Best-effort read-only tuning to reduce export latency.

    These PRAGMAs are hints; failures are ignored to preserve portability.
    """
    # Make writes impossible even if a caller tries.
    try:
        con.execute("PRAGMA query_only=ON;")
    except Exception:
        pass
    # Prefer keeping temporary state in memory.
    try:
        con.execute("PRAGMA temp_store=MEMORY;")
    except Exception:
        pass
    # Increase page cache (negative = KB) to reduce IO churn on large blobs.
    try:
        con.execute("PRAGMA cache_size=-8000;")  # ~8 MiB
    except Exception:
        pass
    # Memory-map file if possible (can significantly reduce memcpy on some systems).
    try:
        con.execute("PRAGMA mmap_size=268435456;")  # 256 MiB
    except Exception:
        pass


def _maybe_decode_hex_json(s: str) -> Optional[Dict[str, Any]]:
    if not s:
        return None
    ss = s.strip()
    if len(ss) % 2 == 0 and all(c in "0123456789abcdef" for c in ss.lower()):
        try:
            raw = binascii.unhexlify(ss)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None
    try:
        return json.loads(ss)
    except Exception:
        return None


def _read_chat_meta_from_connection(con: sqlite3.Connection) -> Optional[AgentChatMeta]:
    # Fast path: observed most commonly as meta key "0" with hex-encoded JSON.
    meta_obj: Optional[Dict[str, Any]] = None
    try:
        row = con.execute("SELECT value FROM meta WHERE key='0' LIMIT 1").fetchone()
        if row and isinstance(row[0], str):
            meta_obj = _maybe_decode_hex_json(row[0])
    except sqlite3.Error:
        meta_obj = None

    if meta_obj is None:
        rows = con.execute("SELECT key, value FROM meta").fetchall()
        if not rows:
            return None

        # Back-compat / fallback: treat meta as a key-value map.
        obj: Dict[str, Any] = {}
        for k, v in rows:
            if isinstance(k, str):
                obj[k] = v
        meta_obj = obj

        # Observed: a single row with key "0" that contains hex-encoded JSON bytes.
        if len(rows) == 1 and isinstance(rows[0][1], str):
            decoded = _maybe_decode_hex_json(rows[0][1])
            if decoded is not None:
                meta_obj = decoded

    agent_id = meta_obj.get("agentId")
    if not isinstance(agent_id, str) or not agent_id:
        return None

    latest_root_blob_id = meta_obj.get("latestRootBlobId")
    if not isinstance(latest_root_blob_id, str) or not latest_root_blob_id:
        latest_root_blob_id = None

    name = meta_obj.get("name")
    if not isinstance(name, str) or not name.strip():
        name = "Untitled"

    mode = meta_obj.get("mode")
    if not isinstance(mode, str):
        mode = None

    created_at_ms = meta_obj.get("createdAt")
    if not isinstance(created_at_ms, int):
        created_at_ms = None

    return AgentChatMeta(
        agent_id=agent_id,
        latest_root_blob_id=latest_root_blob_id,
        name=name,
        mode=mode,
        created_at_ms=created_at_ms,
    )


def read_chat_meta(store_db: Path) -> Optional[AgentChatMeta]:
    if not store_db.exists():
        return None

    def _op(con: sqlite3.Connection) -> Optional[AgentChatMeta]:
        return _read_chat_meta_from_connection(con)

    res = _with_ro_connection(store_db, _op)
    return res if isinstance(res, AgentChatMeta) else None


def _read_blob_from_connection(con: sqlite3.Connection, blob_id: str) -> Optional[bytes]:
    row = con.execute("SELECT data FROM blobs WHERE id=? LIMIT 1", (blob_id,)).fetchone()
    if not row:
        return None
    data = row[0]
    if isinstance(data, memoryview):
        data = data.tobytes()
    if isinstance(data, bytes):
        return data
    return None


def read_blob(store_db: Path, blob_id: str) -> Optional[bytes]:
    def _op(con: sqlite3.Connection) -> Optional[bytes]:
        return _read_blob_from_connection(con, blob_id)

    res = _with_ro_connection(store_db, _op)
    return res if isinstance(res, (bytes, bytearray)) else None


def _iter_embedded_json_objects(data: bytes, *, max_objects: int = 200) -> Iterator[Dict[str, Any]]:
    """
    Extract JSON objects embedded in a binary blob by scanning for balanced braces.
    This is best-effort but works for cursor-agent root blobs in practice.
    """
    n = len(data)
    i = 0
    found = 0
    while i < n and found < max_objects:
        start = data.find(b"{", i)
        if start == -1:
            break

        depth = 0
        in_str = False
        esc = False
        j = start

        while j < n:
            b = data[j]
            if in_str:
                if esc:
                    esc = False
                elif b == 0x5C:  # backslash
                    esc = True
                elif b == 0x22:  # quote
                    in_str = False
            else:
                if b == 0x22:
                    in_str = True
                elif b == 0x7B:  # {
                    depth += 1
                elif b == 0x7D:  # }
                    depth -= 1
                    if depth == 0:
                        chunk = data[start : j + 1]
                        try:
                            obj = json.loads(chunk.decode("utf-8"))
                            if isinstance(obj, dict):
                                yield obj
                                found += 1
                                i = j + 1
                            else:
                                i = start + 1
                        except Exception:
                            i = start + 1
                        break
            j += 1
        else:
            i = start + 1


_ROLE_MARKER = b"\"role\""

# Full-history extraction can be expensive on large store.db files. Cache the fully
# extracted message list per DB+stamp so repeated preview/export is instant.
_FULL_CACHE_LOCK = threading.Lock()
# key: (db_path, roles_tuple, stamp) -> (messages, approx_bytes)
_FULL_CACHE: "OrderedDict[Tuple[str, Tuple[str, ...], Tuple[int, int]], Tuple[List[Tuple[str, str]], int]]" = OrderedDict()
_FULL_CACHE_BYTES = 0
_FULL_CACHE_MAX_ENTRIES = 24
_FULL_CACHE_MAX_BYTES = 32 * 1024 * 1024  # 32 MiB


def _clear_caches_for_tests() -> None:
    global _FULL_CACHE_BYTES
    with _FULL_CACHE_LOCK:
        _FULL_CACHE.clear()
        _FULL_CACHE_BYTES = 0


def _approx_messages_bytes(msgs: Sequence[Tuple[str, str]]) -> int:
    # Rough accounting (Python object overhead ignored); sufficient for bounding cache growth.
    total = 0
    for r, t in msgs:
        total += len(r) + len(t) + 32
    return total


def _blobs_stamp(con: sqlite3.Connection) -> Tuple[int, int]:
    """
    Compute a cheap, content-sensitive stamp for the blobs table.

    We include both MAX(rowid) and SUM(LENGTH(data)) so that the stamp changes on
    appends and most in-place updates, without reading all blob contents into Python.
    """
    try:
        row = con.execute(
            "SELECT COALESCE(MAX(rowid), 0), COALESCE(SUM(LENGTH(data)), 0) FROM blobs"
        ).fetchone()
    except sqlite3.Error:
        return (0, 0)
    if not row:
        return (0, 0)
    try:
        return (int(row[0] or 0), int(row[1] or 0))
    except Exception:
        return (0, 0)


def _full_cache_get(db_key: str, roles_key: Tuple[str, ...], stamp: Tuple[int, int]) -> Optional[List[Tuple[str, str]]]:
    k = (db_key, roles_key, stamp)
    with _FULL_CACHE_LOCK:
        hit = _FULL_CACHE.get(k)
        if hit is None:
            return None
        _FULL_CACHE.move_to_end(k, last=True)
        return hit[0]


def _full_cache_put(db_key: str, roles_key: Tuple[str, ...], stamp: Tuple[int, int], msgs: List[Tuple[str, str]]) -> None:
    global _FULL_CACHE_BYTES
    k = (db_key, roles_key, stamp)
    b = _approx_messages_bytes(msgs)
    with _FULL_CACHE_LOCK:
        old = _FULL_CACHE.pop(k, None)
        if old is not None:
            _FULL_CACHE_BYTES -= int(old[1] or 0)
        _FULL_CACHE[k] = (msgs, b)
        _FULL_CACHE_BYTES += b
        _FULL_CACHE.move_to_end(k, last=True)

        while _FULL_CACHE and (len(_FULL_CACHE) > _FULL_CACHE_MAX_ENTRIES or _FULL_CACHE_BYTES > _FULL_CACHE_MAX_BYTES):
            _k, (_msgs, _b) = _FULL_CACHE.popitem(last=False)
            _FULL_CACHE_BYTES -= int(_b or 0)


def _scan_balanced_object_end(data: bytes, start: int, *, max_len: int) -> Optional[int]:
    """
    Given `data[start] == b'{'`, find the matching '}' index using brace balancing.

    Returns the inclusive end index, or None if not found within max_len.
    """
    n = len(data)
    if start < 0 or start >= n or data[start] != 0x7B:  # '{'
        return None
    end_limit = min(n, start + max(0, max_len))

    depth = 0
    in_str = False
    esc = False
    j = start
    while j < end_limit:
        b = data[j]
        if in_str:
            if esc:
                esc = False
            elif b == 0x5C:  # backslash
                esc = True
            elif b == 0x22:  # quote
                in_str = False
        else:
            if b == 0x22:  # quote
                in_str = True
            elif b == 0x7B:  # {
                depth += 1
            elif b == 0x7D:  # }
                depth -= 1
                if depth == 0:
                    return j
        j += 1
    return None


def _parse_json_dict_from_span(data: bytes, start: int, end_inclusive: int) -> Optional[Dict[str, Any]]:
    if start < 0 or end_inclusive < start or end_inclusive >= len(data):
        return None
    try:
        chunk = data[start : end_inclusive + 1]
        obj = json.loads(chunk.decode("utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _find_enclosing_message_obj_around_role(
    data: bytes,
    role_pos: int,
    *,
    roles_set: set[str],
    back_window: int = 65536,
    max_candidates: int = 16,
    max_obj_len: int = 262144,
) -> Optional[Tuple[Dict[str, Any], int]]:
    """
    Try to find and parse the message object that contains a `\"role\"` marker at `role_pos`.

    Returns (obj, end_pos_exclusive) if a plausible message dict is found.
    """
    n = len(data)
    if role_pos < 0 or role_pos >= n:
        return None

    left = max(0, role_pos - max(0, back_window))
    candidates: List[int] = []
    p = role_pos
    for _ in range(max(1, max_candidates)):
        s = data.rfind(b"{", left, p + 1)
        if s == -1:
            break
        candidates.append(s)
        p = s - 1

    for s in candidates:
        end = _scan_balanced_object_end(data, s, max_len=max_obj_len)
        if end is None:
            continue
        obj = _parse_json_dict_from_span(data, s, end)
        if not isinstance(obj, dict):
            continue
        role = obj.get("role")
        if not isinstance(role, str) or role not in roles_set:
            continue
        # Fast reject: messages must have a "content" field to be useful for us.
        if "content" not in obj:
            continue
        return obj, end + 1
    return None


def _iter_message_objects_role_anchored(
    data: bytes,
    *,
    roles: Sequence[str],
    max_objects: int = 500,
) -> Iterator[Dict[str, Any]]:
    """
    Faster iterator over message dicts by anchoring on `\"role\"` markers.

    Compared to `_iter_embedded_json_objects`, this avoids parsing unrelated JSON
    objects inside blobs once we know where a message-like object must exist.
    """
    if not data:
        return
    roles_set = {r for r in roles if isinstance(r, str)}
    if not roles_set:
        return
    if b"{" not in data or _ROLE_MARKER not in data:
        return

    found = 0
    pos = 0
    n = len(data)
    while pos < n and found < max_objects:
        role_pos = data.find(_ROLE_MARKER, pos)
        if role_pos == -1:
            break
        res = _find_enclosing_message_obj_around_role(data, role_pos, roles_set=roles_set)
        if res is None:
            pos = role_pos + len(_ROLE_MARKER)
            continue
        obj, end_pos = res
        yield obj
        found += 1
        # Skip past the whole object to avoid re-parsing it if `\"role\"` also
        # appears inside string content.
        pos = max(end_pos, role_pos + len(_ROLE_MARKER))


def _extract_text_from_message(msg: Dict[str, Any]) -> Optional[str]:
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            # Common part shapes:
            # - {"type":"text","text":"..."}
            # - {"type":"output_text","text":"..."}
            # - {"type":"input_text","text":"..."}
            t = part.get("text")
            if isinstance(t, str) and t.strip():
                return t
            # Sometimes content is nested in "data"
            d = part.get("data")
            if isinstance(d, str) and d.strip() and part.get("type") in ("text", "output_text", "input_text"):
                return d
    return None


def _extract_messages_from_connection(
    con: sqlite3.Connection,
    *,
    max_messages: Optional[int] = 10,
    max_blobs: Optional[int] = 200,
    roles: Sequence[str] = ("user", "assistant"),
    from_start: bool = False,
) -> List[Tuple[str, str]]:
    if max_messages is not None and max_messages <= 0:
        return []

    seen: set[Tuple[str, str]] = set()
    out: List[Tuple[str, str]] = []
    stopped_early = False

    def _append(role: str, text: str) -> None:
        nonlocal stopped_early
        if stopped_early:
            return
        key = (role, text)
        if key in seen:
            return
        seen.add(key)
        # Drop consecutive duplicates as we build (helps when messages are embedded multiple times).
        if out and out[-1] == (role, text):
            return
        out.append((role, text))
        if from_start and max_messages is not None and len(out) >= max_messages:
            stopped_early = True

    # Decide which blobs to scan.
    #
    # - from_start=True: scan chronologically; optionally limit to earliest max_blobs blobs.
    # - from_start=False: keep the existing behavior of scanning *most recent* blobs for performance.
    if from_start:
        if max_blobs is None:
            rows_iter = con.execute("SELECT rowid, data FROM blobs ORDER BY rowid ASC")
        else:
            if max_blobs <= 0:
                return []
            rows_iter = con.execute(
                "SELECT rowid, data FROM blobs ORDER BY rowid ASC LIMIT ?",
                (max_blobs,),
            )
    else:
        if max_blobs is None:
            rows_iter = con.execute("SELECT rowid, data FROM blobs ORDER BY rowid ASC")
        else:
            if max_blobs <= 0:
                return []
            # Scan only the most recent blobs for performance, but keep chronological order.
            rows = con.execute(
                "SELECT rowid, data FROM blobs ORDER BY rowid DESC LIMIT ?",
                (max_blobs,),
            ).fetchall()
            rows.reverse()
            rows_iter = rows

    for _rowid, data in rows_iter:
        if isinstance(data, memoryview):
            data = data.tobytes()
        if not isinstance(data, (bytes, bytearray)):
            continue
        blob = bytes(data)

        # Quick filter: avoid scanning blobs that clearly don't contain JSON.
        if b"{" not in blob or b"\"role\"" not in blob:
            continue

        # Fast path: role-anchored extraction (falls back per-blob if needed).
        #
        # This is much faster on large store.db files where blobs contain many
        # unrelated JSON objects (e.g., state, tool results, caches) in addition
        # to a handful of message objects.
        objs: Iterable[Dict[str, Any]] = _iter_message_objects_role_anchored(blob, roles=roles, max_objects=500)
        got_any = False
        for obj in objs:
            got_any = True
            role = obj.get("role")
            if not isinstance(role, str) or role not in roles:
                continue
            text = _extract_text_from_message(obj)
            if not text:
                continue

            # Skip the auto-injected environment block if present.
            if role == "user" and text.lstrip().startswith("<user_info>"):
                continue

            _append(role, text.strip())
            if stopped_early:
                break
        if (not got_any) and (b"{" in blob and b"\"role\"" in blob):
            # Fallback: if the role-anchored scan couldn't find any message-like
            # objects (e.g., unusual blob layout), fall back to the slower but
            # more general embedded-object scan for this blob only.
            for obj in _iter_embedded_json_objects(blob, max_objects=500):
                role = obj.get("role")
                if not isinstance(role, str) or role not in roles:
                    continue
                text = _extract_text_from_message(obj)
                if not text:
                    continue
                if role == "user" and text.lstrip().startswith("<user_info>"):
                    continue
                _append(role, text.strip())
                if stopped_early:
                    break
        if stopped_early:
            break

    if max_messages is None:
        return out
    if from_start:
        return out[:max_messages]
    return out[-max_messages:]


def _extract_last_message_preview_from_connection(
    con: sqlite3.Connection, latest_root_blob_id: str
) -> Tuple[Optional[str], Optional[str]]:
    blob = _read_blob_from_connection(con, latest_root_blob_id)
    if blob:
        last_role: Optional[str] = None
        last_text: Optional[str] = None

        for obj in _iter_embedded_json_objects(blob):
            role = obj.get("role")
            if not isinstance(role, str):
                continue
            text = _extract_text_from_message(obj)
            if not text:
                continue
            # Skip the auto-injected environment block if present.
            if role == "user" and text.lstrip().startswith("<user_info>"):
                continue
            last_role = role
            last_text = text
        if last_text:
            return last_role, last_text

    msgs = _extract_messages_from_connection(con, max_messages=1, max_blobs=200, roles=("user", "assistant"), from_start=False)
    if msgs:
        role, text = msgs[-1]
        return role, text

    return None, None


def extract_recent_messages(
    store_db: Path,
    *,
    max_messages: Optional[int] = 10,
    max_blobs: Optional[int] = 200,
    roles: Sequence[str] = ("user", "assistant"),
) -> List[Tuple[str, str]]:
    """
    Best-effort extraction of recent chat messages from cursor-agent store.db.

    Some cursor-agent versions store the latestRootBlobId as a binary root node that
    does not directly contain message JSON objects. In that case, scanning across
    recent blobs usually finds embedded message objects.
    """
    if max_messages is not None and max_messages <= 0:
        return []

    # When scanning the full DB (max_blobs=None), reuse a per-process cache keyed by a
    # cheap blobs-table stamp. This makes repeated full-preview/export instant.
    if max_blobs is None and store_db.exists():
        db_key = store_db.as_posix()
        roles_key = tuple(str(r) for r in roles)

        def _op(con: sqlite3.Connection) -> List[Tuple[str, str]]:
            stamp = _blobs_stamp(con)
            cached = _full_cache_get(db_key, roles_key, stamp)
            if cached is None:
                cached = _extract_messages_from_connection(
                    con,
                    max_messages=None,
                    max_blobs=None,
                    roles=roles_key,
                    from_start=False,
                )
                _full_cache_put(db_key, roles_key, stamp, cached)
            if max_messages is None:
                return cached
            return cached[-max_messages:]

        res = _with_ro_connection(store_db, _op)
        return res if isinstance(res, list) else []

    def _op(con: sqlite3.Connection) -> List[Tuple[str, str]]:
        return _extract_messages_from_connection(
            con,
            max_messages=max_messages,
            max_blobs=max_blobs,
            roles=roles,
            from_start=False,
        )

    res = _with_ro_connection(store_db, _op)
    return res if isinstance(res, list) else []


def extract_initial_messages(
    store_db: Path,
    *,
    max_messages: int = 10,
    max_blobs: Optional[int] = None,
    roles: Sequence[str] = ("user", "assistant"),
) -> List[Tuple[str, str]]:
    """
    Best-effort extraction of messages from the *start* of the chat.

    This is used for fast preview snippets: we can stop early once we have enough
    messages, avoiding a full DB scan.
    """
    if max_messages <= 0:
        return []

    # If we already have a cached full-history extraction for this DB, serve the
    # initial snippet from it without scanning/parsing blobs again. Importantly,
    # we do NOT populate the full cache from here, so initial preview remains fast.
    if max_blobs is None and store_db.exists():
        db_key = store_db.as_posix()
        roles_key = tuple(str(r) for r in roles)

        def _op(con: sqlite3.Connection) -> List[Tuple[str, str]]:
            stamp = _blobs_stamp(con)
            cached = _full_cache_get(db_key, roles_key, stamp)
            if cached is not None:
                return cached[:max_messages]
            return _extract_messages_from_connection(
                con,
                max_messages=max_messages,
                max_blobs=None,
                roles=roles_key,
                from_start=True,
            )

        res = _with_ro_connection(store_db, _op)
        return res if isinstance(res, list) else []

    def _op(con: sqlite3.Connection) -> List[Tuple[str, str]]:
        return _extract_messages_from_connection(
            con,
            max_messages=max_messages,
            max_blobs=max_blobs,
            roles=roles,
            from_start=True,
        )

    res = _with_ro_connection(store_db, _op)
    return res if isinstance(res, list) else []


def format_messages_preview(
    messages: Sequence[Tuple[str, str]],
    *,
    max_chars_per_message: int = 600,
) -> str:
    """
    Render messages into a multi-line preview string suitable for the TUI.
    """
    parts: List[str] = []
    for role, text in messages:
        label = "User" if role == "user" else "Assistant" if role == "assistant" else role
        parts.append(f"{label}:")
        t = text.strip()
        if max_chars_per_message > 0 and len(t) > max_chars_per_message:
            t = t[: max_chars_per_message - 1].rstrip() + "…"
        parts.append(t)
        parts.append("")
    return "\n".join(parts).rstrip()


def extract_last_message_preview(store_db: Path, latest_root_blob_id: str) -> Tuple[Optional[str], Optional[str]]:
    def _op(con: sqlite3.Connection) -> Tuple[Optional[str], Optional[str]]:
        return _extract_last_message_preview_from_connection(con, latest_root_blob_id)

    res = _with_ro_connection(store_db, _op)
    return res if isinstance(res, tuple) else (None, None)


def read_chat_meta_and_preview(
    store_db: Path,
) -> Tuple[Optional[AgentChatMeta], Optional[str], Optional[str]]:
    """
    Read chat meta and a last-message preview using a single SQLite connection.
    """
    if not store_db.exists():
        return None, None, None

    def _op(con: sqlite3.Connection) -> Tuple[Optional[AgentChatMeta], Optional[str], Optional[str]]:
        meta = _read_chat_meta_from_connection(con)
        if meta is None or not meta.latest_root_blob_id:
            return meta, None, None
        role, text = _extract_last_message_preview_from_connection(con, meta.latest_root_blob_id)
        return meta, role, text

    res = _with_ro_connection(store_db, _op)
    return res if isinstance(res, tuple) else (None, None, None)

