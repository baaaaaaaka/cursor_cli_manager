import unittest

import curses

from cursor_cli_manager.tui import _preview_find_matches, _preview_scroll_to_match


class TestTuiPreviewSearch(unittest.TestCase):
    def test_preview_find_matches_case_insensitive(self) -> None:
        lines = ["Hello world", "nothing", "heLLo again", ""]
        self.assertEqual(_preview_find_matches(lines, "hello"), [0, 2])
        self.assertEqual(_preview_find_matches(lines, "WORLD"), [0])

    def test_preview_find_matches_empty_returns_empty(self) -> None:
        self.assertEqual(_preview_find_matches(["a", "b"], ""), [])
        self.assertEqual(_preview_find_matches(["a", "b"], "   "), [])

    def test_preview_scroll_to_match_centers_best_effort(self) -> None:
        # view_h=9 => center offset is 4
        self.assertEqual(_preview_scroll_to_match(0, view_h=9), 0)
        self.assertEqual(_preview_scroll_to_match(4, view_h=9), 0)
        self.assertEqual(_preview_scroll_to_match(5, view_h=9), 1)
        self.assertEqual(_preview_scroll_to_match(20, view_h=9), 16)

    def test_preview_scroll_to_match_handles_tiny_view(self) -> None:
        self.assertEqual(_preview_scroll_to_match(10, view_h=0), 10)
        self.assertEqual(_preview_scroll_to_match(10, view_h=1), 10)


if __name__ == "__main__":
    unittest.main()

