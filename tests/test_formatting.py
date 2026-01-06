import unittest

from cursor_cli_manager.formatting import center_to_width, display_width, pad_to_width, truncate_to_width, wrap_text


class TestFormatting(unittest.TestCase):
    def test_display_width_ascii(self) -> None:
        self.assertEqual(display_width("abc"), 3)

    def test_display_width_cjk(self) -> None:
        # East Asian Wide characters should count as 2 columns.
        self.assertEqual(display_width("你"), 2)
        self.assertEqual(display_width("你好"), 4)

    def test_display_width_combining(self) -> None:
        # "e" + combining acute accent
        s = "e\u0301"
        self.assertEqual(display_width(s), 1)

    def test_truncate_to_width(self) -> None:
        self.assertEqual(truncate_to_width("hello", 5), "hello")
        self.assertEqual(truncate_to_width("hello", 4), "hel…")

    def test_pad_to_width_ascii(self) -> None:
        self.assertEqual(pad_to_width("abc", 5), "abc  ")
        self.assertEqual(display_width(pad_to_width("abc", 5)), 5)

    def test_pad_to_width_cjk(self) -> None:
        # "你" is width 2.
        s = pad_to_width("你", 4)
        self.assertEqual(display_width(s), 4)
        self.assertTrue(s.startswith("你"))

    def test_center_to_width(self) -> None:
        s = center_to_width("hi", 6)
        self.assertEqual(display_width(s), 6)
        self.assertIn("hi", s)

    def test_wrap_text(self) -> None:
        lines = wrap_text("hello world", 5)
        self.assertGreaterEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()

