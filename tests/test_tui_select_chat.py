import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest.mock import patch

from cursor_cli_manager.models import AgentChat, AgentWorkspace
from cursor_cli_manager.tui import ExportPendingExit, Theme, UpdateRequested, select_chat
from cursor_cli_manager.update import UpdateStatus


class _ImmediateThread:
    def __init__(self, *, target, daemon: bool = False, args=(), kwargs=None):  # noqa: ANN001
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.daemon = daemon

    def start(self) -> None:
        self._target(*self._args, **self._kwargs)


class _FakeWindow:
    def __init__(
        self,
        h: int,
        w: int,
        *,
        name: str = "win",
        inputs: Optional[List[int]] = None,
        root: Optional["_FakeWindow"] = None,
    ) -> None:
        self._h = h
        self._w = w
        self.name = name
        self._root = root or self
        if root is None:
            self._inputs = list(inputs or [])
        self.children: List["_FakeWindow"] = []
        self.timeout_ms: Optional[int] = None

    def getmaxyx(self) -> Tuple[int, int]:
        return self._h, self._w

    def derwin(self, h: int, w: int, y: int, x: int) -> "_FakeWindow":
        child = _FakeWindow(h, w, name=f"{self.name}.derwin({h}x{w}@{y},{x})", root=self._root)
        self.children.append(child)
        return child

    def getch(self) -> int:
        if getattr(self._root, "_inputs", []):
            return self._root._inputs.pop(0)
        return -1

    def keypad(self, _flag: bool) -> None:
        return

    def timeout(self, ms: int) -> None:
        self.timeout_ms = ms

    def leaveok(self, _flag: bool) -> None:
        return

    def idlok(self, _flag: bool) -> None:
        return

    def idcok(self, _flag: bool) -> None:
        return

    def scrollok(self, _flag: bool) -> None:
        return

    def noutrefresh(self) -> None:
        return

    def erase(self) -> None:
        return

    def box(self) -> None:
        return

    def hline(self, _y: int, _x: int, _ch, _n: int) -> None:  # noqa: ANN001
        return

    def addstr(self, _y: int, _x: int, _s: str, _attr: int = 0) -> None:
        return

    def scroll(self, _n: int) -> None:
        return

    def move(self, _y: int, _x: int) -> None:
        return


class _FakeBackgroundLoader:
    def __init__(
        self,
        *,
        chats_by_workspace: Dict[str, List[AgentChat]],
        full_by_chat: Optional[Dict[str, Tuple[Optional[str], Optional[str]]]] = None,
    ) -> None:
        self._queue: List[Tuple[object, ...]] = []
        self._chats_by_workspace = chats_by_workspace
        self._full_by_chat = full_by_chat or {}

    def ensure_chats(self, ws: AgentWorkspace) -> None:
        self._queue.append(("chats_ok", ws.cwd_hash, list(self._chats_by_workspace.get(ws.cwd_hash, []))))

    def ensure_preview_snippet(self, chat: AgentChat, *, max_messages: int) -> None:
        text = f"snippet:{chat.chat_id}:{max_messages}"
        self._queue.append(("preview_snippet_ok", chat.chat_id, max_messages, "history", text))

    def ensure_preview_full(self, chat: AgentChat) -> None:
        if chat.chat_id in self._full_by_chat:
            role, text = self._full_by_chat[chat.chat_id]
            self._queue.append(("preview_full_ok", chat.chat_id, role, text))

    def has_pending(self) -> bool:
        return bool(self._queue)

    def drain(self) -> List[Tuple[object, ...]]:
        out = list(self._queue)
        self._queue.clear()
        return out


class TestTuiSelectChat(unittest.TestCase):
    def _workspace(self, root: Path) -> AgentWorkspace:
        ws_path = root / "ws"
        ws_path.mkdir(parents=True, exist_ok=True)
        return AgentWorkspace(
            cwd_hash="ws-1",
            workspace_path=ws_path,
            chats_root=root / "cursor" / "chats" / "ws-1",
        )

    def _chat(self, root: Path) -> AgentChat:
        return AgentChat(
            chat_id="chat-1",
            name="Chat 1",
            created_at_ms=123,
            mode="default",
            latest_root_blob_id="root-1",
            store_db_path=root / "store.db",
        )

    def _run_select_chat(
        self,
        *,
        stdscr: _FakeWindow,
        workspaces: List[AgentWorkspace],
        loader: _FakeBackgroundLoader,
        update_status: Optional[UpdateStatus] = None,
        extra_patches: Optional[List[object]] = None,
    ):
        patches = [
            patch("cursor_cli_manager.tui._BackgroundLoader", return_value=loader),
            patch("cursor_cli_manager.tui.check_for_update", return_value=update_status or UpdateStatus(supported=False, error="no update")),
            patch("cursor_cli_manager.tui.threading.Thread", side_effect=lambda *a, **k: _ImmediateThread(*a, **k)),
            patch("cursor_cli_manager.tui.curses.curs_set", return_value=None),
            patch("cursor_cli_manager.tui.curses.mousemask", return_value=None),
            patch("cursor_cli_manager.tui.curses.doupdate", return_value=None),
            patch("cursor_cli_manager.tui._init_theme", return_value=Theme(0, 0)),
            patch("cursor_cli_manager.tui.load_chat_title_cache", return_value=None),
            patch("cursor_cli_manager.tui.save_chat_title_cache", return_value=None),
            patch("cursor_cli_manager.tui.set_cached_title", return_value=None),
        ]
        patches.extend(extra_patches or [])
        with ExitStack() as stack:
            for ctx in patches:
                stack.enter_context(ctx)
            return select_chat(
                stdscr,
                workspaces=workspaces,
                load_chats=lambda _ws: [],
                load_preview_snippet=lambda _chat, _max_messages: (None, None),
                load_preview_full=lambda _chat: (None, None),
            )

    def test_select_chat_enter_on_new_agent_returns_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = self._workspace(root)
            stdscr = _FakeWindow(30, 120, inputs=[9, 10])
            loader = _FakeBackgroundLoader(chats_by_workspace={})

            selection = self._run_select_chat(
                stdscr=stdscr,
                workspaces=[ws],
                loader=loader,
            )

            self.assertEqual(selection, (ws, None))

    def test_select_chat_enter_on_existing_chat_returns_chat(self) -> None:
        import curses

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = self._workspace(root)
            chat = self._chat(root)
            stdscr = _FakeWindow(30, 120, inputs=[-1, 9, curses.KEY_DOWN, 10])
            loader = _FakeBackgroundLoader(chats_by_workspace={ws.cwd_hash: [chat]})

            selection = self._run_select_chat(
                stdscr=stdscr,
                workspaces=[ws],
                loader=loader,
            )

            self.assertEqual(selection, (ws, chat))

    def test_select_chat_ctrl_u_raises_update_requested(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = self._workspace(root)
            stdscr = _FakeWindow(30, 120, inputs=[21])
            loader = _FakeBackgroundLoader(chats_by_workspace={})

            with self.assertRaises(UpdateRequested):
                self._run_select_chat(
                    stdscr=stdscr,
                    workspaces=[ws],
                    loader=loader,
                    update_status=UpdateStatus(supported=True, update_available=True),
                )

    def test_select_chat_ctrl_s_saves_cached_full_preview(self) -> None:
        import curses

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = self._workspace(root)
            chat = self._chat(root)
            out_path = root / "saved.md"
            stdscr = _FakeWindow(30, 120, inputs=[-1, 9, curses.KEY_DOWN, 9, -1, 19, ord("q")])
            loader = _FakeBackgroundLoader(
                chats_by_workspace={ws.cwd_hash: [chat]},
                full_by_chat={chat.chat_id: ("history", "full history")},
            )

            with patch("cursor_cli_manager.tui._prompt_save_path", return_value=out_path), patch(
                "cursor_cli_manager.tui.choose_nonconflicting_path", side_effect=lambda parent, name: parent / name
            ), patch("cursor_cli_manager.tui.write_text_file") as write_text_file:
                selection = self._run_select_chat(
                    stdscr=stdscr,
                    workspaces=[ws],
                    loader=loader,
                )

            self.assertIsNone(selection)
            write_text_file.assert_called_once_with(out_path, "full history")

    def test_select_chat_quit_with_pending_export_raises(self) -> None:
        import curses

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = self._workspace(root)
            chat = self._chat(root)
            out_path = root / "pending.md"
            stdscr = _FakeWindow(30, 120, inputs=[-1, 9, curses.KEY_DOWN, 9, 19, ord("q")])
            loader = _FakeBackgroundLoader(chats_by_workspace={ws.cwd_hash: [chat]})

            with patch("cursor_cli_manager.tui._prompt_save_path", return_value=out_path), patch(
                "cursor_cli_manager.tui.choose_nonconflicting_path", side_effect=lambda parent, name: parent / name
            ), patch("cursor_cli_manager.tui.write_text_file") as write_text_file:
                with self.assertRaises(ExportPendingExit) as ctx:
                    self._run_select_chat(
                        stdscr=stdscr,
                        workspaces=[ws],
                        loader=loader,
                    )

            self.assertEqual(ctx.exception.out_path, out_path)
            self.assertEqual(ctx.exception.store_db_path, chat.store_db_path)
            write_text_file.assert_called_once_with(out_path, "")


if __name__ == "__main__":
    unittest.main()
