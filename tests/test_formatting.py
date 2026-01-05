import unittest

from cursor_cli_manager.formatting import display_width, truncate_to_width, wrap_text


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

    def test_wrap_text(self) -> None:
        lines = wrap_text("hello world", 5)
        self.assertGreaterEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()

