import unittest
from dataclasses import dataclass
from typing import List, Optional, Tuple

import curses

from cursor_cli_manager.tui import _StatusBar


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


class TestStatusBar(unittest.TestCase):
    def test_right_segment_is_right_aligned_and_can_be_bold(self) -> None:
        stdscr = _FakeWindow(10, 40, name="stdscr")
        bar = _StatusBar(stdscr, max_y=10, max_x=40)

        bar.draw("left", right="v0.1.0 已是最新", right_attr=0, force=True)
        ops = [op for op in bar.win.ops if op.kind == "addstr"]
        # Base bar at x=0.
        self.assertTrue(any(op.x == 0 and op.attr == curses.A_REVERSE for op in ops))
        # Right segment should be drawn near the right edge.
        self.assertTrue(any((op.s or "").strip().startswith("v0.1.0") and op.attr == curses.A_REVERSE for op in ops))

        bar.win.ops.clear()
        bar.draw("left", right="Ctrl+U 更新", right_attr=curses.A_BOLD, force=True)
        ops = [op for op in bar.win.ops if op.kind == "addstr"]
        self.assertTrue(any((op.s or "").strip().startswith("Ctrl+U") and (op.attr or 0) & curses.A_BOLD for op in ops))


if __name__ == "__main__":
    unittest.main()

