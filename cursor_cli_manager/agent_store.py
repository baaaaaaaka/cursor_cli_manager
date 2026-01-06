from __future__ import annotations

import binascii
import json
import sqlite3
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


def _extract_recent_messages_from_connection(
    con: sqlite3.Connection,
    *,
    max_messages: int = 10,
    max_blobs: int = 200,
    roles: Sequence[str] = ("user", "assistant"),
) -> List[Tuple[str, str]]:
    if max_messages <= 0:
        return []

    # Scan only the most recent blobs for performance, but keep ordering by rowid
    # so the returned messages are chronological.
    rows = con.execute(
        "SELECT rowid, data FROM blobs ORDER BY rowid DESC LIMIT ?",
        (max_blobs,),
    ).fetchall()
    rows.reverse()

    seen: set[Tuple[str, str]] = set()
    out: List[Tuple[str, str]] = []

    for _rowid, data in rows:
        if isinstance(data, memoryview):
            data = data.tobytes()
        if not isinstance(data, (bytes, bytearray)):
            continue
        blob = bytes(data)

        # Quick filter: avoid scanning blobs that clearly don't contain JSON.
        if b"{" not in blob or b"\"role\"" not in blob:
            continue

        for obj in _iter_embedded_json_objects(blob, max_objects=500):
            role = obj.get("role")
            if not isinstance(role, str) or role not in roles:
                continue
            text = _extract_text_from_message(obj)
            if not text:
                continue

            # Skip the auto-injected environment block if present.
            if role == "user" and text.lstrip().startswith("<user_info>"):
                continue

            msg_id = obj.get("id")
            key_id = msg_id if isinstance(msg_id, str) and msg_id else None
            key = (role, key_id or text)
            if key in seen:
                continue
            seen.add(key)
            out.append((role, text.strip()))

    # Drop consecutive duplicates (helps when the same message is embedded multiple times).
    deduped: List[Tuple[str, str]] = []
    for role, text in out:
        if deduped and deduped[-1] == (role, text):
            continue
        deduped.append((role, text))

    return deduped[-max_messages:]


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

    msgs = _extract_recent_messages_from_connection(con, max_messages=1)
    if msgs:
        role, text = msgs[-1]
        return role, text

    return None, None


def extract_recent_messages(
    store_db: Path,
    *,
    max_messages: int = 10,
    max_blobs: int = 200,
    roles: Sequence[str] = ("user", "assistant"),
) -> List[Tuple[str, str]]:
    """
    Best-effort extraction of recent chat messages from cursor-agent store.db.

    Some cursor-agent versions store the latestRootBlobId as a binary root node that
    does not directly contain message JSON objects. In that case, scanning across
    recent blobs usually finds embedded message objects.
    """
    if max_messages <= 0:
        return []

    def _op(con: sqlite3.Connection) -> List[Tuple[str, str]]:
        return _extract_recent_messages_from_connection(con, max_messages=max_messages, max_blobs=max_blobs, roles=roles)

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

