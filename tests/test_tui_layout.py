import unittest

from cursor_cli_manager.tui import compute_layout


class TestTuiLayout(unittest.TestCase):
    def test_layout_3col(self) -> None:
        layout = compute_layout(40, 160)
        self.assertEqual(layout.mode, "3col")

    def test_layout_2col(self) -> None:
        layout = compute_layout(30, 100)
        self.assertEqual(layout.mode, "2col")

    def test_layout_1col(self) -> None:
        layout = compute_layout(20, 70)
        self.assertEqual(layout.mode, "1col")


if __name__ == "__main__":
    unittest.main()

