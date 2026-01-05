import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cursor_cli_manager.vscdb import read_json, read_value


def _make_vscdb(db_path: Path, items: dict[str, object]) -> None:
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);")
    for k, v in items.items():
        if isinstance(v, (bytes, bytearray, memoryview)):
            val = bytes(v)
        else:
            val = v
        con.execute("INSERT INTO ItemTable(key, value) VALUES(?, ?);", (k, val))
    con.commit()
    con.close()


class TestVscdb(unittest.TestCase):
    def test_read_value_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "state.vscdb"
            _make_vscdb(db, {"foo": "bar"})
            self.assertEqual(read_value(db, "foo"), "bar")
            self.assertIsNone(read_value(db, "missing"))

    def test_read_json_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "state.vscdb"
            _make_vscdb(db, {"j": json.dumps({"a": 1})})
            self.assertEqual(read_json(db, "j"), {"a": 1})

    def test_read_json_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "state.vscdb"
            _make_vscdb(db, {"j": json.dumps({"a": 2}).encode("utf-8")})
            self.assertEqual(read_json(db, "j"), {"a": 2})


if __name__ == "__main__":
    unittest.main()

