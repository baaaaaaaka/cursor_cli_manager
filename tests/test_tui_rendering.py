import unittest
from pathlib import Path
from random import Random

from cursor_cli_manager.formatting import display_width
from cursor_cli_manager.models import AgentChat, AgentWorkspace
from cursor_cli_manager.tui import ListState, Rect, Theme, _list_rows, _preview_rows, compute_layout


class TestTuiLayoutInvariants(unittest.TestCase):
    def test_layout_invariants_random_sizes(self) -> None:
        rng = Random(0)
        # Sample a bunch of terminal sizes, including tiny ones.
        for _ in range(500):
            max_y = rng.randint(1, 80)
            max_x = rng.randint(10, 220)
            usable_h = max(1, max_y - 1)
            layout = compute_layout(max_y, max_x)

            for rect in (layout.workspaces, layout.conversations, layout.preview):
                self.assertGreaterEqual(rect.x, 0)
                self.assertGreaterEqual(rect.y, 0)
                self.assertGreaterEqual(rect.w, 0)
                self.assertGreaterEqual(rect.h, 0)
                self.assertLessEqual(rect.x + rect.w, max_x)
                self.assertLessEqual(rect.y + rect.h, usable_h)

            if layout.mode == "3col":
                self.assertEqual(layout.workspaces.y, 0)
                self.assertEqual(layout.conversations.y, 0)
                self.assertEqual(layout.preview.y, 0)
                self.assertEqual(layout.workspaces.h, usable_h)
                self.assertEqual(layout.conversations.h, usable_h)
                self.assertEqual(layout.preview.h, usable_h)
                self.assertEqual(layout.workspaces.x, 0)
                self.assertEqual(layout.conversations.x, layout.workspaces.w)
                self.assertEqual(layout.preview.x, layout.workspaces.w + layout.conversations.w)
                self.assertEqual(layout.workspaces.w + layout.conversations.w + layout.preview.w, max_x)

            elif layout.mode == "2col":
                self.assertEqual(layout.workspaces.y, 0)
                self.assertEqual(layout.workspaces.h, usable_h)
                self.assertEqual(layout.workspaces.x, 0)
                self.assertEqual(layout.conversations.x, layout.workspaces.w)
                self.assertEqual(layout.preview.x, layout.workspaces.w)
                self.assertEqual(layout.conversations.y, 0)
                self.assertEqual(layout.preview.y, layout.conversations.h)
                self.assertEqual(layout.conversations.h + layout.preview.h, usable_h)

            elif layout.mode == "1col":
                self.assertEqual(layout.workspaces, layout.conversations)
                self.assertEqual(layout.workspaces.x, 0)
                self.assertEqual(layout.preview.x, 0)
                self.assertEqual(layout.workspaces.w, max_x)
                self.assertEqual(layout.preview.w, max_x)
                self.assertEqual(layout.preview.y, layout.workspaces.h)
                self.assertEqual(layout.workspaces.h + layout.preview.h, usable_h)

            else:
                self.fail(f"Unknown layout mode: {layout.mode}")


class TestTuiRenderingModels(unittest.TestCase):
    def test_list_rows_exact_width(self) -> None:
        rect = Rect(0, 0, 12, 24)  # inner is 10x22
        theme = Theme(focused_selected_attr=1, unfocused_selected_attr=2)
        state = ListState()
        state.selected = 2
        state.scroll = 0
        items = [
            ("short", object()),
            ("含中文的标签", object()),  # CJK wide chars
            ("a very very long label that will be truncated", object()),
        ]
        rows = _list_rows(rect, items, state, focused=True, filter_text="", theme=theme)
        self.assertEqual(len(rows), rect.h - 2)
        for text, _attr in rows:
            self.assertEqual(display_width(text), rect.w - 2)

    def test_preview_rows_exact_width(self) -> None:
        rect = Rect(0, 0, 10, 30)  # inner is 8x28
        ws = AgentWorkspace(cwd_hash="abc", workspace_path=Path("/tmp/ws"), chats_root=Path("/tmp/chats"))
        chat = AgentChat(
            chat_id="cid",
            name="new agent",
            created_at_ms=0,
            mode="agent",
            latest_root_blob_id="root",
            store_db_path=Path("/tmp/store.db"),
            last_role="history",
            last_text="你好 world\nsecond line with more text",
        )
        rows = _preview_rows(rect, ws, chat, message=None)
        self.assertEqual(len(rows), rect.h - 2)
        for text, _attr in rows:
            self.assertEqual(display_width(text), rect.w - 2)


if __name__ == "__main__":
    unittest.main()

