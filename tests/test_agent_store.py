import binascii
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Optional, Tuple
from unittest import mock

import cursor_cli_manager.agent_store as agent_store
from cursor_cli_manager.agent_store import (
    extract_initial_messages,
    extract_last_message_preview,
    extract_recent_messages,
    read_chat_meta,
    read_chat_meta_and_preview,
)


def _make_store_db(db_path: Path, *, meta_obj: dict, blob_id: str, blob_data: bytes) -> None:
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")

    meta_json = json.dumps(meta_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    meta_hex = binascii.hexlify(meta_json).decode("ascii")
    con.execute("INSERT INTO meta(key, value) VALUES(?, ?);", ("0", meta_hex))
    con.execute("INSERT INTO blobs(id, data) VALUES(?, ?);", (blob_id, blob_data))
    con.commit()
    con.close()


class TestAgentStore(unittest.TestCase):
    def _extract_messages_slow_reference(
        self,
        con: sqlite3.Connection,
        *,
        max_messages: Optional[int],
        max_blobs: Optional[int],
        roles: Tuple[str, ...] = ("user", "assistant"),
        from_start: bool = False,
    ) -> list:
        """
        Reference implementation of the previous behavior:
        scan all embedded JSON objects (brace-balanced) and pick message dicts.
        """
        if max_messages is not None and max_messages <= 0:
            return []

        seen = set()
        out = []
        stopped_early = False

        def _append(role: str, text: str) -> None:
            nonlocal stopped_early
            if stopped_early:
                return
            key = (role, text)
            if key in seen:
                return
            seen.add(key)
            if out and out[-1] == key:
                return
            out.append(key)
            if from_start and max_messages is not None and len(out) >= max_messages:
                stopped_early = True

        if from_start:
            if max_blobs is None:
                rows_iter = con.execute("SELECT rowid, data FROM blobs ORDER BY rowid ASC")
            else:
                rows_iter = con.execute("SELECT rowid, data FROM blobs ORDER BY rowid ASC LIMIT ?", (max_blobs,))
        else:
            if max_blobs is None:
                rows_iter = con.execute("SELECT rowid, data FROM blobs ORDER BY rowid ASC")
            else:
                rows = con.execute("SELECT rowid, data FROM blobs ORDER BY rowid DESC LIMIT ?", (max_blobs,)).fetchall()
                rows.reverse()
                rows_iter = rows

        for _rowid, data in rows_iter:
            if isinstance(data, memoryview):
                data = data.tobytes()
            if not isinstance(data, (bytes, bytearray)):
                continue
            blob = bytes(data)
            if b"{" not in blob or b"\"role\"" not in blob:
                continue

            for obj in agent_store._iter_embedded_json_objects(blob, max_objects=500):
                role = obj.get("role")
                if not isinstance(role, str) or role not in roles:
                    continue
                text = agent_store._extract_text_from_message(obj)
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

    def test_read_meta_and_preview(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "store.db"
            root = "rootblob"
            meta = {
                "agentId": "chat-1",
                "latestRootBlobId": root,
                "name": "My Chat",
                "mode": "default",
                "createdAt": 123,
            }
            msg = b'{"id":"1","role":"user","content":[{"type":"text","text":"hello"}]}'
            blob = b"\x00\x01" + msg + b"\x00"
            _make_store_db(db, meta_obj=meta, blob_id=root, blob_data=blob)

            m = read_chat_meta(db)
            assert m is not None
            self.assertEqual(m.agent_id, "chat-1")
            self.assertEqual(m.latest_root_blob_id, root)
            self.assertEqual(m.name, "My Chat")
            self.assertEqual(m.mode, "default")
            self.assertEqual(m.created_at_ms, 123)

            role, text = extract_last_message_preview(db, root)
            self.assertEqual(role, "user")
            self.assertEqual(text, "hello")

            meta2, role2, text2 = read_chat_meta_and_preview(db)
            assert meta2 is not None
            self.assertEqual(meta2.agent_id, "chat-1")
            self.assertEqual(role2, "user")
            self.assertEqual(text2, "hello")

    def test_preview_fallback_scans_other_blobs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "store.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);")
            con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")

            # Root blob is binary/no-json (simulates latestRootBlobId pointing to a non-message root node).
            root = "rootblob"
            con.execute("INSERT INTO blobs(id, data) VALUES(?, ?);", (root, b"\x00\x01\x02\x03"))

            # Another blob contains the actual message JSON.
            msg_blob = "msgblob"
            msg = b'{"id":"1","role":"user","content":[{"type":"text","text":"hello"}]}'
            con.execute("INSERT INTO blobs(id, data) VALUES(?, ?);", (msg_blob, msg))

            meta = {"agentId": "chat-1", "latestRootBlobId": root, "name": "My Chat"}
            meta_json = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            meta_hex = binascii.hexlify(meta_json).decode("ascii")
            con.execute("INSERT INTO meta(key, value) VALUES(?, ?);", ("0", meta_hex))
            con.commit()
            con.close()

            role, text = extract_last_message_preview(db, root)
            self.assertEqual(role, "user")
            self.assertEqual(text, "hello")

            msgs = extract_recent_messages(db, max_messages=5)
            self.assertEqual(msgs, [("user", "hello")])

            msgs_all = extract_recent_messages(db, max_messages=None, max_blobs=None)
            self.assertEqual(msgs_all, [("user", "hello")])

            msgs_init = extract_initial_messages(db, max_messages=5, max_blobs=None)
            self.assertEqual(msgs_init, [("user", "hello")])

    def test_extract_initial_vs_recent_message_order(self) -> None:
        """
        initial: from the start of the chat (chronological)
        recent: from the end of the chat (chronological, last N)
        """
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "store.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);")
            con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
            con.execute("INSERT INTO meta(key, value) VALUES(?, ?);", ("0", ""))

            def _msg(i: int, role: str) -> bytes:
                txt = f"m{i}"
                return json.dumps(
                    {"id": str(i), "role": role, "content": [{"type": "text", "text": txt}]},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")

            # 6 messages across 3 blobs, in chronological insertion order.
            blob1 = b"\x00" + _msg(0, "user") + b"\x00" + _msg(1, "assistant") + b"\x00"
            blob2 = b"\x00" + _msg(2, "user") + b"\x00" + _msg(3, "assistant") + b"\x00"
            blob3 = b"\x00" + _msg(4, "user") + b"\x00" + _msg(5, "assistant") + b"\x00"
            con.execute("INSERT INTO blobs(id, data) VALUES(?, ?);", ("b1", blob1))
            con.execute("INSERT INTO blobs(id, data) VALUES(?, ?);", ("b2", blob2))
            con.execute("INSERT INTO blobs(id, data) VALUES(?, ?);", ("b3", blob3))
            con.commit()
            con.close()

            init3 = extract_initial_messages(db, max_messages=3, max_blobs=None)
            self.assertEqual(init3, [("user", "m0"), ("assistant", "m1"), ("user", "m2")])

            recent3 = extract_recent_messages(db, max_messages=3, max_blobs=None)
            self.assertEqual(recent3, [("assistant", "m3"), ("user", "m4"), ("assistant", "m5")])

    def test_role_anchored_extraction_matches_reference(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "store.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);")
            con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
            con.execute("INSERT INTO meta(key, value) VALUES(?, ?);", ("0", ""))

            # One blob that contains many JSON objects, but only two actual messages.
            noise = [{"n": i, "payload": {"x": i, "y": [1, 2, 3]}} for i in range(200)]
            msg1 = {"id": "1", "role": "user", "content": [{"type": "text", "text": "hello"}]}
            msg2 = {"id": "2", "role": "assistant", "content": [{"type": "text", "text": "world"}]}

            parts = [json.dumps(o, separators=(",", ":")).encode("utf-8") for o in noise]
            parts.insert(50, json.dumps(msg1, separators=(",", ":")).encode("utf-8"))
            parts.insert(150, json.dumps(msg2, separators=(",", ":")).encode("utf-8"))
            blob = b"\x00" + b"\n".join(parts) + b"\x00"
            con.execute("INSERT INTO blobs(id, data) VALUES(?, ?);", ("b1", blob))
            con.commit()

            fast = agent_store._extract_messages_from_connection(
                con, max_messages=None, max_blobs=None, roles=("user", "assistant"), from_start=False
            )
            slow = self._extract_messages_slow_reference(
                con, max_messages=None, max_blobs=None, roles=("user", "assistant"), from_start=False
            )
            self.assertEqual(fast, slow)
            self.assertEqual(fast, [("user", "hello"), ("assistant", "world")])
            con.close()

    def test_role_anchored_reduces_json_loads_calls(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "store.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);")
            con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
            con.execute("INSERT INTO meta(key, value) VALUES(?, ?);", ("0", ""))

            noise = [{"k": i, "v": {"a": i}} for i in range(300)]
            msg1 = {"id": "1", "role": "user", "content": [{"type": "text", "text": "a"}]}
            msg2 = {"id": "2", "role": "assistant", "content": [{"type": "text", "text": "b"}]}
            parts = [json.dumps(o, separators=(",", ":")).encode("utf-8") for o in noise]
            parts.insert(10, json.dumps(msg1, separators=(",", ":")).encode("utf-8"))
            parts.insert(290, json.dumps(msg2, separators=(",", ":")).encode("utf-8"))
            blob = b"\n".join(parts)
            con.execute("INSERT INTO blobs(id, data) VALUES(?, ?);", ("b1", blob))
            con.commit()

            calls = {"n": 0}
            real_loads = agent_store.json.loads

            def _counting_loads(*args, **kwargs):
                calls["n"] += 1
                return real_loads(*args, **kwargs)

            with mock.patch.object(agent_store.json, "loads", side_effect=_counting_loads):
                calls["n"] = 0
                _ = self._extract_messages_slow_reference(
                    con, max_messages=None, max_blobs=None, roles=("user", "assistant"), from_start=False
                )
                slow_calls = calls["n"]

                calls["n"] = 0
                _ = agent_store._extract_messages_from_connection(
                    con, max_messages=None, max_blobs=None, roles=("user", "assistant"), from_start=False
                )
                fast_calls = calls["n"]

            self.assertGreater(slow_calls, 100)
            self.assertLess(fast_calls, slow_calls // 5)
            con.close()

    def test_full_history_cache_hit_avoids_json_loads(self) -> None:
        agent_store._clear_caches_for_tests()
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "store.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);")
            con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
            con.execute("INSERT INTO meta(key, value) VALUES(?, ?);", ("0", ""))

            noise = [{"n": i, "payload": {"x": i, "y": [1, 2, 3]}} for i in range(400)]
            msg1 = {"id": "1", "role": "user", "content": [{"type": "text", "text": "hello"}]}
            msg2 = {"id": "2", "role": "assistant", "content": [{"type": "text", "text": "world"}]}
            parts = [json.dumps(o, separators=(",", ":")).encode("utf-8") for o in noise]
            parts.insert(100, json.dumps(msg1, separators=(",", ":")).encode("utf-8"))
            parts.insert(300, json.dumps(msg2, separators=(",", ":")).encode("utf-8"))
            con.execute("INSERT INTO blobs(id, data) VALUES(?, ?);", ("b1", b"\n".join(parts)))
            con.commit()
            con.close()

            calls = {"n": 0}
            real_loads = agent_store.json.loads

            def _counting_loads(*args, **kwargs):
                calls["n"] += 1
                return real_loads(*args, **kwargs)

            with mock.patch.object(agent_store.json, "loads", side_effect=_counting_loads):
                calls["n"] = 0
                msgs1 = extract_recent_messages(db, max_messages=None, max_blobs=None)
                first_calls = calls["n"]
                self.assertEqual(msgs1, [("user", "hello"), ("assistant", "world")])
                self.assertGreater(first_calls, 0)

                calls["n"] = 0
                msgs2 = extract_recent_messages(db, max_messages=None, max_blobs=None)
                second_calls = calls["n"]
                self.assertEqual(msgs2, msgs1)
                self.assertEqual(second_calls, 0)

    def test_full_history_cache_invalidation_on_db_change(self) -> None:
        agent_store._clear_caches_for_tests()
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "store.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);")
            con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
            con.execute("INSERT INTO meta(key, value) VALUES(?, ?);", ("0", ""))
            msg1 = {"id": "1", "role": "user", "content": [{"type": "text", "text": "a"}]}
            con.execute("INSERT INTO blobs(id, data) VALUES(?, ?);", ("b1", json.dumps(msg1).encode("utf-8")))
            con.commit()
            con.close()

            _ = extract_recent_messages(db, max_messages=None, max_blobs=None)

            # Append a new blob (stamp must change).
            con2 = sqlite3.connect(db)
            msg2 = {"id": "2", "role": "assistant", "content": [{"type": "text", "text": "b"}]}
            con2.execute("INSERT INTO blobs(id, data) VALUES(?, ?);", ("b2", json.dumps(msg2).encode("utf-8")))
            con2.commit()
            con2.close()

            msgs = extract_recent_messages(db, max_messages=None, max_blobs=None)
            self.assertEqual(msgs, [("user", "a"), ("assistant", "b")])

    def test_extract_initial_uses_full_cache_when_available(self) -> None:
        agent_store._clear_caches_for_tests()
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "store.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);")
            con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
            con.execute("INSERT INTO meta(key, value) VALUES(?, ?);", ("0", ""))
            msg1 = {"id": "1", "role": "user", "content": [{"type": "text", "text": "first"}]}
            msg2 = {"id": "2", "role": "assistant", "content": [{"type": "text", "text": "second"}]}
            con.execute("INSERT INTO blobs(id, data) VALUES(?, ?);", ("b1", json.dumps(msg1).encode("utf-8")))
            con.execute("INSERT INTO blobs(id, data) VALUES(?, ?);", ("b2", json.dumps(msg2).encode("utf-8")))
            con.commit()
            con.close()

            # Populate full-history cache.
            full = extract_recent_messages(db, max_messages=None, max_blobs=None)
            self.assertEqual(full, [("user", "first"), ("assistant", "second")])

            calls = {"n": 0}
            real_loads = agent_store.json.loads

            def _counting_loads(*args, **kwargs):
                calls["n"] += 1
                return real_loads(*args, **kwargs)

            with mock.patch.object(agent_store.json, "loads", side_effect=_counting_loads):
                calls["n"] = 0
                init1 = extract_initial_messages(db, max_messages=1, max_blobs=None)
                self.assertEqual(init1, [("user", "first")])
                self.assertEqual(calls["n"], 0)


if __name__ == "__main__":
    unittest.main()

