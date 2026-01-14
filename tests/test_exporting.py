import datetime as dt
import tempfile
import unittest
from pathlib import Path

from cursor_cli_manager.exporting import (
    build_export_filename,
    choose_nonconflicting_path,
    sanitize_filename_component,
    tab_complete_path,
    write_text_file,
)


class TestExporting(unittest.TestCase):
    def test_sanitize_filename_component_removes_invalid_chars_and_limits(self) -> None:
        s = '  a/b\\c:d*e?f"g<h>i|j  \n'
        out = sanitize_filename_component(s, max_len=10)
        self.assertTrue(out)
        self.assertNotIn("/", out)
        self.assertNotIn("\\", out)
        self.assertNotIn(" ", out)
        self.assertLessEqual(len(out), 10)

    def test_build_export_filename_contains_timestamp_and_ext(self) -> None:
        when = dt.datetime(2026, 1, 2, 3, 4, 5)
        fn = build_export_filename(title="Hello", when=when, ext=".md")
        self.assertTrue(fn.startswith("2026-01-02_03-04-05_"))
        self.assertTrue(fn.endswith(".md"))

    def test_choose_nonconflicting_path_appends_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            p1 = choose_nonconflicting_path(d, "a.md")
            write_text_file(p1, "x")
            p2 = choose_nonconflicting_path(d, "a.md")
            self.assertNotEqual(p1, p2)
            self.assertTrue(p2.name.startswith("a-"))

    def test_tab_complete_path_completes_common_prefix_and_directories(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "abc.txt").write_text("x", encoding="utf-8")
            (d / "abd.txt").write_text("x", encoding="utf-8")
            (d / "folder").mkdir()

            t, c = tab_complete_path("a", 1, cwd=d)
            self.assertEqual(t, "ab")
            self.assertEqual(c, 2)

            t2, c2 = tab_complete_path("abc", 3, cwd=d)
            self.assertEqual(t2, "abc.txt")
            self.assertEqual(c2, len("abc.txt"))

            t3, c3 = tab_complete_path("fol", 3, cwd=d)
            self.assertTrue(t3.startswith("folder"))
            self.assertTrue(t3.endswith("/") or t3.endswith("\\"))
            self.assertEqual(c3, len(t3))


if __name__ == "__main__":
    unittest.main()

