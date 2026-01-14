import unittest
from dataclasses import dataclass
from typing import List, Optional, Tuple
from unittest.mock import patch

from cursor_cli_manager.tui import Rect, _Pane, _sync_output_begin, _sync_output_end, probe_synchronized_output_support
from cursor_cli_manager.tui import disable_xon_xoff_flow_control, restore_termios


@dataclass
class _Op:
    kind: str
    y: Optional[int] = None
    x: Optional[int] = None
    s: Optional[str] = None
    attr: Optional[int] = None


class _FakeWindow:
    def __init__(self, h: int, w: int, *, name: str = "win") -> None:
        self._h = h
        self._w = w
        self.name = name
        self.ops: List[_Op] = []
        self.children: List["_FakeWindow"] = []

    def getmaxyx(self) -> Tuple[int, int]:
        return self._h, self._w

    def derwin(self, h: int, w: int, y: int, x: int) -> "_FakeWindow":
        child = _FakeWindow(h, w, name=f"{self.name}.derwin({h}x{w}@{y},{x})")
        self.children.append(child)
        return child

    def leaveok(self, _flag: bool) -> None:
        return

    def noutrefresh(self) -> None:
        self.ops.append(_Op("noutrefresh"))

    def addstr(self, y: int, x: int, s: str, attr: int = 0) -> None:
        self.ops.append(_Op("addstr", y=y, x=x, s=s, attr=attr))


class TestTuiMisc(unittest.TestCase):
    def test_draw_inner_rows_skips_noutrefresh_when_unchanged(self) -> None:
        stdscr = _FakeWindow(40, 120, name="stdscr")
        rect = Rect(0, 0, 6, 20)  # inner is 4 rows
        pane = _Pane(stdscr, rect)
        assert pane.inner is not None

        rows = [("hello".ljust(18), 0), ("".ljust(18), 0), ("".ljust(18), 0), ("".ljust(18), 0)]
        pane.draw_inner_rows(rows, force=True)
        pane.inner.ops.clear()

        # Same rows again -> should not schedule a refresh.
        pane.draw_inner_rows(rows, force=False)
        self.assertEqual([op.kind for op in pane.inner.ops], [])

    def test_probe_synchronized_output_support_returns_false_when_not_tty(self) -> None:
        # In most test environments stdin/stdout are not ttys. Make it explicit.
        with patch("sys.stdin.isatty", return_value=False), patch("sys.stdout.isatty", return_value=False):
            self.assertFalse(probe_synchronized_output_support(timeout_s=0.01))

    def test_sync_output_wrappers_swallow_os_write_errors(self) -> None:
        with patch("os.write", side_effect=OSError("nope")):
            # Must not raise.
            _sync_output_begin()
            _sync_output_end()

    def test_disable_xon_xoff_returns_none_when_not_tty(self) -> None:
        with patch("sys.stdin.isatty", return_value=False):
            self.assertIsNone(disable_xon_xoff_flow_control())

    def test_restore_termios_is_noop_on_none(self) -> None:
        # Must not raise.
        restore_termios(None)


if __name__ == "__main__":
    unittest.main()

