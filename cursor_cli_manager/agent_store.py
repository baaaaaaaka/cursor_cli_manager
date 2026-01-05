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


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """
    Open SQLite DB in read-only mode (avoid lock/journal writes).
    """
    # `immutable=1` helps in environments where sqlite would otherwise try to
    # create -shm/-wal/-journal files (e.g., read-only sandboxes).
    #
    # NOTE: Some SQLite builds can appear to "connect" successfully with mode=ro
    # but fail on the first statement. We therefore validate by touching
    # sqlite_master before returning.
    candidates = [
        f"file:{db_path.as_posix()}?mode=ro",
        f"file:{db_path.as_posix()}?mode=ro&immutable=1",
    ]
    last_err: Optional[BaseException] = None
    for uri in candidates:
        con: Optional[sqlite3.Connection] = None
        try:
            con = sqlite3.connect(uri, uri=True, timeout=0.2)
            con.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1").fetchall()
            return con
        except sqlite3.Error as e:
            last_err = e
            try:
                if con is not None:
                    con.close()
            except Exception:
                pass
            continue
    raise sqlite3.Error(str(last_err) if last_err else "unable to open database file")


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


def read_chat_meta(store_db: Path) -> Optional[AgentChatMeta]:
    if not store_db.exists():
        return None

    try:
        con = _connect_ro(store_db)
    except sqlite3.Error:
        return None

    try:
        rows = con.execute("SELECT key, value FROM meta").fetchall()
        if not rows:
            return None

        meta_obj: Optional[Dict[str, Any]] = None
        # Observed: a single row with key "0" that contains hex-encoded JSON bytes.
        if len(rows) == 1 and isinstance(rows[0][1], str):
            meta_obj = _maybe_decode_hex_json(rows[0][1])
        if meta_obj is None:
            # Fallback: treat meta as a key-value map (best-effort).
            obj: Dict[str, Any] = {}
            for k, v in rows:
                if isinstance(k, str):
                    obj[k] = v
            meta_obj = obj

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
    except sqlite3.Error:
        return None
    finally:
        con.close()


def read_blob(store_db: Path, blob_id: str) -> Optional[bytes]:
    try:
        con = _connect_ro(store_db)
    except sqlite3.Error:
        return None
    try:
        row = con.execute("SELECT data FROM blobs WHERE id=? LIMIT 1", (blob_id,)).fetchone()
        if not row:
            return None
        data = row[0]
        if isinstance(data, memoryview):
            data = data.tobytes()
        if isinstance(data, bytes):
            return data
        return None
    except sqlite3.Error:
        return None
    finally:
        con.close()


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

    try:
        con = _connect_ro(store_db)
    except sqlite3.Error:
        return []

    try:
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
    except sqlite3.Error:
        return []
    finally:
        con.close()


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
    blob = read_blob(store_db, latest_root_blob_id)
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

    # Fallback: scan recent blobs in the DB and take the last user/assistant message.
    msgs = extract_recent_messages(store_db, max_messages=1)
    if msgs:
        role, text = msgs[-1]
        return role, text

    return None, None

