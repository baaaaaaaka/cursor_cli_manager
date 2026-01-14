import unittest

import curses

from cursor_cli_manager.tui import _input_timeout_ms, _should_quit


class TestTuiInputHandling(unittest.TestCase):
    def test_should_quit_ignores_q_and_Q_in_search_mode(self) -> None:
        self.assertFalse(_should_quit(ch=ord("q"), input_mode="ws"))
        self.assertFalse(_should_quit(ch=ord("Q"), input_mode="chat"))

    def test_should_quit_allows_q_and_Q_when_not_searching(self) -> None:
        self.assertTrue(_should_quit(ch=ord("q"), input_mode=None))
        self.assertTrue(_should_quit(ch=ord("Q"), input_mode=None))

    def test_should_quit_allows_key_exit_when_available(self) -> None:
        key_exit = getattr(curses, "KEY_EXIT", None)
        if isinstance(key_exit, int):
            self.assertTrue(_should_quit(ch=key_exit, input_mode=None))
            self.assertFalse(_should_quit(ch=key_exit, input_mode="ws"))

    def test_input_timeout_ms_polls_when_bg_or_update_pending(self) -> None:
        self.assertEqual(_input_timeout_ms(bg_pending=False, update_checking=False, ui_pending=False), -1)
        self.assertEqual(_input_timeout_ms(bg_pending=True, update_checking=False, ui_pending=False), 80)
        self.assertEqual(_input_timeout_ms(bg_pending=False, update_checking=True, ui_pending=False), 80)
        self.assertEqual(_input_timeout_ms(bg_pending=False, update_checking=False, ui_pending=True), 80)


if __name__ == "__main__":
    unittest.main()

