import unittest
from dataclasses import dataclass
from typing import List, Optional, Tuple

from cursor_cli_manager.tui import ListState, Rect, Theme, _Pane, _list_rows


@dataclass
class _Op:
    kind: str  # "box" | "hline" | "addstr" | "noutrefresh" | "erase"
    y: Optional[int] = None
    x: Optional[int] = None
    n: Optional[int] = None
    s: Optional[str] = None
    attr: Optional[int] = None


class _FakeWindow:
    """
    A minimal curses-like window that records draw calls.

    This is used to test that we only touch small regions (no full box redraw)
    during normal navigation (focus/selection changes).
    """

    def __init__(self, h: int, w: int, *, name: str = "win") -> None:
        self._h = h
        self._w = w
        self.name = name
        self.ops: List[_Op] = []
        self.children: List["_FakeWindow"] = []

    def getmaxyx(self) -> Tuple[int, int]:
        return self._h, self._w

    def derwin(self, h: int, w: int, y: int, x: int) -> "_FakeWindow":
        # Size/position are trusted; we only track size for tests.
        child = _FakeWindow(h, w, name=f"{self.name}.derwin({h}x{w}@{y},{x})")
        self.children.append(child)
        return child

    def leaveok(self, _flag: bool) -> None:
        return

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


class TestTuiRefreshRegions(unittest.TestCase):
    def test_pane_focus_switch_does_not_redraw_border(self) -> None:
        stdscr = _FakeWindow(50, 200, name="stdscr")
        rect = Rect(0, 0, 12, 40)
        pane = _Pane(stdscr, rect)

        theme = Theme(focused_selected_attr=10, unfocused_selected_attr=20)
        items = [("one", object()), ("two", object()), ("three", object())]
        state = ListState()
        state.selected = 1

        rows_focused = _list_rows(rect, items, state, focused=True, filter_text="", theme=theme)
        rows_unfocused = _list_rows(rect, items, state, focused=False, filter_text="", theme=theme)

        # Initial draw (allowed to draw border once).
        pane.draw_frame("Workspaces", focused=True, filter_text="", force=True)
        pane.draw_inner_rows(rows_focused, force=True)
        pane.outer.ops.clear()
        if pane.inner:
            pane.inner.ops.clear()

        # Focus switch should NOT call box() again.
        pane.draw_frame("Workspaces", focused=False, filter_text="", force=False)
        pane.draw_inner_rows(rows_unfocused, force=False)

        outer_kinds = [op.kind for op in pane.outer.ops]
        self.assertNotIn("box", outer_kinds)
        # Title update should touch only small areas (hline + addstr).
        self.assertIn("addstr", outer_kinds)

        if pane.inner:
            inner_kinds = [op.kind for op in pane.inner.ops]
            # Only selected row attribute should change => 1 line rewrite.
            self.assertEqual(inner_kinds.count("addstr"), 1)

    def test_focus_flip_two_panes_updates_titles_and_selected_rows_only(self) -> None:
        stdscr = _FakeWindow(60, 240, name="stdscr")
        rect_ws = Rect(0, 0, 20, 40)
        rect_chats = Rect(0, 40, 20, 60)
        ws_pane = _Pane(stdscr, rect_ws)
        chats_pane = _Pane(stdscr, rect_chats)

        theme = Theme(focused_selected_attr=1, unfocused_selected_attr=2)
        ws_items = [("ws1", object()), ("ws2", object())]
        chat_items = [("(New Agent)", object()), ("chat A", object()), ("chat B", object())]

        ws_state = ListState()
        ws_state.selected = 0
        chat_state = ListState()
        chat_state.selected = 1

        # Initial draw with focus on workspaces.
        ws_pane.draw_frame("Workspaces", focused=True, filter_text="", force=True)
        ws_pane.draw_inner_rows(_list_rows(rect_ws, ws_items, ws_state, focused=True, filter_text="", theme=theme), force=True)
        chats_pane.draw_frame("Chat Sessions", focused=False, filter_text="", force=True)
        chats_pane.draw_inner_rows(
            _list_rows(rect_chats, chat_items, chat_state, focused=False, filter_text="", theme=theme), force=True
        )
        ws_pane.outer.ops.clear()
        chats_pane.outer.ops.clear()
        if ws_pane.inner:
            ws_pane.inner.ops.clear()
        if chats_pane.inner:
            chats_pane.inner.ops.clear()

        # Flip focus to chats.
        ws_pane.draw_frame("Workspaces", focused=False, filter_text="", force=False)
        ws_pane.draw_inner_rows(_list_rows(rect_ws, ws_items, ws_state, focused=False, filter_text="", theme=theme), force=False)
        chats_pane.draw_frame("Chat Sessions", focused=True, filter_text="", force=False)
        chats_pane.draw_inner_rows(
            _list_rows(rect_chats, chat_items, chat_state, focused=True, filter_text="", theme=theme), force=False
        )

        self.assertNotIn("box", [op.kind for op in ws_pane.outer.ops])
        self.assertNotIn("box", [op.kind for op in chats_pane.outer.ops])

        # Each pane should rewrite exactly one selected row (attr changes).
        if ws_pane.inner and chats_pane.inner:
            self.assertEqual([op.kind for op in ws_pane.inner.ops].count("addstr"), 1)
            self.assertEqual([op.kind for op in chats_pane.inner.ops].count("addstr"), 1)


if __name__ == "__main__":
    unittest.main()

