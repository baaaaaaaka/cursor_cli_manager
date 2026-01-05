import binascii
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cursor_cli_manager.agent_store import extract_last_message_preview, extract_recent_messages, read_chat_meta


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


if __name__ == "__main__":
    unittest.main()

