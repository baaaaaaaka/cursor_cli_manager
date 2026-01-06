import unittest
from pathlib import Path
from typing import Optional, Tuple

from cursor_cli_manager.models import AgentChat
from cursor_cli_manager.agent_title_cache import is_generic_chat_name
from cursor_cli_manager.tui import _hydrate_generic_titles


class TestTuiTitleDerivation(unittest.TestCase):
    def test_is_generic_chat_name(self) -> None:
        self.assertTrue(is_generic_chat_name("New Agent"))
        self.assertTrue(is_generic_chat_name("  untitled "))
        self.assertFalse(is_generic_chat_name("My Real Title"))

    def test_hydrate_generic_titles_updates_in_place(self) -> None:
        chats = [
            AgentChat(
                chat_id="c1",
                name="New Agent",
                created_at_ms=None,
                mode=None,
                latest_root_blob_id="root1",
                store_db_path=Path("/tmp/store1.db"),
            ),
            AgentChat(
                chat_id="c2",
                name="Already Named",
                created_at_ms=None,
                mode=None,
                latest_root_blob_id="root2",
                store_db_path=Path("/tmp/store2.db"),
            ),
        ]

        def get_preview(chat: AgentChat) -> Tuple[Optional[str], Optional[str]]:
            if chat.chat_id == "c1":
                return "history", "User:\nFix a bug in my code\n\nAssistant:\nSure"
            raise AssertionError("Should not call preview for non-generic named chat")

        done: set[str] = set()
        n, _idx, updates = _hydrate_generic_titles(
            chats, get_preview, done_ids=done, start_idx=0, max_items=10, budget_s=1.0
        )
        self.assertEqual(n, 1)
        self.assertEqual(updates, [("c1", "Fix a bug in my code")])
        self.assertEqual(chats[0].name, "Fix a bug in my code")
        self.assertEqual(chats[0].last_role, "history")
        self.assertIsInstance(chats[0].last_text, str)
        self.assertEqual(chats[1].name, "Already Named")
        # Non-candidates are marked done as well.
        self.assertEqual(done, {"c1", "c2"})


if __name__ == "__main__":
    unittest.main()

