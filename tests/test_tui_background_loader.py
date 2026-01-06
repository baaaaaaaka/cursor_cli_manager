import threading
import time
import unittest
from pathlib import Path
from typing import List, Optional, Tuple

from cursor_cli_manager.models import AgentChat, AgentWorkspace
from cursor_cli_manager.tui import _BackgroundLoader


class TestTuiBackgroundLoader(unittest.TestCase):
    def test_ensure_chats_is_non_blocking(self) -> None:
        ws = AgentWorkspace(cwd_hash="h", workspace_path=Path("/tmp/ws"), chats_root=Path("/tmp/chats/h"))
        evt = threading.Event()

        def load_chats(_ws: AgentWorkspace) -> List[AgentChat]:
            # Block until the test allows it to finish.
            evt.wait(timeout=2.0)
            return [
                AgentChat(
                    chat_id="c1",
                    name="Chat",
                    created_at_ms=None,
                    mode=None,
                    latest_root_blob_id=None,
                    store_db_path=Path("/tmp/store.db"),
                )
            ]

        def load_preview(_chat: AgentChat) -> Tuple[Optional[str], Optional[str]]:
            return None, None

        bg = _BackgroundLoader(load_chats=load_chats, load_preview=load_preview)

        t0 = time.monotonic()
        bg.ensure_chats(ws)
        # Must return immediately (well under the event timeout).
        self.assertLess(time.monotonic() - t0, 0.2)

        # No result until we release the event.
        self.assertEqual(bg.drain(), [])
        evt.set()

        # Eventually we should see a chats_ok message.
        deadline = time.monotonic() + 2.0
        seen = False
        while time.monotonic() < deadline:
            for msg in bg.drain():
                if msg[0] == "chats_ok":
                    seen = True
                    break
            if seen:
                break
            time.sleep(0.01)
        self.assertTrue(seen)


if __name__ == "__main__":
    unittest.main()

