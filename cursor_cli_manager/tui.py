from __future__ import annotations

import curses
import queue
import threading
import time
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
        load_preview: Callable[[AgentChat], Tuple[Optional[str], Optional[str]]],
    ) -> None:
        self._load_chats = load_chats
        self._load_preview = load_preview
        self._q: "queue.Queue[Tuple[str, object]]" = queue.Queue()

        self._chats_inflight: Set[str] = set()  # ws_hash
        self._preview_inflight: Set[str] = set()  # chat_id
        self._lock = threading.Lock()

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._chats_inflight or self._preview_inflight) or (not self._q.empty())

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

    def ensure_preview(self, chat: AgentChat) -> None:
        key = chat.chat_id
        with self._lock:
            if key in self._preview_inflight:
                return
            self._preview_inflight.add(key)

        def _run() -> None:
            try:
                role, text = self._load_preview(chat)
                self._q.put(("preview_ok", key, role, text))
            except Exception as e:
                self._q.put(("preview_err", key, str(e)))
            finally:
                with self._lock:
                    self._preview_inflight.discard(key)

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

        # Border is expensive to redraw on some terminals; draw it only when forced.
        self._border_drawn = False
        self._title_key: Optional[Tuple[str, bool]] = None
        self._hint_key: Optional[str] = None
        self._last_title_span: Optional[Tuple[int, int]] = None  # (x, w) in columns
        self._last_hint_span: Optional[Tuple[int, int]] = None  # (x, w) in columns

        self._inner_cache: List[Tuple[str, int]] = []

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

        for i in range(min(inner_h, len(rows))):
            s, attr = rows[i]
            # Ensure each row is exactly inner width by display width.
            s = pad_to_width(s, inner_w)
            if not force and self._inner_cache[i] == (s, attr):
                continue
            _safe_addstr(self.inner, i, 0, s, attr)
            self._inner_cache[i] = (s, attr)

        # If the caller provided fewer rows than the visible height, blank the rest.
        blank = (" " * inner_w, 0)
        for i in range(len(rows), inner_h):
            if not force and self._inner_cache[i] == blank:
                continue
            _safe_addstr(self.inner, i, 0, blank[0], blank[1])
            self._inner_cache[i] = blank

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
            else:
                attr = 0
            out.append((line, attr))
        else:
            out.append((pad_to_width("", inner_w), 0))
    return out


def _preview_rows(
    rect: Rect,
    workspace: Optional[AgentWorkspace],
    chat: Optional[AgentChat],
    message: Optional[str],
) -> List[Tuple[str, int]]:
    if rect.h < 3 or rect.w < 4:
        return []
    inner_h = rect.h - 2
    inner_w = rect.w - 2

    lines: List[str] = []
    if message:
        lines.extend(wrap_text(message, inner_w))
    elif chat is None:
        lines.append("Select a chat session to see details.")
    else:
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

    out: List[Tuple[str, int]] = []
    for i in range(inner_h):
        ln = lines[i] if i < len(lines) else ""
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
        self._cache: Optional[str] = None
        self._w = max_x

    def draw(self, text: str, *, force: bool = False) -> None:
        bar = pad_to_width(truncate_to_width(text, self._w), self._w)
        if not force and self._cache == bar:
            return
        self._cache = bar
        _safe_addstr(self.win, 0, 0, bar, curses.A_REVERSE)
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
    load_preview: Callable[[AgentChat], Tuple[Optional[str], Optional[str]]],
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
    focus = "workspaces"  # or "chats"
    ws_filter = ""
    chat_filter = ""
    input_mode: Optional[str] = None  # "ws" | "chat"

    bg = _BackgroundLoader(load_chats=load_chats, load_preview=load_preview)

    chat_cache: Dict[str, List[AgentChat]] = {}
    chat_error: Dict[str, str] = {}
    chat_loading: Set[str] = set()
    preview_cache: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    preview_error: Dict[str, str] = {}
    preview_loading: Set[str] = set()
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

    def get_preview(chat: AgentChat) -> Tuple[Optional[str], Optional[str]]:
        key = chat.chat_id
        if key in preview_cache:
            return preview_cache[key]
        if key not in preview_loading:
            preview_loading.add(key)
            bg.ensure_preview(chat)
        return None, None

    renderer = _Renderer(stdscr)

    while True:
        now = time.monotonic()
        spin = _spinner(now)

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
            elif kind == "preview_ok":
                _, chat_id, role, text = item
                if isinstance(chat_id, str):
                    preview_cache[chat_id] = (role if isinstance(role, str) else None, text if isinstance(text, str) else None)
                    preview_error.pop(chat_id, None)
                    preview_loading.discard(chat_id)
            elif kind == "preview_err":
                _, chat_id, err = item
                if isinstance(chat_id, str):
                    preview_cache[chat_id] = (None, None)
                    preview_error[chat_id] = f"Failed to load preview: {err}"
                    preview_loading.discard(chat_id)

        max_y, max_x = stdscr.getmaxyx()
        layout = compute_layout(max_y, max_x)
        force_full = renderer.ensure(layout, max_y, max_x)

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

        if ws and selected_chat and not msg:
            role, text = (None, None)
            if selected_chat.latest_root_blob_id:
                role, text = get_preview(selected_chat)
            if selected_chat.chat_id in preview_loading and (role is None and text is None):
                msg = f"{spin} Loading preview…"
            if selected_chat.chat_id in preview_error and not msg:
                msg = preview_error[selected_chat.chat_id]
            new_name = selected_chat.name
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

        # Draw panes with minimal updates.
        if layout.mode == "1col":
            if renderer.list_pane is None:
                continue
            list_title = "Workspaces" if focus == "workspaces" else "Chat Sessions"
            list_items = ws_items if focus == "workspaces" else chat_items
            list_state = ws_state if focus == "workspaces" else chat_state
            list_filter = ws_filter if focus == "workspaces" else chat_filter

            renderer.list_pane.draw_frame(list_title, focused=True, filter_text=list_filter, force=force_full)
            renderer.list_pane.draw_inner_rows(
                _list_rows(
                    layout.workspaces,
                    list_items,
                    list_state,
                    focused=True,
                    filter_text=list_filter,
                    theme=theme,
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
                ),
                force=force_full,
            )

        if renderer.preview_pane is None:
            continue
        renderer.preview_pane.draw_frame("Preview", focused=False, filter_text="", force=force_full)
        renderer.preview_pane.draw_inner_rows(_preview_rows(layout.preview, ws, selected_chat, msg), force=force_full)

        if renderer.status is None:
            continue
        renderer.status.draw(status, force=force_full)

        curses.doupdate()

        # Poll while background work is in progress, otherwise block on input.
        try:
            stdscr.timeout(80 if bg.has_pending() else -1)
        except Exception:
            pass

        ch = stdscr.getch()
        if ch == -1:
            continue

        if ch == curses.KEY_RESIZE:
            continue

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

        if ch in (ord("q"), ord("Q")):
            return None

        if ch in (9,):  # Tab
            focus = "chats" if focus == "workspaces" else "workspaces"
            continue
        if ch == curses.KEY_LEFT:
            focus = "workspaces"
            continue
        if ch == curses.KEY_RIGHT:
            focus = "chats"
            continue

        if ch in (ord("/"),):
            input_mode = "ws" if focus == "workspaces" else "chat"
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

            if layout.mode != "1col" and layout.workspaces.contains(my, mx):
                if delta:
                    ws_state.move(delta, len(_filter_items(ws_items, ws_filter)))
                else:
                    idx = ws_state.scroll + max(0, my - (layout.workspaces.y + 1))
                    ws_state.selected = idx
                    ws_state.clamp(len(_filter_items(ws_items, ws_filter)))
                    focus = "workspaces"
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
                    if focus == "workspaces":
                        ws_state.move(delta, len(_filter_items(ws_items, ws_filter)))
                    else:
                        chat_state.move(delta, len(_filter_items(chat_items, chat_filter)))
                    continue

                idx = (ws_state.scroll if focus == "workspaces" else chat_state.scroll) + max(
                    0, my - (layout.workspaces.y + 1)
                )
                if focus == "workspaces":
                    ws_state.selected = idx
                    ws_state.clamp(len(_filter_items(ws_items, ws_filter)))
                    chat_state.selected = 0
                    chat_state.scroll = 0
                    if is_click:
                        last_click_at = now
                        last_click_target = ("workspaces", ws_state.selected)
                    continue

                chat_state.selected = idx
                chat_state.clamp(len(_filter_items(chat_items, chat_filter)))
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
