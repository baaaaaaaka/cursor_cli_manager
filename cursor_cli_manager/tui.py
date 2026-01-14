from __future__ import annotations

import curses
import os
import queue
import sys
import threading
import time
import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple, Union
from cursor_cli_manager.agent_title_cache import (
    ChatTitleCache,
    is_generic_chat_name,
    load_chat_title_cache,
    save_chat_title_cache,
    set_cached_title,
)
from cursor_cli_manager.formatting import (
    clamp,
    display_width,
    format_epoch_ms,
    pad_to_width,
    truncate_to_width,
    wrap_text,
)
from cursor_cli_manager.models import AgentChat, AgentWorkspace
from cursor_cli_manager.update import UpdateStatus, check_for_update
from cursor_cli_manager import __version__
from cursor_cli_manager.exporting import build_export_filename, choose_nonconflicting_path, tab_complete_path, write_text_file


@dataclass(frozen=True)
class Theme:
    focused_selected_attr: int
    unfocused_selected_attr: int


@dataclass(frozen=True)
class NewAgentItem:
    """
    Synthetic list row that represents starting a brand-new cursor-agent session
    in the selected workspace.
    """

    always_visible: bool = True


NEW_AGENT_ITEM = NewAgentItem()


@dataclass(frozen=True)
class LoadingItem:
    always_visible: bool = True


@dataclass(frozen=True)
class ErrorItem:
    message: str
    always_visible: bool = True


LOADING_ITEM = LoadingItem()


def _spinner(t: float) -> str:
    # Simple ASCII-safe spinner (works everywhere).
    frames = ["|", "/", "-", "\\"]
    idx = int(t * 10) % len(frames)  # ~10 FPS
    return frames[idx]


_CSI = "\x1b["


def probe_synchronized_output_support(*, timeout_s: float = 0.05) -> bool:
    """
    Best-effort probe for xterm "synchronized output" support.

    We use DECRQM to query private mode 2026:
      CSI ? 2026 $ p
    Expected response:
      CSI ? 2026 ; Ps $ y
    Where Ps in {1,2,3,4} means the mode is recognized (supported).
    Ps==0 means "not recognized".

    If we can't confidently detect support, we return False.
    """
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
        in_fd = sys.stdin.fileno()
        out_fd = sys.stdout.fileno()
    except Exception:
        return False

    try:
        import fcntl  # POSIX
        import select  # POSIX
        import termios  # POSIX
        import tty  # POSIX
    except Exception:
        return False

    try:
        orig_attr = termios.tcgetattr(in_fd)
        orig_flags = fcntl.fcntl(in_fd, fcntl.F_GETFL)
    except Exception:
        return False

    buf = b""
    try:
        # Cbreak mode lets us read responses without waiting for a newline.
        tty.setcbreak(in_fd)
        try:
            fcntl.fcntl(in_fd, fcntl.F_SETFL, orig_flags | os.O_NONBLOCK)
        except Exception:
            pass

        # Query the mode.
        try:
            os.write(out_fd, b"\x1b[?2026$p")
        except Exception:
            return False

        deadline = time.monotonic() + max(0.0, timeout_s)
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                r, _, _ = select.select([in_fd], [], [], remaining)
            except Exception:
                break
            if not r:
                break
            try:
                chunk = os.read(in_fd, 4096)
            except BlockingIOError:
                continue
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            # If we see the terminator, we likely have the full response.
            if b"$y" in buf and b"\x1b[?2026;" in buf:
                break
    finally:
        # Restore terminal settings.
        try:
            import termios  # type: ignore[no-redef]

            termios.tcsetattr(in_fd, termios.TCSADRAIN, orig_attr)
        except Exception:
            pass
        try:
            import fcntl  # type: ignore[no-redef]

            fcntl.fcntl(in_fd, fcntl.F_SETFL, orig_flags)
        except Exception:
            pass

    # Parse: ESC[?2026;Ps$y
    marker = b"\x1b[?2026;"
    i = buf.find(marker)
    if i < 0:
        return False
    tail = buf[i + len(marker) :]
    digits = bytearray()
    for b in tail:
        if 48 <= b <= 57:
            digits.append(b)
            continue
        break
    if not digits:
        return False
    try:
        ps = int(digits.decode("ascii", "ignore") or "0")
    except Exception:
        return False
    # DECRQM Ps: 0=not recognized; 1=set; 2=reset; 3=permanently set; 4=permanently reset.
    return ps in (1, 2, 3, 4)


def _sync_output_begin() -> None:
    # xterm "synchronized output" begin: DECSET 2026.
    try:
        os.write(sys.stdout.fileno(), b"\x1b[?2026h")
    except Exception:
        return


def _sync_output_end() -> None:
    # xterm "synchronized output" end: DECRST 2026.
    try:
        os.write(sys.stdout.fileno(), b"\x1b[?2026l")
    except Exception:
        return


def _init_theme() -> Theme:
    # Fallback theme (no color support).
    focused = curses.A_REVERSE | curses.A_BOLD
    unfocused = curses.A_REVERSE | curses.A_DIM

    if not curses.has_colors():
        return Theme(focused_selected_attr=focused, unfocused_selected_attr=unfocused)

    try:
        curses.start_color()
    except Exception:
        return Theme(focused_selected_attr=focused, unfocused_selected_attr=unfocused)

    try:
        curses.use_default_colors()
    except Exception:
        pass

    colors = getattr(curses, "COLORS", 0) or 0
    if colors >= 256:
        # Light gray background (slightly dimmer than pure white).
        grey_bg = 245
        unfocused_fg = curses.COLOR_BLACK
    elif colors >= 16:
        # Bright black is typically a dark gray in 16-color terminals.
        grey_bg = 8
        unfocused_fg = curses.COLOR_WHITE
    else:
        return Theme(focused_selected_attr=focused, unfocused_selected_attr=unfocused)

    try:
        pair_focused = 1
        pair_unfocused = 2
        curses.init_pair(pair_focused, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(pair_unfocused, unfocused_fg, grey_bg)
        return Theme(
            focused_selected_attr=curses.color_pair(pair_focused) | curses.A_BOLD,
            unfocused_selected_attr=curses.color_pair(pair_unfocused),
        )
    except Exception:
        return Theme(focused_selected_attr=focused, unfocused_selected_attr=unfocused)


def _derive_title_from_history(history_text: str) -> Optional[str]:
    """
    Try to derive a human-friendly title from the history preview text.
    """
    lines = [ln.strip() for ln in history_text.splitlines()]
    # Find the first "User:" block and pick the first meaningful line after it.
    for i, ln in enumerate(lines):
        if ln.lower() in ("user:", "user"):
            for j in range(i + 1, len(lines)):
                cand = lines[j].strip()
                if not cand:
                    continue
                # Skip common wrapper tags.
                if cand.lower() in (
                    "<user_query>",
                    "</user_query>",
                    "<user_info>",
                    "</user_info>",
                ):
                    continue
                # Skip other angle-bracket tags.
                if cand.startswith("<") and cand.endswith(">"):
                    continue
                return cand
    return None


def _hydrate_generic_titles(
    chats: List[AgentChat],
    get_preview: Callable[[AgentChat], Tuple[Optional[str], Optional[str]]],
    *,
    done_ids: "set[str]",
    start_idx: int = 0,
    max_items: int = 1,
    budget_s: float = 0.004,
) -> Tuple[int, int, List[Tuple[str, str]]]:
    """
    Best-effort: derive better titles for chats whose meta name is generic.

    We do this in tiny batches to avoid blocking the TUI.
    Mutates the `chats` list in-place by replacing `AgentChat` entries.
    """
    if max_items <= 0 or budget_s <= 0:
        return 0, start_idx, []

    started = time.monotonic()
    processed = 0
    updates: List[Tuple[str, str]] = []

    i = clamp(start_idx, 0, max(0, len(chats)))
    while i < len(chats):
        if processed >= max_items:
            break
        if (time.monotonic() - started) >= budget_s:
            break
        c = chats[i]
        if c.chat_id in done_ids:
            i += 1
            continue

        # If it's not a candidate, mark done to avoid revisiting it every frame.
        if (not c.latest_root_blob_id) or (not is_generic_chat_name(c.name)):
            done_ids.add(c.chat_id)
            i += 1
            continue

        role, text = get_preview(c)
        new_name = c.name
        if isinstance(role, str) and role == "history" and isinstance(text, str):
            derived = _derive_title_from_history(text)
            if derived:
                new_name = derived

        chats[i] = AgentChat(
            **{
                **c.__dict__,
                "name": new_name,
                "last_role": role,
                "last_text": text,
            }  # type: ignore[arg-type]
        )
        done_ids.add(c.chat_id)
        if new_name and new_name != c.name and not is_generic_chat_name(new_name):
            updates.append((c.chat_id, new_name))
        processed += 1
        i += 1

    # If we've hit the end, keep returning len(chats) as "done" index.
    return processed, i, updates


class _BackgroundLoader:
    """
    Background loader that runs blocking I/O off the UI thread.

    Curses is not thread-safe; this class only executes data reads and returns
    results via a queue. The UI thread applies results to in-memory caches.
    """

    def __init__(
        self,
        *,
        load_chats: Callable[[AgentWorkspace], List[AgentChat]],
        load_preview_snippet: Callable[[AgentChat, int], Tuple[Optional[str], Optional[str]]],
        load_preview_full: Callable[[AgentChat], Tuple[Optional[str], Optional[str]]],
    ) -> None:
        self._load_chats = load_chats
        self._load_preview_snippet = load_preview_snippet
        self._load_preview_full = load_preview_full
        self._q: "queue.Queue[Tuple[str, object]]" = queue.Queue()

        self._chats_inflight: Set[str] = set()  # ws_hash
        self._preview_snippet_inflight: Set[Tuple[str, int]] = set()  # (chat_id, max_messages)
        self._preview_full_inflight: Set[str] = set()  # chat_id
        self._lock = threading.Lock()

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._chats_inflight or self._preview_snippet_inflight or self._preview_full_inflight) or (
                not self._q.empty()
            )

    def ensure_chats(self, ws: AgentWorkspace) -> None:
        key = ws.cwd_hash
        with self._lock:
            if key in self._chats_inflight:
                return
            self._chats_inflight.add(key)

        def _run() -> None:
            try:
                chats = self._load_chats(ws)
                self._q.put(("chats_ok", key, chats))
            except Exception as e:
                self._q.put(("chats_err", key, str(e)))
            finally:
                with self._lock:
                    self._chats_inflight.discard(key)

        threading.Thread(target=_run, daemon=True).start()

    def ensure_preview_snippet(self, chat: AgentChat, *, max_messages: int) -> None:
        key = (chat.chat_id, max_messages)
        with self._lock:
            if key in self._preview_snippet_inflight:
                return
            self._preview_snippet_inflight.add(key)

        def _run() -> None:
            try:
                role, text = self._load_preview_snippet(chat, max_messages)
                self._q.put(("preview_snippet_ok", chat.chat_id, max_messages, role, text))
            except Exception as e:
                self._q.put(("preview_snippet_err", chat.chat_id, max_messages, str(e)))
            finally:
                with self._lock:
                    self._preview_snippet_inflight.discard(key)

        threading.Thread(target=_run, daemon=True).start()

    def ensure_preview_full(self, chat: AgentChat) -> None:
        key = chat.chat_id
        with self._lock:
            if key in self._preview_full_inflight:
                return
            self._preview_full_inflight.add(key)

        def _run() -> None:
            try:
                role, text = self._load_preview_full(chat)
                self._q.put(("preview_full_ok", key, role, text))
            except Exception as e:
                self._q.put(("preview_full_err", key, str(e)))
            finally:
                with self._lock:
                    self._preview_full_inflight.discard(key)

        threading.Thread(target=_run, daemon=True).start()

    def drain(self, *, max_items: int = 50) -> List[Tuple[str, object]]:
        out: List[Tuple[str, object]] = []
        for _ in range(max_items):
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out


@dataclass(frozen=True)
class Rect:
    y: int
    x: int
    h: int
    w: int

    def contains(self, y: int, x: int) -> bool:
        return self.y <= y < self.y + self.h and self.x <= x < self.x + self.w


@dataclass(frozen=True)
class Layout:
    workspaces: Rect
    conversations: Rect
    preview: Rect
    mode: str  # "3col" | "2col" | "1col"


def compute_layout(max_y: int, max_x: int) -> Layout:
    # Reserve last line for status bar.
    usable_h = max(1, max_y - 1)

    if max_x >= 120 and usable_h >= 10:
        left_w = min(40, max(24, max_x // 4))
        mid_w = min(60, max(32, max_x // 3))
        right_w = max(20, max_x - left_w - mid_w)
        return Layout(
            workspaces=Rect(0, 0, usable_h, left_w),
            conversations=Rect(0, left_w, usable_h, mid_w),
            preview=Rect(0, left_w + mid_w, usable_h, right_w),
            mode="3col",
        )

    if max_x >= 80 and usable_h >= 10:
        left_w = min(40, max(24, max_x // 3))
        right_w = max_x - left_w
        conv_h = max(6, int(usable_h * 0.60))
        prev_h = max(3, usable_h - conv_h)
        return Layout(
            workspaces=Rect(0, 0, usable_h, left_w),
            conversations=Rect(0, left_w, conv_h, right_w),
            preview=Rect(conv_h, left_w, prev_h, right_w),
            mode="2col",
        )

    # Small terminal: stack list + preview. The focused list determines what the list pane shows.
    if usable_h <= 1:
        list_h = usable_h
        prev_h = 0
    else:
        list_h = max(1, int(usable_h * 0.60))
        # Ensure preview gets at least 1 row when possible.
        list_h = clamp(list_h, 1, usable_h - 1)
        prev_h = usable_h - list_h
    return Layout(
        workspaces=Rect(0, 0, list_h, max_x),
        conversations=Rect(0, 0, list_h, max_x),
        preview=Rect(list_h, 0, prev_h, max_x),
        mode="1col",
    )


class ListState:
    def __init__(self) -> None:
        self.selected = 0
        self.scroll = 0

    def clamp(self, n_items: int) -> None:
        if n_items <= 0:
            self.selected = 0
            self.scroll = 0
            return
        self.selected = clamp(self.selected, 0, n_items - 1)
        self.scroll = clamp(self.scroll, 0, max(0, n_items - 1))

    def move(self, delta: int, n_items: int) -> None:
        if n_items <= 0:
            self.selected = 0
            self.scroll = 0
            return
        self.selected = clamp(self.selected + delta, 0, n_items - 1)

    def page(self, delta_pages: int, page_size: int, n_items: int) -> None:
        self.move(delta_pages * max(1, page_size), n_items)

    def ensure_visible(self, view_h: int, n_items: int) -> None:
        if n_items <= 0:
            self.scroll = 0
            return
        if view_h <= 0:
            self.scroll = 0
            return
        max_scroll = max(0, n_items - view_h)
        if self.selected < self.scroll:
            self.scroll = self.selected
        elif self.selected >= self.scroll + view_h:
            self.scroll = self.selected - view_h + 1
        self.scroll = clamp(self.scroll, 0, max_scroll)


class PreviewState:
    def __init__(self) -> None:
        self.scroll = 0

    def clamp(self, n_lines: int, view_h: int) -> None:
        if n_lines <= 0 or view_h <= 0:
            self.scroll = 0
            return
        max_scroll = max(0, n_lines - view_h)
        self.scroll = clamp(self.scroll, 0, max_scroll)

    def move(self, delta: int, n_lines: int, view_h: int) -> None:
        self.scroll += delta
        self.clamp(n_lines, view_h)

    def page(self, delta_pages: int, n_lines: int, view_h: int) -> None:
        self.move(delta_pages * max(1, view_h), n_lines, view_h)


class UpdateRequested(Exception):
    """
    Raised from inside the TUI when the user triggers an in-app upgrade.

    `curses.wrapper()` will restore terminal state before propagating the exception.
    """


class ExportPendingExit(Exception):
    """
    Raised when the user quits while an export is still pending.
    """

    def __init__(self, *, out_path: Path, store_db_path: Path, chat_title: str) -> None:
        super().__init__(str(out_path))
        self.out_path = out_path
        self.store_db_path = store_db_path
        self.chat_title = chat_title


def _input_timeout_ms(*, bg_pending: bool, update_checking: bool, ui_pending: bool = False) -> int:
    """
    Decide the curses input timeout.

    If we have background work (chat/preview loaders or version-check) we need
    to keep the UI loop ticking to apply results and refresh the screen.
    """
    return 80 if (bg_pending or update_checking or ui_pending) else -1


def _is_quit_key(ch: int) -> bool:
    """
    Quit keys must be ignored while in search input mode.
    """
    key_exit = getattr(curses, "KEY_EXIT", None)
    if isinstance(key_exit, int) and ch == key_exit:
        return True
    return ch in (ord("q"), ord("Q"))


def _should_quit(*, ch: int, input_mode: Optional[str]) -> bool:
    return (input_mode is None) and _is_quit_key(ch)


def disable_xon_xoff_flow_control() -> Optional[Tuple[int, object]]:
    """
    Disable terminal XON/XOFF (Ctrl+S/Ctrl+Q) flow control.

    Many Linux terminals bind Ctrl+S to XOFF, which stops terminal output and can
    make it look like the app is "frozen". Disabling IXON/IXOFF lets us use
    Ctrl+S as an in-app shortcut.

    Returns (fd, original_termios_attrs) to restore later, or None if not applicable.
    """
    try:
        if not sys.stdin.isatty():
            return None
        fd = sys.stdin.fileno()
    except Exception:
        return None
    try:
        import termios  # POSIX
    except Exception:
        return None
    try:
        orig = termios.tcgetattr(fd)
        attrs = list(orig)
        iflag = int(attrs[0])
        iflag2 = iflag & ~(getattr(termios, "IXON", 0) | getattr(termios, "IXOFF", 0))
        if iflag2 != iflag:
            attrs[0] = iflag2
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        return (fd, orig)
    except Exception:
        return None


def restore_termios(saved: Optional[Tuple[int, object]]) -> None:
    if not saved:
        return
    fd, orig = saved
    try:
        import termios  # POSIX

        termios.tcsetattr(fd, termios.TCSANOW, orig)
    except Exception:
        return


def _prompt_save_path(stdscr: "curses.window", *, default_path: Path) -> Optional[Path]:
    """
    Modal prompt to ask user for output path.

    Returns the chosen Path or None if cancelled.
    """
    try:
        max_y, max_x = stdscr.getmaxyx()
    except Exception:
        return None
    # A tiny terminal can't show a usable prompt.
    if max_y < 7 or max_x < 30:
        return None

    w = min(90, max_x - 4)
    w = max(30, w)
    h = 7
    y = max(0, (max_y - h) // 2)
    x = max(0, (max_x - w) // 2)
    try:
        win = stdscr.derwin(h, w, y, x)
    except Exception:
        return None
    saved_escdelay: Optional[int] = None
    try:
        win.keypad(True)
    except Exception:
        pass
    # Curses uses ESCDELAY to decide whether an ESC is a prefix (Alt / function-key
    # sequences) or a standalone Escape. Default can be ~1000ms, which makes Esc
    # feel laggy in modal dialogs. Lower it for the duration of this prompt.
    try:
        saved_escdelay = curses.get_escdelay()
        curses.set_escdelay(25)
    except Exception:
        saved_escdelay = None
    try:
        # Make the input cursor visible while editing the path.
        curses.curs_set(1)
    except Exception:
        pass
    try:
        win.timeout(-1)  # block for input
    except Exception:
        pass

    text = str(default_path)
    cursor = len(text)
    offset = 0

    label = "Save to:"
    field_x = 2 + len(label) + 1
    field_w = max(1, w - field_x - 2)

    def _maybe_decode_esc_sequence(first: int) -> Optional[int]:
        # If keypad translation doesn't work, arrow keys often arrive as:
        #   ESC [ A/B/C/D  or  ESC O A/B/C/D
        if first != 27:
            return first
        # We want ESC to cancel immediately, but still recognize arrow key sequences.
        # So we do a tiny non-blocking read (with a very small budget) to see if the
        # next bytes are already available.
        n1 = -1
        try:
            win.timeout(0)
            n1 = win.getch()
            if n1 == -1:
                # Give the terminal a very small window to deliver the rest of an
                # arrow-key escape sequence; keeps ESC cancel feeling instant.
                win.timeout(10)
                n1 = win.getch()
        except Exception:
            return 27
        finally:
            try:
                win.timeout(-1)
            except Exception:
                pass
        if n1 == -1:
            return 27  # real ESC (no sequence)
        if n1 not in (ord("["), ord("O")):
            return None
        try:
            # Same principle: avoid noticeable delay on cancellation, but allow a
            # short window for the final byte of the sequence to arrive.
            win.timeout(0)
            n2 = win.getch()
            if n2 == -1:
                win.timeout(10)
                n2 = win.getch()
        except Exception:
            return None
        finally:
            try:
                win.timeout(-1)
            except Exception:
                pass
        if n2 == -1:
            return None
        if n2 == ord("A"):
            return curses.KEY_UP
        if n2 == ord("B"):
            return curses.KEY_DOWN
        if n2 == ord("C"):
            return curses.KEY_RIGHT
        if n2 == ord("D"):
            return curses.KEY_LEFT
        return None

    try:
        while True:
            try:
                win.erase()
            except Exception:
                pass
            try:
                win.box()
            except Exception:
                pass
            _safe_addstr(win, 0, 2, " Save chat history ", curses.A_REVERSE)
            _safe_addstr(win, 2, 2, label, curses.A_BOLD)
            _safe_addstr(win, 5, 2, "Enter: save   Esc: cancel", curses.A_DIM)

            # Horizontal scrolling for long paths.
            cursor = clamp(cursor, 0, len(text))
            if cursor < offset:
                offset = cursor
            if cursor > offset + field_w:
                offset = cursor - field_w
            offset = clamp(offset, 0, max(0, len(text) - field_w))

            vis = text[offset : offset + field_w]
            _safe_addstr(win, 2, field_x, vis.ljust(field_w), 0)
            try:
                win.move(2, field_x + (cursor - offset))
            except Exception:
                pass

            try:
                win.noutrefresh()
                curses.doupdate()
            except Exception:
                pass

            try:
                ch = win.getch()
            except Exception:
                return None

            if ch == 27:
                mapped = _maybe_decode_esc_sequence(ch)
                if mapped is None:
                    continue
                if mapped == 27:
                    return None
                ch = mapped

            if ch in (curses.KEY_ENTER, 10, 13):
                s = (text or "").strip()
                if not s:
                    return None
                return Path(s).expanduser()
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if cursor > 0:
                    text = text[: cursor - 1] + text[cursor:]
                    cursor -= 1
                continue
            if ch == 9:  # Tab
                text, cursor = tab_complete_path(text, cursor)
                continue
            if ch == curses.KEY_LEFT:
                cursor = max(0, cursor - 1)
                continue
            if ch == curses.KEY_RIGHT:
                cursor = min(len(text), cursor + 1)
                continue
            if ch in (curses.KEY_UP, curses.KEY_DOWN):
                # No-op (single-field dialog).
                continue
            if ch == curses.KEY_HOME:
                cursor = 0
                continue
            if ch == curses.KEY_END:
                cursor = len(text)
                continue
            if 32 <= ch <= 126:
                text = text[:cursor] + chr(ch) + text[cursor:]
                cursor += 1
                continue
    finally:
        try:
            curses.curs_set(0)
        except Exception:
            pass
        if saved_escdelay is not None:
            try:
                curses.set_escdelay(int(saved_escdelay))
            except Exception:
                pass


def _safe_addstr(win: "curses.window", y: int, x: int, s: str, attr: int = 0) -> None:
    try:
        win.addstr(y, x, s, attr)
    except curses.error:
        # Ignore drawing errors at borders / tiny terminals.
        return


class _Pane:
    """
    A single pane with an outer (border) window and an optional inner content window.

    We keep windows persistent and update only changed rows to reduce flicker on
    high-latency / web-based terminals (e.g., remote SSH inside VSCode/Cursor).
    """

    def __init__(
        self,
        stdscr: "curses.window",
        rect: Rect,
    ) -> None:
        self.rect = rect
        self.outer: "curses.window" = stdscr.derwin(rect.h, rect.w, rect.y, rect.x)
        self.outer.leaveok(True)

        self.inner: Optional["curses.window"] = None
        if rect.h >= 3 and rect.w >= 4:
            self.inner = self.outer.derwin(rect.h - 2, rect.w - 2, 1, 1)
            self.inner.leaveok(True)
            # Enable terminal-side scroll optimizations when available.
            # This is especially helpful for preview scrolling.
            try:
                self.inner.idlok(True)  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                self.inner.idcok(True)  # type: ignore[attr-defined]
            except Exception:
                pass

        # Border is expensive to redraw on some terminals; draw it only when forced.
        self._border_drawn = False
        self._title_key: Optional[Tuple[str, bool]] = None
        self._hint_key: Optional[str] = None
        self._last_title_span: Optional[Tuple[int, int]] = None  # (x, w) in columns
        self._last_hint_span: Optional[Tuple[int, int]] = None  # (x, w) in columns

        self._inner_cache: List[Tuple[str, int]] = []
        # Preview-only incremental scroll state.
        self._preview_last_start: Optional[int] = None

    def _title_x(self, title: str) -> int:
        # Center by display width (handles CJK correctly).
        tw = display_width(title)
        if self.rect.w <= 2:
            return 0
        return max(1, (self.rect.w - tw) // 2)

    def draw_frame(self, title: str, *, focused: bool, filter_text: str, force: bool = False) -> None:
        if self.rect.h <= 0 or self.rect.w <= 0:
            return
        changed = False
        try:
            # Border: only draw on forced redraw (resize/layout rebuild).
            if force or not self._border_drawn:
                self.outer.box()
                self._border_drawn = True
                changed = True
                # Border redraw restores the hline behind title/hint.
                self._last_title_span = None
                self._last_hint_span = None

            # Title: update only the title span on focus/title changes.
            title_key = (title, focused)
            if force or self._title_key != title_key:
                self._title_key = title_key

                if focused:
                    t = f" > {title} < "
                    title_attr = curses.A_REVERSE | curses.A_BOLD
                else:
                    t = f" {title} "
                    title_attr = curses.A_REVERSE

                t = truncate_to_width(t, max(0, self.rect.w - 2))
                x = self._title_x(t)
                w = display_width(t)

                # Restore previous title area with the border hline.
                if self._last_title_span is not None and self.rect.w >= 3:
                    px, pw = self._last_title_span
                    px = clamp(px, 1, max(1, self.rect.w - 2))
                    pw = clamp(pw, 0, max(0, (self.rect.w - 1) - px))
                    if pw:
                        try:
                            self.outer.hline(0, px, curses.ACS_HLINE, pw)
                        except Exception:
                            # Fallback: overwrite with '-' if ACS isn't available.
                            self.outer.hline(0, px, ord("-"), pw)

                # Draw the title and remember its span.
                _safe_addstr(self.outer, 0, x, t, title_attr)
                self._last_title_span = (x, w)
                changed = True

            # Filter hint: update only on filter changes.
            if force or self._hint_key != filter_text:
                self._hint_key = filter_text
                y = self.rect.h - 1
                if y >= 0 and self.rect.w >= 4:
                    # Restore previous hint area with the border hline.
                    if self._last_hint_span is not None:
                        px, pw = self._last_hint_span
                        px = clamp(px, 1, max(1, self.rect.w - 2))
                        pw = clamp(pw, 0, max(0, (self.rect.w - 1) - px))
                        if pw:
                            try:
                                self.outer.hline(y, px, curses.ACS_HLINE, pw)
                            except Exception:
                                self.outer.hline(y, px, ord("-"), pw)
                    self._last_hint_span = None

                    if filter_text:
                        hint = truncate_to_width(f"/{filter_text}", max(0, self.rect.w - 4))
                        _safe_addstr(self.outer, y, 2, hint, curses.A_DIM)
                        self._last_hint_span = (2, display_width(hint))
                changed = True

            if changed:
                self.outer.noutrefresh()
        except curses.error:
            return

    def draw_inner_rows(self, rows: List[Tuple[str, int]], *, force: bool = False) -> None:
        if not self.inner:
            return
        try:
            inner_h, inner_w = self.inner.getmaxyx()
        except Exception:
            return
        if inner_h <= 0 or inner_w <= 0:
            return

        # Ensure cache matches current window size.
        if len(self._inner_cache) != inner_h:
            self._inner_cache = [("", -1) for _ in range(inner_h)]
            force = True
        changed = force

        for i in range(min(inner_h, len(rows))):
            s, attr = rows[i]
            # Ensure each row is exactly inner width by display width.
            s = pad_to_width(s, inner_w)
            if not force and self._inner_cache[i] == (s, attr):
                continue
            _safe_addstr(self.inner, i, 0, s, attr)
            self._inner_cache[i] = (s, attr)
            changed = True

        # If the caller provided fewer rows than the visible height, blank the rest.
        blank = (" " * inner_w, 0)
        for i in range(len(rows), inner_h):
            if not force and self._inner_cache[i] == blank:
                continue
            _safe_addstr(self.inner, i, 0, blank[0], blank[1])
            self._inner_cache[i] = blank
            changed = True

        if changed:
            try:
                self.inner.noutrefresh()
            except curses.error:
                return

    def draw_preview_lines(
        self,
        lines: List[str],
        start: int,
        *,
        use_terminal_scroll: bool = False,
        bottom_overlay: Optional[Tuple[str, int]] = None,
        force: bool = False,
    ) -> None:
        """
        Draw preview content with incremental scrolling.

        When `start` changes by a small delta, we scroll the inner window and only
        redraw the newly exposed lines, reducing terminal output and flicker.
        """
        if not self.inner:
            return
        try:
            inner_h, inner_w = self.inner.getmaxyx()
        except Exception:
            return
        if inner_h <= 0 or inner_w <= 0:
            return

        # Terminal-side scrolling affects the whole window region. If we're using a
        # fixed bottom overlay (e.g. "Loading…"), disable terminal scrolling to
        # keep the overlay row stable and avoid scrolling artifacts.
        if bottom_overlay is not None:
            use_terminal_scroll = False

        # Ensure cache matches current window size.
        if len(self._inner_cache) != inner_h:
            self._inner_cache = [("", -1) for _ in range(inner_h)]
            force = True
            self._preview_last_start = None

        blank = (" " * inner_w, 0)
        content_h = inner_h - 1 if (bottom_overlay is not None and inner_h >= 1) else inner_h
        content_h = max(0, content_h)

        # Clamp start to available content.
        max_start = max(0, len(lines) - content_h)
        start = clamp(start, 0, max_start)

        last = self._preview_last_start
        diff = 0 if last is None else (start - last)

        def _apply_overlay(changed: bool) -> bool:
            if bottom_overlay is None or inner_h <= 0:
                return changed
            y = inner_h - 1
            text, attr = bottom_overlay
            s = pad_to_width(truncate_to_width(text, inner_w), inner_w)
            if force or self._inner_cache[y] != (s, attr):
                _safe_addstr(self.inner, y, 0, s, attr)
                self._inner_cache[y] = (s, attr)
                return True
            return changed

        # For multi-pane layouts, terminal-side line insert/delete scrolling often
        # affects the whole terminal row, which can cause visible "flicker" in
        # adjacent panes on some terminals (e.g. xterm.js). In those cases, prefer
        # a straightforward redraw confined to this window.
        if not use_terminal_scroll:
            changed = force
            for row in range(content_h):
                idx = start + row
                s = lines[idx] if idx < len(lines) else ""
                s = pad_to_width(truncate_to_width(s, inner_w), inner_w)
                if force or self._inner_cache[row] != (s, 0):
                    _safe_addstr(self.inner, row, 0, s, 0)
                    self._inner_cache[row] = (s, 0)
                    changed = True
            changed = _apply_overlay(changed)
            self._preview_last_start = start
            if changed:
                try:
                    self.inner.noutrefresh()
                except curses.error:
                    return
            return

        # If it's a big jump (page/home/end) or we don't have a baseline, redraw all.
        if force or last is None or abs(diff) >= content_h:
            changed = force
            for row in range(content_h):
                idx = start + row
                s = lines[idx] if idx < len(lines) else ""
                s = pad_to_width(truncate_to_width(s, inner_w), inner_w)
                if force or self._inner_cache[row] != (s, 0):
                    _safe_addstr(self.inner, row, 0, s, 0)
                    self._inner_cache[row] = (s, 0)
                    changed = True
            changed = _apply_overlay(changed)
            self._preview_last_start = start
            if changed:
                try:
                    self.inner.noutrefresh()
                except curses.error:
                    return
            return

        # Small incremental scroll: scroll the window and patch the new lines.
        if diff != 0:
            # Best-effort: ensure scrolling is allowed.
            try:
                self.inner.scrollok(True)  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                # Some curses implementations gate the use of insert/delete-line
                # optimizations on idlok/idcok.
                try:
                    self.inner.idlok(True)  # type: ignore[attr-defined]
                except Exception:
                    pass
                try:
                    self.inner.idcok(True)  # type: ignore[attr-defined]
                except Exception:
                    pass
                self.inner.scroll(diff)  # type: ignore[attr-defined]
            except Exception:
                # If scrolling isn't supported, fall back to full redraw.
                for row in range(inner_h):
                    idx = start + row
                    s = lines[idx] if idx < len(lines) else ""
                    s = pad_to_width(truncate_to_width(s, inner_w), inner_w)
                    if self._inner_cache[row] != (s, 0):
                        _safe_addstr(self.inner, row, 0, s, 0)
                        self._inner_cache[row] = (s, 0)
                self._preview_last_start = start
                try:
                    self.inner.noutrefresh()
                except curses.error:
                    return
                return

            if diff > 0:
                # View moved down: window content scrolled up; fill bottom lines.
                d = diff
                self._inner_cache = self._inner_cache[d:] + [blank for _ in range(d)]
                for j in range(d):
                    row = content_h - d + j
                    idx = start + row
                    s = lines[idx] if idx < len(lines) else ""
                    s = pad_to_width(truncate_to_width(s, inner_w), inner_w)
                    _safe_addstr(self.inner, row, 0, s, 0)
                    self._inner_cache[row] = (s, 0)
            else:
                # View moved up: window content scrolled down; fill top lines.
                d = -diff
                self._inner_cache = [blank for _ in range(d)] + self._inner_cache[: inner_h - d]
                for j in range(d):
                    row = j
                    idx = start + row
                    s = lines[idx] if idx < len(lines) else ""
                    s = pad_to_width(truncate_to_width(s, inner_w), inner_w)
                    _safe_addstr(self.inner, row, 0, s, 0)
                    self._inner_cache[row] = (s, 0)

            _apply_overlay(True)
            self._preview_last_start = start

            try:
                self.inner.noutrefresh()
            except curses.error:
                return
            return

        # No scroll delta: just patch any changed visible lines.
        changed = force
        for row in range(content_h):
            idx = start + row
            s = lines[idx] if idx < len(lines) else ""
            s = pad_to_width(truncate_to_width(s, inner_w), inner_w)
            if self._inner_cache[row] != (s, 0):
                _safe_addstr(self.inner, row, 0, s, 0)
                self._inner_cache[row] = (s, 0)
                changed = True
        changed = _apply_overlay(changed)
        self._preview_last_start = start
        if changed:
            try:
                self.inner.noutrefresh()
            except curses.error:
                return


def _filter_items(items: List[Tuple[str, object]], needle: str) -> List[Tuple[str, object]]:
    if not needle:
        return items
    n = needle.lower()
    out: List[Tuple[str, object]] = []
    for label, obj in items:
        if getattr(obj, "always_visible", False):
            out.append((label, obj))
            continue
        if n in label.lower():
            out.append((label, obj))
    return out


def _list_rows(
    rect: Rect,
    items: List[Tuple[str, object]],
    state: ListState,
    *,
    focused: bool,
    filter_text: str,
    theme: Theme,
    dim_all: bool = False,
) -> List[Tuple[str, int]]:
    """
    Build the visible list rows for a list pane. Each row is (text, attr).
    """
    if rect.h < 3 or rect.w < 4:
        return []
    inner_h = rect.h - 2
    inner_w = rect.w - 2

    filtered = _filter_items(items, filter_text)
    state.clamp(len(filtered))
    state.ensure_visible(inner_h, len(filtered))

    start = state.scroll
    end = min(len(filtered), start + inner_h)

    out: List[Tuple[str, int]] = []
    for row in range(inner_h):
        idx = start + row
        if idx < end:
            label, _ = filtered[idx]
            line = pad_to_width(truncate_to_width(label, inner_w), inner_w)
            if idx == state.selected:
                attr = theme.focused_selected_attr if focused else theme.unfocused_selected_attr
                if dim_all and (not focused):
                    attr |= curses.A_DIM
            else:
                attr = curses.A_DIM if (dim_all and (not focused)) else 0
            out.append((line, attr))
        else:
            out.append((pad_to_width("", inner_w), curses.A_DIM if (dim_all and (not focused)) else 0))
    return out


def _preview_content_lines(
    inner_w: int,
    workspace: Optional[AgentWorkspace],
    chat: Optional[AgentChat],
    message: Optional[str],
) -> List[str]:
    lines: List[str] = []
    if message:
        lines.extend(wrap_text(message, inner_w))
        return lines
    if chat is None:
        lines.append("Select a chat session to see details.")
        return lines

    title = chat.name or "Untitled"
    lines.append(f"Title: {title}")
    if chat.mode:
        lines.append(f"Mode: {chat.mode}")
    lines.append(f"Created: {format_epoch_ms(chat.created_at_ms)}")
    if workspace and workspace.workspace_path:
        lines.append(f"Workspace: {workspace.workspace_path}")
    lines.append(f"Chat ID: {chat.chat_id}")

    if chat.last_text:
        lines.append("")
        role = chat.last_role or "message"
        if role == "history":
            lines.append("History:")
        else:
            lines.append(f"Last {role}:")
        lines.extend(wrap_text(chat.last_text, inner_w))
    return lines


def _preview_rows(
    rect: Rect,
    workspace: Optional[AgentWorkspace],
    chat: Optional[AgentChat],
    message: Optional[str],
    *,
    scroll: int = 0,
) -> List[Tuple[str, int]]:
    if rect.h < 3 or rect.w < 4:
        return []
    inner_h = rect.h - 2
    inner_w = rect.w - 2

    lines = _preview_content_lines(inner_w, workspace, chat, message)
    max_scroll = max(0, len(lines) - inner_h)
    start = clamp(scroll, 0, max_scroll)

    out: List[Tuple[str, int]] = []
    for i in range(inner_h):
        src_i = start + i
        ln = lines[src_i] if src_i < len(lines) else ""
        out.append((pad_to_width(truncate_to_width(ln, inner_w), inner_w), 0))
    return out


class _StatusBar:
    def __init__(
        self,
        stdscr: "curses.window",
        max_y: int,
        max_x: int,
    ) -> None:
        self.win: "curses.window" = stdscr.derwin(1, max_x, max_y - 1, 0)
        self.win.leaveok(True)
        self._cache: Optional[Tuple[str, str, int]] = None  # (left_bar, right_text, right_attr)
        self._w = max_x

    def draw(self, text: str, *, right: str = "", right_attr: int = 0, force: bool = False) -> None:
        """
        Draw a full-width status bar, with an optional right-aligned segment.

        The entire bar is reverse video; the right segment can additionally set
        attributes (e.g. bold).
        """
        left_bar = pad_to_width(truncate_to_width(text, self._w), self._w)
        right_s = truncate_to_width(right or "", self._w)
        cache_key = (left_bar, right_s, int(right_attr or 0))
        if not force and self._cache == cache_key:
            return
        self._cache = cache_key

        # Base bar (always full width, so it clears any previous right segment).
        _safe_addstr(self.win, 0, 0, left_bar, curses.A_REVERSE)

        # Right segment overlay (right-aligned).
        if right_s:
            rw = display_width(right_s)
            x = max(0, self._w - rw)
            try:
                self.win.addstr(0, x, right_s, curses.A_REVERSE | right_attr)
            except curses.error:
                # Fallback: if wide/unicode chars can't be drawn (locale/terminal),
                # at least show an ASCII-only version.
                safe = "".join(ch for ch in right_s if ord(ch) < 128)
                if safe:
                    rw2 = display_width(safe)
                    x2 = max(0, self._w - rw2)
                    _safe_addstr(self.win, 0, x2, safe, curses.A_REVERSE | right_attr)
        try:
            self.win.noutrefresh()
        except curses.error:
            return


class _Renderer:
    def __init__(self, stdscr: "curses.window") -> None:
        self.stdscr = stdscr
        self._layout: Optional[Layout] = None
        self._max_yx: Optional[Tuple[int, int]] = None

        # Panes (1col uses list_pane; 2/3col uses ws/chats panes).
        self.list_pane: Optional[_Pane] = None
        self.ws_pane: Optional[_Pane] = None
        self.chats_pane: Optional[_Pane] = None
        self.preview_pane: Optional[_Pane] = None
        self.status: Optional[_StatusBar] = None

    def ensure(self, layout: Layout, max_y: int, max_x: int) -> bool:
        """
        Ensure windows exist for the given layout. Returns True if rebuilt.
        """
        if self._layout == layout and self._max_yx == (max_y, max_x):
            return False

        # On layout changes (including resize), do a one-time full clear.
        # This may produce a visible refresh only on resize, which is acceptable.
        try:
            self.stdscr.erase()
            self.stdscr.noutrefresh()
        except curses.error:
            pass

        self._layout = layout
        self._max_yx = (max_y, max_x)

        self.list_pane = None
        self.ws_pane = None
        self.chats_pane = None

        if layout.mode == "1col":
            self.list_pane = _Pane(self.stdscr, layout.workspaces)
        else:
            self.ws_pane = _Pane(self.stdscr, layout.workspaces)
            self.chats_pane = _Pane(self.stdscr, layout.conversations)

        self.preview_pane = _Pane(self.stdscr, layout.preview)
        self.status = _StatusBar(self.stdscr, max_y, max_x)
        return True


def select_chat(
    stdscr: "curses.window",
    *,
    workspaces: List[AgentWorkspace],
    load_chats: Callable[[AgentWorkspace], List[AgentChat]],
    load_preview_snippet: Callable[[AgentChat, int], Tuple[Optional[str], Optional[str]]],
    load_preview_full: Callable[[AgentChat], Tuple[Optional[str], Optional[str]]],
    sync_output: bool = False,
) -> Optional[Tuple[AgentWorkspace, Optional[AgentChat]]]:
    try:
        curses.curs_set(0)
    except Exception:
        # Some terminals (or TERM/terminfo combinations) don't support this.
        pass
    stdscr.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)

    theme = _init_theme()

    ws_state = ListState()
    chat_state = ListState()
    focus = "workspaces"  # "workspaces" | "chats" | "preview"
    last_list_focus = "workspaces"  # "workspaces" | "chats"
    preview_state = PreviewState()
    last_preview_key: object = object()
    last_preview_lines_key: object = object()
    preview_lines_cached: List[str] = [""]
    ws_filter = ""
    chat_filter = ""
    input_mode: Optional[str] = None  # "ws" | "chat"

    bg = _BackgroundLoader(load_chats=load_chats, load_preview_snippet=load_preview_snippet, load_preview_full=load_preview_full)

    # Ephemeral user feedback in the status bar (expires automatically).
    toast: Optional[Tuple[str, float]] = None  # (text, expires_at_monotonic)

    # Pending export request (when user hits Ctrl+S before full history is ready).
    export_pending: Optional[Tuple[str, Path]] = None  # (chat_id, output_path)

    # Modal dialogs can draw over the panes without updating per-pane caches.
    # Use this flag to force a full redraw on the next frame after a modal closes.
    force_full_next = False

    # Best-effort auto-update check (non-blocking).
    update_q: "queue.Queue[UpdateStatus]" = queue.Queue()
    update_status: Optional[UpdateStatus] = None
    update_checked_at = 0.0
    update_checking = False

    def _start_update_check(*, force: bool = False) -> None:
        nonlocal update_checking, update_checked_at
        now = time.monotonic()
        # Check at most every 5 minutes (or on explicit force).
        if (not force) and update_checked_at and (now - update_checked_at) < 300:
            return
        if update_checking:
            return
        update_checking = True
        update_checked_at = now

        def _run() -> None:
            nonlocal update_checking
            try:
                st = check_for_update(timeout_s=8.0)
                update_q.put(st)
            except Exception:
                # Mark as "can't check" so the UI doesn't look stuck.
                update_q.put(UpdateStatus(supported=False, error="update check failed"))
            finally:
                update_checking = False

        threading.Thread(target=_run, daemon=True).start()

    _start_update_check(force=True)

    chat_cache: Dict[str, List[AgentChat]] = {}
    chat_error: Dict[str, str] = {}
    chat_loading: Set[str] = set()
    preview_snippet_cache: Dict[Tuple[str, int], Tuple[Optional[str], Optional[str]]] = {}
    preview_snippet_error: Dict[Tuple[str, int], str] = {}
    preview_snippet_loading: Set[Tuple[str, int]] = set()
    preview_full_cache: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    preview_full_error: Dict[str, str] = {}
    preview_full_loading: Set[str] = set()
    title_cache: Optional[ChatTitleCache] = None
    title_cache_dir: Optional["Path"] = None
    # Used only for selected-chat title persistence.
    title_cache_dirty = False
    last_cache_save_at = 0.0

    try:
        if workspaces:
            # workspaces[*].chats_root == <config_dir>/chats/<hash>
            title_cache_dir = workspaces[0].chats_root.parent.parent
            title_cache = load_chat_title_cache(title_cache_dir)
    except Exception:
        title_cache = None
        title_cache_dir = None

    last_click_at: float = 0.0
    last_click_target: Optional[Tuple[str, int]] = None  # (pane, index)

    def current_workspace() -> Optional[AgentWorkspace]:
        if not workspaces:
            return None
        idx = clamp(ws_state.selected, 0, len(workspaces) - 1)
        return workspaces[idx]

    def get_chats(ws: AgentWorkspace) -> List[AgentChat]:
        key = ws.cwd_hash
        if key in chat_cache:
            return chat_cache[key]
        if key not in chat_loading:
            chat_loading.add(key)
            bg.ensure_chats(ws)
        return []

    def get_preview_snippet(chat: AgentChat, *, max_messages: int) -> Tuple[Optional[str], Optional[str]]:
        key = (chat.chat_id, max_messages)
        if key in preview_snippet_cache:
            return preview_snippet_cache[key]
        if key not in preview_snippet_loading:
            preview_snippet_loading.add(key)
            bg.ensure_preview_snippet(chat, max_messages=max_messages)
        return None, None

    def get_preview_full(chat: AgentChat) -> Tuple[Optional[str], Optional[str]]:
        key = chat.chat_id
        if key in preview_full_cache:
            return preview_full_cache[key]
        if key not in preview_full_loading:
            preview_full_loading.add(key)
            bg.ensure_preview_full(chat)
        return None, None

    renderer = _Renderer(stdscr)

    while True:
        now = time.monotonic()
        spin = _spinner(now)
        if toast is not None and now >= toast[1]:
            toast = None

        # Apply update status results.
        try:
            while True:
                update_status = update_q.get_nowait()
        except queue.Empty:
            pass
        _start_update_check(force=False)

        # Apply background results.
        for item in bg.drain():
            kind = item[0]
            if kind == "chats_ok":
                _, ws_hash, chats = item
                if isinstance(ws_hash, str) and isinstance(chats, list):
                    chat_cache[ws_hash] = chats  # type: ignore[assignment]
                    chat_error.pop(ws_hash, None)
                    chat_loading.discard(ws_hash)
            elif kind == "chats_err":
                _, ws_hash, err = item
                if isinstance(ws_hash, str):
                    chat_cache[ws_hash] = []
                    chat_error[ws_hash] = f"Failed to load chats: {err}"
                    chat_loading.discard(ws_hash)
            elif kind == "preview_snippet_ok":
                _, chat_id, max_messages, role, text = item
                if isinstance(chat_id, str) and isinstance(max_messages, int):
                    k = (chat_id, max_messages)
                    preview_snippet_cache[k] = (
                        role if isinstance(role, str) else None,
                        text if isinstance(text, str) else None,
                    )
                    preview_snippet_error.pop(k, None)
                    preview_snippet_loading.discard(k)
            elif kind == "preview_snippet_err":
                _, chat_id, max_messages, err = item
                if isinstance(chat_id, str) and isinstance(max_messages, int):
                    k = (chat_id, max_messages)
                    preview_snippet_cache[k] = (None, None)
                    preview_snippet_error[k] = f"Failed to load preview: {err}"
                    preview_snippet_loading.discard(k)
            elif kind == "preview_full_ok":
                _, chat_id, role, text = item
                if isinstance(chat_id, str):
                    preview_full_cache[chat_id] = (
                        role if isinstance(role, str) else None,
                        text if isinstance(text, str) else None,
                    )
                    preview_full_error.pop(chat_id, None)
                    preview_full_loading.discard(chat_id)
                    if export_pending is not None and export_pending[0] == chat_id:
                        out_path = export_pending[1]
                        export_pending = None
                        if isinstance(text, str) and text.strip():
                            try:
                                write_text_file(out_path, text)
                                toast = (f"Saved: {out_path}", time.monotonic() + 4.0)
                            except Exception as e:
                                toast = (f"Save failed: {e}", time.monotonic() + 3.0)
                        else:
                            toast = ("Save failed: no history", time.monotonic() + 3.0)
            elif kind == "preview_full_err":
                _, chat_id, err = item
                if isinstance(chat_id, str):
                    preview_full_cache[chat_id] = (None, None)
                    preview_full_error[chat_id] = f"Failed to load preview: {err}"
                    preview_full_loading.discard(chat_id)
                    if export_pending is not None and export_pending[0] == chat_id:
                        export_pending = None
                        toast = (f"Save failed: {err}", time.monotonic() + 3.0)

        max_y, max_x = stdscr.getmaxyx()
        layout = compute_layout(max_y, max_x)
        force_full = renderer.ensure(layout, max_y, max_x) or force_full_next
        force_full_next = False
        preview_inner_h = max(0, layout.preview.h - 2)
        preview_inner_w = max(0, layout.preview.w - 2)

        # Best-effort: detect current working directory workspace.
        try:
            cwd = Path.cwd()
        except Exception:
            cwd = None  # type: ignore[assignment]
        try:
            cwd_resolved = cwd.resolve() if isinstance(cwd, Path) else None
        except Exception:
            cwd_resolved = None

        ws_items: List[Tuple[str, object]] = []
        for ws in workspaces:
            extra = "" if ws.workspace_path is not None else "  (unknown path)"
            is_current = False
            if isinstance(cwd, Path) and ws.workspace_path is not None:
                try:
                    is_current = ws.workspace_path == cwd or (cwd_resolved is not None and ws.workspace_path == cwd_resolved)
                except Exception:
                    is_current = False
            prefix = "[current] " if is_current else ""
            ws_items.append((f"{prefix}{ws.display_name}{extra}", ws))

        ws = current_workspace()
        chats: List[AgentChat] = get_chats(ws) if ws else []

        chat_items: List[Tuple[str, object]] = []
        chat_items.append(("(New Agent)", NEW_AGENT_ITEM))
        if ws and (ws.cwd_hash in chat_loading) and not chats:
            chat_items.append((f"({spin} Loading chats…)", LOADING_ITEM))
        if ws and (ws.cwd_hash in chat_error) and not chats:
            chat_items.append((f"(Error: {chat_error[ws.cwd_hash]})", ErrorItem(chat_error[ws.cwd_hash])))
        for c in chats:
            ts = format_epoch_ms(c.created_at_ms)
            label = f"{c.name}  ({ts})"
            chat_items.append((label, c))

        selected_chat: Optional[AgentChat] = None
        selected_is_new_agent = False
        selected_is_loading = False
        selected_error: Optional[str] = None
        if chat_items:
            filtered = _filter_items(chat_items, chat_filter)
            if filtered:
                chat_state.clamp(len(filtered))
                obj = filtered[chat_state.selected][1]
                if isinstance(obj, AgentChat):
                    selected_chat = obj
                elif isinstance(obj, NewAgentItem):
                    selected_is_new_agent = True
                elif isinstance(obj, LoadingItem):
                    selected_is_loading = True
                elif isinstance(obj, ErrorItem):
                    selected_error = obj.message
                else:
                    selected_is_new_agent = False

        msg = None
        if ws:
            msg = chat_error.get(ws.cwd_hash)

        if ws and selected_is_loading and not msg:
            msg = f"{spin} Loading chat sessions…"

        if ws and selected_error and not msg:
            msg = selected_error

        if ws and selected_is_new_agent and not msg:
            if ws.workspace_path:
                msg = f"Start a new Cursor Agent chat in:\n{ws.workspace_path}"
            else:
                msg = "Workspace path is unknown. Run ccm from that folder to learn it."

        preview_loading_more = False
        if ws and selected_chat and not msg:
            role, text = (None, None)
            new_name = selected_chat.name

            # When not in the preview pane, only load a small "initial" snippet
            # (enough messages to roughly fill the preview height). When the user
            # focuses the preview pane, load the full history.
            snippet_max_messages = max(8, preview_inner_h)

            if selected_chat.latest_root_blob_id:
                if focus == "preview":
                    role, text = get_preview_full(selected_chat)
                    if selected_chat.chat_id in preview_full_error and (role is None and text is None):
                        msg = preview_full_error[selected_chat.chat_id]
                    if role is None and text is None and not msg:
                        # Fall back to snippet while full is loading.
                        s_role, s_text = get_preview_snippet(selected_chat, max_messages=snippet_max_messages)
                        if s_role is not None or s_text is not None:
                            role, text = s_role, s_text
                            preview_loading_more = True
                        else:
                            # No snippet yet: show a loading message in the preview body.
                            if (
                                (selected_chat.chat_id in preview_full_loading)
                                or ((selected_chat.chat_id, snippet_max_messages) in preview_snippet_loading)
                            ):
                                msg = f"{spin} Loading preview…"
                            k = (selected_chat.chat_id, snippet_max_messages)
                            if k in preview_snippet_error and not msg:
                                msg = preview_snippet_error[k]
                else:
                    role, text = get_preview_snippet(selected_chat, max_messages=snippet_max_messages)
                    k = (selected_chat.chat_id, snippet_max_messages)
                    if k in preview_snippet_loading and (role is None and text is None):
                        msg = f"{spin} Loading preview…"
                    if k in preview_snippet_error and not msg:
                        msg = preview_snippet_error[k]

            if isinstance(role, str) and role == "history" and isinstance(text, str) and is_generic_chat_name(new_name):
                derived_title = _derive_title_from_history(text)
                if derived_title:
                    new_name = derived_title

            selected_chat = AgentChat(
                **{
                    **selected_chat.__dict__,
                    "name": new_name,
                    "last_role": role,
                    "last_text": text,
                }  # type: ignore[arg-type]
            )
            # Persist preview/title into the cached list (in-place) so list labels update.
            if ws and ws.cwd_hash in chat_cache:
                try:
                    lst = chat_cache[ws.cwd_hash]
                    for i, c in enumerate(lst):
                        if c.chat_id == selected_chat.chat_id:
                            lst[i] = selected_chat
                            break
                except Exception:
                    pass

            # If we derived a better title, persist it.
            if (
                ws
                and title_cache is not None
                and title_cache_dir is not None
                and new_name
                and not is_generic_chat_name(new_name)
            ):
                try:
                    set_cached_title(title_cache, cwd_hash=ws.cwd_hash, chat_id=selected_chat.chat_id, title=new_name)
                    save_chat_title_cache(title_cache_dir, title_cache)
                    title_cache_dirty = False
                    last_cache_save_at = time.monotonic()
                except Exception:
                    pass

        status = "Tab/Left/Right: switch  /: search  Enter: open  q: quit"
        if input_mode:
            status = "Type to search. Enter: apply  Esc: cancel"
        elif focus == "preview":
            status = "↑/↓ PgUp/PgDn: scroll preview  Tab/Left/Right: switch  q: quit"
        # Hint: save is available when a real chat with history is selected and we know the workspace path.
        if (
            toast is None
            and input_mode is None
            and selected_chat is not None
            and isinstance(selected_chat.latest_root_blob_id, str)
            and bool(selected_chat.latest_root_blob_id)
        ):
            status = status + "  Ctrl+S: save"
        if toast is not None:
            status = toast[0]
        # Right-bottom update info:
        # - Up-to-date: show version + "latest"
        # - Can't check: show version only
        # - Update available: bold keybinding hint
        update_right = f"v{__version__}"
        update_right_attr = 0
        if update_status is None:
            if update_checking:
                update_right = f"v{__version__} checking"
        elif update_status.supported:
            if update_status.update_available:
                update_right = f"v{__version__}  Ctrl+U upgrade"
                update_right_attr = curses.A_BOLD
            else:
                update_right = f"v{__version__} latest"

        # Preview scroll state: reset on content changes, clamp every frame.
        # Cache wrapped preview lines so scrolling doesn't re-wrap on every keypress.
        preview_key = ("msg" if msg else "chat", (ws.cwd_hash if ws else None), (selected_chat.chat_id if selected_chat else None))
        preview_lines_key = (
            preview_inner_w,
            msg,
            (ws.workspace_path.as_posix() if (ws and ws.workspace_path) else None),
            (selected_chat.chat_id if selected_chat else None),
            (selected_chat.name if selected_chat else None),
            (selected_chat.mode if selected_chat else None),
            (selected_chat.created_at_ms if selected_chat else None),
            (selected_chat.last_role if selected_chat else None),
            (selected_chat.last_text if selected_chat else None),
        )
        if preview_lines_key != last_preview_lines_key:
            preview_lines_cached = _preview_content_lines(preview_inner_w, ws, selected_chat, msg) if preview_inner_w > 0 else [""]
            last_preview_lines_key = preview_lines_key
        preview_lines = preview_lines_cached
        if preview_key != last_preview_key:
            preview_state.scroll = 0
            last_preview_key = preview_key
        preview_overlay_active = bool(focus == "preview" and preview_loading_more)
        preview_view_h = max(0, preview_inner_h - (1 if preview_overlay_active else 0))
        preview_state.clamp(len(preview_lines), preview_view_h)

        # Draw panes with minimal updates.
        if layout.mode == "1col":
            if renderer.list_pane is None:
                continue
            list_mode = last_list_focus if focus == "preview" else focus
            list_title = "Workspaces" if list_mode == "workspaces" else "Chat Sessions"
            list_items = ws_items if list_mode == "workspaces" else chat_items
            list_state = ws_state if list_mode == "workspaces" else chat_state
            list_filter = ws_filter if list_mode == "workspaces" else chat_filter

            renderer.list_pane.draw_frame(
                list_title, focused=(focus != "preview"), filter_text=list_filter, force=force_full
            )
            renderer.list_pane.draw_inner_rows(
                _list_rows(
                    layout.workspaces,
                    list_items,
                    list_state,
                    focused=(focus != "preview"),
                    filter_text=list_filter,
                    theme=theme,
                    dim_all=(focus == "preview"),
                ),
                force=force_full,
            )
        else:
            if renderer.ws_pane is None or renderer.chats_pane is None:
                continue

            renderer.ws_pane.draw_frame(
                "Workspaces", focused=(focus == "workspaces"), filter_text=ws_filter, force=force_full
            )
            renderer.ws_pane.draw_inner_rows(
                _list_rows(
                    layout.workspaces,
                    ws_items,
                    ws_state,
                    focused=(focus == "workspaces"),
                    filter_text=ws_filter,
                    theme=theme,
                    dim_all=(focus == "preview"),
                ),
                force=force_full,
            )

            renderer.chats_pane.draw_frame(
                "Chat Sessions", focused=(focus == "chats"), filter_text=chat_filter, force=force_full
            )
            renderer.chats_pane.draw_inner_rows(
                _list_rows(
                    layout.conversations,
                    chat_items,
                    chat_state,
                    focused=(focus == "chats"),
                    filter_text=chat_filter,
                    theme=theme,
                    dim_all=(focus == "preview"),
                ),
                force=force_full,
            )

        if renderer.preview_pane is None:
            continue
        renderer.preview_pane.draw_frame("Preview", focused=(focus == "preview"), filter_text="", force=force_full)
        if renderer.preview_pane.inner:
            bottom_overlay = None
            if focus == "preview" and preview_loading_more and preview_inner_h > 0 and preview_inner_w > 0:
                bottom_overlay = ("Loading…", curses.A_DIM)
            renderer.preview_pane.draw_preview_lines(
                preview_lines,
                preview_state.scroll,
                use_terminal_scroll=(layout.mode == "1col"),
                bottom_overlay=bottom_overlay,
                force=force_full,
            )
        else:
            renderer.preview_pane.draw_inner_rows(
                _preview_rows(layout.preview, ws, selected_chat, msg, scroll=preview_state.scroll), force=force_full
            )

        if renderer.status is None:
            continue
        renderer.status.draw(status, right=update_right, right_attr=update_right_attr, force=force_full)

        if sync_output:
            _sync_output_begin()
        try:
            curses.doupdate()
        finally:
            if sync_output:
                _sync_output_end()

        # Poll while background work is in progress, otherwise block on input.
        try:
            ui_pending = bool(toast is not None or export_pending is not None or not update_q.empty())
            stdscr.timeout(_input_timeout_ms(bg_pending=bg.has_pending(), update_checking=update_checking, ui_pending=ui_pending))
        except Exception:
            pass

        ch = stdscr.getch()
        if ch == -1:
            continue

        if ch == curses.KEY_RESIZE:
            continue

        if _should_quit(ch=ch, input_mode=input_mode):
            if export_pending is not None:
                chat_id, out_path = export_pending
                # Finish saving after curses exits (handled by CLI).
                if selected_chat is not None and selected_chat.chat_id == chat_id:
                    raise ExportPendingExit(
                        out_path=out_path,
                        store_db_path=selected_chat.store_db_path,
                        chat_title=selected_chat.name or "chat",
                    )
            return None

        if input_mode:
            if ch in (27,):  # ESC
                input_mode = None
                continue
            if ch in (curses.KEY_ENTER, 10, 13):
                input_mode = None
                continue
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if input_mode == "ws":
                    ws_filter = ws_filter[:-1]
                else:
                    chat_filter = chat_filter[:-1]
                continue
            if 32 <= ch <= 126:
                if input_mode == "ws":
                    ws_filter += chr(ch)
                else:
                    chat_filter += chr(ch)
                continue
            continue

        # Ctrl+U: upgrade (when we can safely fast-forward)
        if ch == 21:  # ^U
            if update_status and update_status.supported and update_status.update_available:
                raise UpdateRequested()
            continue

        # Ctrl+S: save chat history (best-effort; writes to workspace folder).
        if ch == 19:  # ^S
            if selected_chat is None or not selected_chat.chat_id:
                toast = ("Save failed: no chat selected", time.monotonic() + 3.0)
                continue
            if not selected_chat.latest_root_blob_id:
                toast = ("Save failed: chat has no history", time.monotonic() + 3.0)
                continue

            base_dir = ws.workspace_path if (ws is not None and ws.workspace_path is not None) else Path.cwd()
            title = selected_chat.name or "chat"
            fname = build_export_filename(title=title, when=datetime.datetime.now(), ext=".md", max_title_len=80)
            default_path = base_dir / fname

            chosen = _prompt_save_path(stdscr, default_path=default_path)
            # Force redraw to remove modal window artifacts.
            force_full_next = True
            if chosen is None:
                toast = ("Save cancelled", time.monotonic() + 2.0)
                continue

            # Allow entering a directory; in that case use default filename inside it.
            s = str(chosen)
            if s.endswith("/") or s.endswith("\\") or (chosen.exists() and chosen.is_dir()):
                out_path = chosen / fname
            else:
                out_path = chosen

            # Normalize for display/writing.
            try:
                out_path = out_path.expanduser()
            except Exception:
                pass
            if not out_path.is_absolute():
                out_path = (Path.cwd() / out_path).absolute()

            # Avoid silent overwrite: choose a non-conflicting path.
            out_path = choose_nonconflicting_path(out_path.parent, out_path.name)

            cached = preview_full_cache.get(selected_chat.chat_id)
            if cached and isinstance(cached[1], str) and cached[1].strip():
                try:
                    write_text_file(out_path, cached[1])
                    toast = (f"Saved: {out_path}", time.monotonic() + 4.0)
                except Exception as e:
                    toast = (f"Save failed: {e}", time.monotonic() + 3.0)
                continue

            export_pending = (selected_chat.chat_id, out_path)
            # Create the file immediately so the user can see the target path right away,
            # then overwrite it once the full history finishes loading.
            try:
                write_text_file(out_path, "")
            except Exception:
                pass
            get_preview_full(selected_chat)
            toast = (f"Saving… {out_path}", time.monotonic() + 3600.0)
            continue

        if ch in (9,):  # Tab
            if focus == "workspaces":
                focus = "chats"
                last_list_focus = "chats"
            elif focus == "chats":
                focus = "preview"
            else:
                focus = "workspaces"
                last_list_focus = "workspaces"
            continue
        if ch == curses.KEY_LEFT:
            if focus == "preview":
                focus = last_list_focus
            elif focus == "chats":
                focus = "workspaces"
                last_list_focus = "workspaces"
            else:
                focus = "workspaces"
                last_list_focus = "workspaces"
            continue
        if ch == curses.KEY_RIGHT:
            if focus == "workspaces":
                focus = "chats"
                last_list_focus = "chats"
            elif focus == "chats":
                focus = "preview"
            else:
                focus = last_list_focus
            continue

        if ch in (ord("/"),):
            base = last_list_focus if focus == "preview" else focus
            input_mode = "ws" if base == "workspaces" else "chat"
            continue

        # Mouse support (best-effort)
        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except Exception:
                continue

            # Scroll wheel
            btn4 = getattr(curses, "BUTTON4_PRESSED", 0) or getattr(curses, "BUTTON4_CLICKED", 0)
            btn5 = getattr(curses, "BUTTON5_PRESSED", 0) or getattr(curses, "BUTTON5_CLICKED", 0)
            if btn4 and (bstate & btn4):
                delta = -3
            elif btn5 and (bstate & btn5):
                delta = 3
            else:
                delta = 0

            btn1_clicked = getattr(curses, "BUTTON1_CLICKED", 0)
            btn1_pressed = getattr(curses, "BUTTON1_PRESSED", 0)
            btn1_double = getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)
            is_click = (btn1_clicked and (bstate & btn1_clicked)) or (btn1_pressed and (bstate & btn1_pressed))
            is_double = bool(btn1_double and (bstate & btn1_double))
            now = time.monotonic()

            if layout.preview.contains(my, mx):
                if delta:
                    preview_state.move(delta, len(preview_lines), preview_view_h)
                elif is_click:
                    focus = "preview"
                continue

            if layout.mode != "1col" and layout.workspaces.contains(my, mx):
                if delta:
                    ws_state.move(delta, len(_filter_items(ws_items, ws_filter)))
                else:
                    idx = ws_state.scroll + max(0, my - (layout.workspaces.y + 1))
                    ws_state.selected = idx
                    ws_state.clamp(len(_filter_items(ws_items, ws_filter)))
                    focus = "workspaces"
                    last_list_focus = "workspaces"
                    chat_state.selected = 0
                    chat_state.scroll = 0
                continue

            if layout.mode != "1col" and layout.conversations.contains(my, mx):
                if delta:
                    chat_state.move(delta, len(_filter_items(chat_items, chat_filter)))
                else:
                    idx = chat_state.scroll + max(0, my - (layout.conversations.y + 1))
                    chat_state.selected = idx
                    chat_state.clamp(len(_filter_items(chat_items, chat_filter)))
                    focus = "chats"
                    last_list_focus = "chats"

                    if is_double or (
                        is_click
                        and last_click_target == ("chats", chat_state.selected)
                        and (now - last_click_at) <= 0.35
                    ):
                        if ws is None:
                            continue
                        filtered = _filter_items(chat_items, chat_filter)
                        if not filtered:
                            continue
                        chat_state.clamp(len(filtered))
                        selected = filtered[chat_state.selected][1]
                        if isinstance(selected, AgentChat):
                            return ws, selected
                        return ws, None

                    if is_click:
                        last_click_at = now
                        last_click_target = ("chats", chat_state.selected)
                continue

            if layout.mode == "1col" and layout.workspaces.contains(my, mx):
                if delta:
                    if (last_list_focus if focus == "preview" else focus) == "workspaces":
                        ws_state.move(delta, len(_filter_items(ws_items, ws_filter)))
                    else:
                        chat_state.move(delta, len(_filter_items(chat_items, chat_filter)))
                    continue

                list_mode = last_list_focus if focus == "preview" else focus
                idx = (ws_state.scroll if list_mode == "workspaces" else chat_state.scroll) + max(0, my - (layout.workspaces.y + 1))
                if (last_list_focus if focus == "preview" else focus) == "workspaces":
                    ws_state.selected = idx
                    ws_state.clamp(len(_filter_items(ws_items, ws_filter)))
                    chat_state.selected = 0
                    chat_state.scroll = 0
                    focus = "workspaces"
                    last_list_focus = "workspaces"
                    if is_click:
                        last_click_at = now
                        last_click_target = ("workspaces", ws_state.selected)
                    continue

                chat_state.selected = idx
                chat_state.clamp(len(_filter_items(chat_items, chat_filter)))
                focus = "chats"
                last_list_focus = "chats"
                if is_double or (
                    is_click and last_click_target == ("chats", chat_state.selected) and (now - last_click_at) <= 0.35
                ):
                    if ws is None:
                        continue
                    filtered = _filter_items(chat_items, chat_filter)
                    if not filtered:
                        continue
                    chat_state.clamp(len(filtered))
                    selected = filtered[chat_state.selected][1]
                    if isinstance(selected, AgentChat):
                        return ws, selected
                    return ws, None
                if is_click:
                    last_click_at = now
                    last_click_target = ("chats", chat_state.selected)
                continue

            continue

        # Preview keyboard scrolling
        if focus == "preview":
            if ch in (curses.KEY_UP, ord("k")):
                preview_state.move(-1, len(preview_lines), preview_view_h)
                continue
            if ch in (curses.KEY_DOWN, ord("j")):
                preview_state.move(1, len(preview_lines), preview_view_h)
                continue
            if ch == curses.KEY_PPAGE:
                preview_state.page(-1, len(preview_lines), preview_view_h)
                continue
            if ch == curses.KEY_NPAGE:
                preview_state.page(1, len(preview_lines), preview_view_h)
                continue
            if ch == curses.KEY_HOME:
                preview_state.scroll = 0
                continue
            if ch == curses.KEY_END:
                preview_state.scroll = max(0, len(preview_lines) - max(1, preview_view_h))
                continue
            continue

        # Keyboard navigation
        if focus == "workspaces":
            n = len(_filter_items(ws_items, ws_filter))
            view_h = max(1, layout.workspaces.h - 2)
            if ch in (curses.KEY_UP, ord("k")):
                ws_state.move(-1, n)
            elif ch in (curses.KEY_DOWN, ord("j")):
                ws_state.move(1, n)
            elif ch == curses.KEY_PPAGE:
                ws_state.page(-1, view_h, n)
            elif ch == curses.KEY_NPAGE:
                ws_state.page(1, view_h, n)
            elif ch == curses.KEY_HOME:
                ws_state.selected = 0
            elif ch == curses.KEY_END:
                ws_state.selected = max(0, n - 1)
            else:
                continue
            ws_state.ensure_visible(view_h, n)
            chat_state.selected = 0
            chat_state.scroll = 0
            continue

        # focus == chats
        n = len(_filter_items(chat_items, chat_filter))
        view_h = max(1, layout.conversations.h - 2)
        if ch in (curses.KEY_UP, ord("k")):
            chat_state.move(-1, n)
            chat_state.ensure_visible(view_h, n)
            continue
        if ch in (curses.KEY_DOWN, ord("j")):
            chat_state.move(1, n)
            chat_state.ensure_visible(view_h, n)
            continue
        if ch == curses.KEY_PPAGE:
            chat_state.page(-1, view_h, n)
            chat_state.ensure_visible(view_h, n)
            continue
        if ch == curses.KEY_NPAGE:
            chat_state.page(1, view_h, n)
            chat_state.ensure_visible(view_h, n)
            continue
        if ch == curses.KEY_HOME:
            chat_state.selected = 0
            chat_state.ensure_visible(view_h, n)
            continue
        if ch == curses.KEY_END:
            chat_state.selected = max(0, n - 1)
            chat_state.ensure_visible(view_h, n)
            continue

        if ch in (curses.KEY_ENTER, 10, 13):
            if ws is None:
                continue
            filtered = _filter_items(chat_items, chat_filter)
            if not filtered:
                continue
            chat_state.clamp(len(filtered))
            selected = filtered[chat_state.selected][1]
            if isinstance(selected, AgentChat):
                return ws, selected
            return ws, None
