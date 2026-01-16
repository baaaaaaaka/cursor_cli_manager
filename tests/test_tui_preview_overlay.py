import unittest
from dataclasses import dataclass
from typing import List, Optional, Tuple

import curses

from cursor_cli_manager.tui import Rect, _Pane


@dataclass
class _Op:
    kind: str
    y: Optional[int] = None
    x: Optional[int] = None
    n: Optional[int] = None
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

    def idlok(self, _flag: bool) -> None:
        self.ops.append(_Op("idlok"))

    def idcok(self, _flag: bool) -> None:
        self.ops.append(_Op("idcok"))

    def scrollok(self, _flag: bool) -> None:
        self.ops.append(_Op("scrollok"))

    def scroll(self, n: int) -> None:
        self.ops.append(_Op("scroll", n=n))

    def noutrefresh(self) -> None:
        self.ops.append(_Op("noutrefresh"))

    def erase(self) -> None:
        self.ops.append(_Op("erase"))

    def box(self) -> None:
        self.ops.append(_Op("box"))

    def hline(self, y: int, x: int, _ch: int, n: int) -> None:
        self.ops.append(_Op("hline", y=y, x=x, n=n))

    def addstr(self, y: int, x: int, s: str, attr: int = 0) -> None:
        self.ops.append(_Op("addstr", y=y, x=x, s=s, attr=attr))


class TestPreviewOverlayDrawing(unittest.TestCase):
    def test_bottom_overlay_reserves_last_row_and_clamps(self) -> None:
        stdscr = _FakeWindow(30, 120, name="stdscr")
        rect = Rect(0, 0, 6, 20)  # inner is 4 rows
        pane = _Pane(stdscr, rect)
        assert pane.inner is not None

        lines = [f"line {i}" for i in range(10)]

        pane.draw_preview_lines(lines, 0, use_terminal_scroll=False, bottom_overlay=("Loading…", curses.A_DIM), force=True)
        addstr = [op for op in pane.inner.ops if op.kind == "addstr"]
        self.assertTrue(any(op.y == 3 and (op.s or "").strip().startswith("Loading") for op in addstr))
        self.assertTrue(any(op.y == 0 and (op.s or "").strip() == "line 0" for op in addstr))
        self.assertTrue(any(op.y == 2 and (op.s or "").strip() == "line 2" for op in addstr))

        pane.inner.ops.clear()
        pane.draw_preview_lines(lines, 5, use_terminal_scroll=False, bottom_overlay=("Loading…", curses.A_DIM), force=False)
        addstr = [op for op in pane.inner.ops if op.kind == "addstr"]
        self.assertTrue(any(op.y == 0 and (op.s or "").strip() == "line 5" for op in addstr))
        self.assertTrue(any(op.y == 2 and (op.s or "").strip() == "line 7" for op in addstr))
        # Overlay may not be re-drawn if unchanged (cached), so don't require an addstr.
        # We validate overlay behavior via force redraw + overlay change below.

        # Clamp: inner_h=4 but content_h=3 (overlay takes last row),
        # so max_start = 10-3 = 7 => row0 should show "line 7".
        pane.inner.ops.clear()
        pane.draw_preview_lines(lines, 10_000, use_terminal_scroll=False, bottom_overlay=("Loading…", curses.A_DIM), force=False)
        addstr = [op for op in pane.inner.ops if op.kind == "addstr"]
        self.assertTrue(any(op.y == 0 and (op.s or "").strip() == "line 7" for op in addstr))

        # Changing overlay text should trigger a bottom-row addstr even without force.
        pane.inner.ops.clear()
        pane.draw_preview_lines(lines, 7, use_terminal_scroll=False, bottom_overlay=("Loading 2…", curses.A_DIM), force=False)
        addstr = [op for op in pane.inner.ops if op.kind == "addstr"]
        self.assertTrue(any(op.y == 3 and (op.s or "").strip().startswith("Loading 2") for op in addstr))

    def test_overlay_disables_terminal_scroll(self) -> None:
        stdscr = _FakeWindow(30, 120, name="stdscr")
        rect = Rect(0, 0, 6, 20)  # inner is 4 rows
        pane = _Pane(stdscr, rect)
        assert pane.inner is not None

        lines = [f"line {i}" for i in range(10)]

        # First draw establishes baseline.
        pane.draw_preview_lines(lines, 0, use_terminal_scroll=True, bottom_overlay=("Loading…", curses.A_DIM), force=True)
        pane.inner.ops.clear()

        # Small scroll delta: even though use_terminal_scroll=True, overlay should force it off.
        pane.draw_preview_lines(lines, 1, use_terminal_scroll=True, bottom_overlay=("Loading…", curses.A_DIM), force=False)
        kinds = [op.kind for op in pane.inner.ops]
        self.assertNotIn("scroll", kinds)

    def test_preview_draw_supports_line_highlighting(self) -> None:
        stdscr = _FakeWindow(30, 120, name="stdscr")
        rect = Rect(0, 0, 6, 20)  # inner is 4 rows
        pane = _Pane(stdscr, rect)
        assert pane.inner is not None

        lines = ["aaa", "bbb", "ccc", "ddd"]
        line_attrs = {1: curses.A_REVERSE}
        pane.draw_preview_lines(lines, 0, use_terminal_scroll=False, line_attrs=line_attrs, force=True)

        addstr = [op for op in pane.inner.ops if op.kind == "addstr"]
        # Row 1 corresponds to lines[1] when start=0.
        row1 = [op for op in addstr if op.y == 1]
        self.assertTrue(row1, "expected row 1 to be drawn")
        self.assertTrue(any((op.attr or 0) & curses.A_REVERSE for op in row1))


if __name__ == "__main__":
    unittest.main()

