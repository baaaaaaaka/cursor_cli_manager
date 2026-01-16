import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import curses
import sys


class TestCliTermPreflight(unittest.TestCase):
    def test_prepare_curses_term_uses_bundled_terminfo_when_available(self) -> None:
        from cursor_cli_manager.cli import _prepare_curses_term_for_tui

        with tempfile.TemporaryDirectory() as td:
            meipass = Path(td)
            (meipass / "terminfo").mkdir(parents=True, exist_ok=True)
            bundled = str(meipass / "terminfo")

            def fake_setupterm(*_args, **kwargs):
                term = kwargs.get("term")
                if os.environ.get("TERMINFO") == bundled and term == "xterm-256color":
                    return None
                raise curses.error("setupterm: could not find terminal")

            with patch.dict(os.environ, {"TERM": "xterm-kitty"}, clear=True), patch.object(
                sys, "frozen", True, create=True
            ), patch.object(sys, "_MEIPASS", str(meipass), create=True), patch(
                "cursor_cli_manager.cli.curses.setupterm", side_effect=fake_setupterm
            ):
                _prepare_curses_term_for_tui()
                self.assertEqual(os.environ.get("TERMINFO"), bundled)
                self.assertEqual(os.environ.get("TERM"), "xterm-256color")

    def test_prepare_curses_term_uses_executable_adjacent_terminfo_in_onedir(self) -> None:
        from cursor_cli_manager.cli import _prepare_curses_term_for_tui

        with tempfile.TemporaryDirectory() as td:
            dist = Path(td)
            (dist / "terminfo").mkdir(parents=True, exist_ok=True)
            bundled_p = (dist / "terminfo").resolve()
            bundled = str(bundled_p)

            def fake_setupterm(*_args, **kwargs):
                term = kwargs.get("term")
                ti = os.environ.get("TERMINFO") or ""
                if (Path(ti).resolve() if ti else None) == bundled_p and term == "xterm":
                    return None
                raise curses.error("setupterm: could not find terminal")

            with patch.dict(os.environ, {"TERM": "wezterm"}, clear=True), patch.object(
                sys, "frozen", True, create=True
            ), patch.object(sys, "_MEIPASS", None, create=True), patch.object(
                sys, "executable", str(dist / "ccm"), create=True
            ), patch(
                "cursor_cli_manager.cli.curses.setupterm", side_effect=fake_setupterm
            ):
                _prepare_curses_term_for_tui()
                self.assertEqual(Path(os.environ.get("TERMINFO") or "").resolve(), bundled_p)
                self.assertEqual(os.environ.get("TERM"), "xterm")

    def test_prepare_curses_term_does_not_override_user_terminfo(self) -> None:
        from cursor_cli_manager.cli import _prepare_curses_term_for_tui

        with tempfile.TemporaryDirectory() as td:
            meipass = Path(td)
            (meipass / "terminfo").mkdir(parents=True, exist_ok=True)
            bundled = str(meipass / "terminfo")

            def fake_setupterm(*_args, **kwargs):
                term = kwargs.get("term")
                if os.environ.get("TERMINFO") == bundled and term == "xterm-256color":
                    return None
                raise curses.error("setupterm: could not find terminal")

            with patch.dict(os.environ, {"TERM": "xterm-kitty", "TERMINFO": "/custom"}, clear=True), patch.object(
                sys, "frozen", True, create=True
            ), patch.object(sys, "_MEIPASS", str(meipass), create=True), patch(
                "cursor_cli_manager.cli.curses.setupterm", side_effect=fake_setupterm
            ):
                _prepare_curses_term_for_tui()
                self.assertEqual(os.environ.get("TERMINFO"), "/custom")
                self.assertEqual(os.environ.get("TERM"), "xterm-kitty")


if __name__ == "__main__":
    unittest.main()

