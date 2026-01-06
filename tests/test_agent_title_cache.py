import binascii
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cursor_cli_manager.agent_discovery import discover_agent_chats, discover_agent_workspaces
from cursor_cli_manager.agent_paths import CursorAgentDirs, md5_hex
from cursor_cli_manager.agent_title_cache import load_chat_title_cache, save_chat_title_cache, set_cached_title


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


class TestAgentTitleCache(unittest.TestCase):
    def test_discover_agent_chats_uses_cached_title_for_generic_names(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td) / "cursor_config"
            chats_dir = config_dir / "chats"
            chats_dir.mkdir(parents=True)

            ws_path = Path(td) / "repo"
            ws_path.mkdir()
            h = md5_hex(str(ws_path))
            ws_hash_dir = chats_dir / h
            ws_hash_dir.mkdir()

            chat_id = "chat-123"
            chat_dir = ws_hash_dir / chat_id
            chat_dir.mkdir()
            db = chat_dir / "store.db"
            root = "rootblob"
            meta = {"agentId": chat_id, "latestRootBlobId": root, "name": "New Agent", "mode": "default", "createdAt": 10}
            blob = b"xx" + b'{"id":"1","role":"assistant","content":"hi"}' + b"yy"
            _make_store_db(db, meta_obj=meta, blob_id=root, blob_data=blob)

            # Persist derived title cache.
            cache = load_chat_title_cache(config_dir)
            set_cached_title(cache, cwd_hash=h, chat_id=chat_id, title="Derived Title")
            save_chat_title_cache(config_dir, cache)

            agent_dirs = CursorAgentDirs(config_dir=config_dir)
            workspaces = discover_agent_workspaces(agent_dirs, workspace_candidates=[ws_path])
            self.assertEqual(len(workspaces), 1)

            chats = discover_agent_chats(workspaces[0], with_preview=False)
            self.assertEqual(len(chats), 1)
            self.assertEqual(chats[0].name, "Derived Title")


if __name__ == "__main__":
    unittest.main()

