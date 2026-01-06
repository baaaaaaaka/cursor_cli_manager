from __future__ import annotations

import curses
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple
from cursor_cli_manager.formatting import (
    clamp,
    format_epoch_ms,
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
    list_h = max(6, int(usable_h * 0.60))
    prev_h = max(1, usable_h - list_h)
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


def _draw_box(stdscr: "curses.window", rect: Rect, title: str, *, focused: bool = False) -> None:
    if rect.h <= 0 or rect.w <= 0:
        return
    try:
        box = stdscr.derwin(rect.h, rect.w, rect.y, rect.x)
        box.erase()
        border_attr = curses.A_BOLD if focused else 0
        if border_attr:
            box.attron(border_attr)
        box.box()
        if border_attr:
            box.attroff(border_attr)

        if focused:
            t = f" > {title} < "
            title_attr = curses.A_REVERSE | curses.A_BOLD
        else:
            t = f" {title} "
            title_attr = curses.A_REVERSE
        _safe_addstr(box, 0, max(1, (rect.w - len(t)) // 2), truncate_to_width(t, rect.w - 2), title_attr)
        box.noutrefresh()
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


def _render_list(
    stdscr: "curses.window",
    rect: Rect,
    title: str,
    items: List[Tuple[str, object]],
    state: ListState,
    *,
    focused: bool,
    filter_text: str,
    theme: Theme,
) -> None:
    _draw_box(stdscr, rect, title, focused=focused)
    if rect.h < 3 or rect.w < 4:
        return

    inner_y = rect.y + 1
    inner_x = rect.x + 1
    inner_h = rect.h - 2
    inner_w = rect.w - 2

    view_h = inner_h
    filtered = _filter_items(items, filter_text)
    state.clamp(len(filtered))
    state.ensure_visible(view_h, len(filtered))

    # Optional filter indicator in the last line.
    if filter_text:
        hint = f"/{filter_text}"
        _safe_addstr(
            stdscr,
            rect.y + rect.h - 1,
            rect.x + 2,
            truncate_to_width(hint, rect.w - 4),
            curses.A_DIM,
        )

    start = state.scroll
    end = min(len(filtered), start + view_h)

    for row, idx in enumerate(range(start, end)):
        label, _ = filtered[idx]
        line = truncate_to_width(label, inner_w)
        if idx == state.selected:
            attr = theme.focused_selected_attr if focused else theme.unfocused_selected_attr
        else:
            attr = 0
        _safe_addstr(stdscr, inner_y + row, inner_x, line.ljust(inner_w), attr)


def _render_preview(
    stdscr: "curses.window",
    rect: Rect,
    workspace: Optional[AgentWorkspace],
    chat: Optional[AgentChat],
    message: Optional[str],
) -> None:
    _draw_box(stdscr, rect, "Preview", focused=False)
    if rect.h < 3 or rect.w < 4:
        return

    inner_y = rect.y + 1
    inner_x = rect.x + 1
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

    # Render within available height.
    for i, ln in enumerate(lines[:inner_h]):
        _safe_addstr(stdscr, inner_y + i, inner_x, truncate_to_width(ln, inner_w))


def _status_bar(stdscr: "curses.window", max_y: int, max_x: int, text: str) -> None:
    y = max_y - 1
    if y < 0:
        return
    bar = truncate_to_width(text, max_x).ljust(max_x)
    _safe_addstr(stdscr, y, 0, bar, curses.A_REVERSE)


def select_chat(
    stdscr: "curses.window",
    *,
    workspaces: List[AgentWorkspace],
    load_chats: Callable[[AgentWorkspace], List[AgentChat]],
    load_preview: Callable[[AgentChat], Tuple[Optional[str], Optional[str]]],
) -> Optional[Tuple[AgentWorkspace, Optional[AgentChat]]]:
    curses.curs_set(0)
    stdscr.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)

    theme = _init_theme()

    ws_state = ListState()
    chat_state = ListState()
    focus = "workspaces"  # or "chats"
    ws_filter = ""
    chat_filter = ""
    input_mode: Optional[str] = None  # "ws" | "chat"

    chat_cache: Dict[str, List[AgentChat]] = {}
    chat_error: Dict[str, str] = {}
    preview_cache: Dict[str, Tuple[Optional[str], Optional[str]]] = {}

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
        try:
            chats = load_chats(ws)
            chat_cache[key] = chats
            return chats
        except Exception as e:
            chat_cache[key] = []
            chat_error[key] = f"Failed to load chats: {e}"
            return []

    def get_preview(chat: AgentChat) -> Tuple[Optional[str], Optional[str]]:
        key = chat.chat_id
        if key in preview_cache:
            return preview_cache[key]
        try:
            role, text = load_preview(chat)
        except Exception:
            role, text = None, None
        preview_cache[key] = (role, text)
        return role, text

    while True:
        max_y, max_x = stdscr.getmaxyx()
        layout = compute_layout(max_y, max_x)

        stdscr.erase()

        ws_items: List[Tuple[str, object]] = []
        for ws in workspaces:
            extra = "" if ws.workspace_path is not None else "  (unknown path)"
            ws_items.append((f"{ws.display_name}{extra}", ws))

        ws = current_workspace()
        chats: List[AgentChat] = get_chats(ws) if ws else []
        chat_items: List[Tuple[str, object]] = []
        chat_items.append(("(New Agent)", NEW_AGENT_ITEM))
        for c in chats:
            ts = format_epoch_ms(c.created_at_ms)
            label = f"{c.name}  ({ts})"
            chat_items.append((label, c))

        if layout.mode == "1col":
            list_title = "Workspaces" if focus == "workspaces" else "Chat Sessions"
            list_items = ws_items if focus == "workspaces" else chat_items
            list_state = ws_state if focus == "workspaces" else chat_state
            list_filter = ws_filter if focus == "workspaces" else chat_filter
            _render_list(
                stdscr,
                layout.workspaces,
                list_title,
                list_items,
                list_state,
                focused=True,
                filter_text=list_filter,
                theme=theme,
            )
        else:
            _render_list(
                stdscr,
                layout.workspaces,
                "Workspaces",
                ws_items,
                ws_state,
                focused=(focus == "workspaces"),
                filter_text=ws_filter,
                theme=theme,
            )
            _render_list(
                stdscr,
                layout.conversations,
                "Chat Sessions",
                chat_items,
                chat_state,
                focused=(focus == "chats"),
                filter_text=chat_filter,
                theme=theme,
            )

        selected_chat: Optional[AgentChat] = None
        selected_is_new_agent = False
        if chat_items:
            filtered = _filter_items(chat_items, chat_filter)
            if filtered:
                chat_state.clamp(len(filtered))
                obj = filtered[chat_state.selected][1]
                if isinstance(obj, AgentChat):
                    selected_chat = obj
                else:
                    selected_is_new_agent = True

        msg = None
        if ws:
            msg = chat_error.get(ws.cwd_hash)

        if ws and selected_is_new_agent and not msg:
            if ws.workspace_path:
                msg = f"Start a new Cursor Agent chat in:\n{ws.workspace_path}"
            else:
                msg = "Workspace path is unknown. Run ccm from that folder to learn it."

        if ws and selected_chat and not msg:
            role, text = (None, None)
            if selected_chat.latest_root_blob_id:
                role, text = get_preview(selected_chat)
            # If the chat name is generic, try to derive a better one from history preview.
            derived_title: Optional[str] = None
            new_name = selected_chat.name
            if (
                isinstance(role, str)
                and role == "history"
                and isinstance(text, str)
                and new_name.strip().lower() in ("new agent", "untitled")
            ):
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

            # Persist derived name into the in-memory chat cache so the list label updates.
            if derived_title and ws and ws.cwd_hash in chat_cache:
                try:
                    updated: List[AgentChat] = []
                    for c in chat_cache[ws.cwd_hash]:
                        if c.chat_id == selected_chat.chat_id:
                            updated.append(AgentChat(**{**c.__dict__, "name": new_name}))  # type: ignore[arg-type]
                        else:
                            updated.append(c)
                    chat_cache[ws.cwd_hash] = updated
                except Exception:
                    pass

        _render_preview(stdscr, layout.preview, ws, selected_chat, msg)

        status = "Tab/Left/Right: switch  /: search  Enter: open  q: quit"
        if input_mode:
            status = "Type to search. Enter: apply  Esc: cancel"
        _status_bar(stdscr, max_y, max_x, status)
        curses.doupdate()

        ch = stdscr.getch()

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


