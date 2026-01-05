from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class VscdbError(Exception):
    message: str

    def __str__(self) -> str:  # pragma: no cover
        return self.message


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    # `mode=ro` prevents accidental writes and helps with locked DBs.
    uri = f"file:{db_path.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=1.0)


def read_value(db_path: Path, key: str, table: str = "ItemTable") -> Optional[str]:
    if not db_path.exists():
        return None

    try:
        con = _connect_readonly(db_path)
    except sqlite3.Error as e:  # pragma: no cover (hard to reproduce reliably)
        raise VscdbError(f"Failed to open vscdb: {db_path} ({e})")

    try:
        row = con.execute(
            f"SELECT value FROM {table} WHERE key = ? LIMIT 1", (key,)
        ).fetchone()
        if not row:
            return None

        val = row[0]
        if isinstance(val, memoryview):
            val = val.tobytes()

        if isinstance(val, bytes):
            return val.decode("utf-8")
        if isinstance(val, str):
            return val

        return str(val)
    except sqlite3.Error as e:  # pragma: no cover
        raise VscdbError(f"Failed to query vscdb: {db_path} ({e})")
    finally:
        con.close()


def read_json(db_path: Path, key: str, table: str = "ItemTable") -> Optional[Any]:
    raw = read_value(db_path, key, table=table)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise VscdbError(f"Key {key!r} is not valid JSON in {db_path}: {e}")


