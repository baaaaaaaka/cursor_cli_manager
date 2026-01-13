# Cursor Agent Chat Manager (ccm)

`ccm` is a terminal UI manager for **`cursor-agent`** (or cursor cli) chats (terminal-only). It helps you:

- Discover folders that have **cursor-agent chat sessions**
- Browse folders + sessions in a responsive **TUI** with a **preview pane** (message history)
- Resume a selected session via **`cursor-agent --resume <chatId>`**

This project targets **macOS** and **Linux**.

## Requirements

- Python **3.7+**
- Cursor installed
  - [Cursor](https://cursor.com/download), [Cursor CLI](https://cursor.com/cli)

## Install

### From source (recommended for development)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip3 install -e .
```

After installing, you will get two command names:

- `ccm`
- `cursor-cli-manager`

### From Git (recommended)

```bash
# HTTPS (recommended)
pip3 install "cursor-cli-manager @ git+https://github.com/baaaaaaaka/cursor_cli_manager@main"

# or pin to a commit:
pip3 install "cursor-cli-manager @ git+https://github.com/baaaaaaaka/cursor_cli_manager.git@<commit_sha>"
```

## Run

From the repo root:

```bash
python3 -m cursor_cli_manager
```

Or (after installing as a package):

```bash
ccm
# or
cursor-cli-manager
```

## TUI controls

- **Navigation**: Up/Down, PageUp/PageDown
- **Switch pane**: Tab / Left / Right (Workspaces → Chats → Preview)
- **Search**: `/` then type, Enter apply, Esc cancel
- **Open**: Enter, or double-click a row with the mouse
  - Select **`(New Agent)`** at the top of a workspace to start a brand-new terminal chat in that folder
- **Quit**: `q`
- **Preview scroll** (when Preview pane is focused): Up/Down, PageUp/PageDown (mouse wheel best-effort)
- **In-app update**: `Ctrl+U` (only works for **pip VCS installs with PEP 610** metadata; see below)
- **Mouse**: click to select (wheel support is best-effort and may be unavailable on some `curses` builds)

The bottom-right corner shows version/update info:

- `vX latest`: update check succeeded and you are up to date
- `vX`: update check unavailable (e.g. not installed via PEP 610 VCS)
- `vX Ctrl+U upgrade` (bold): update available

## Commands

- `ccm tui` (default): interactive TUI
- `ccm list`: print discovered workspaces and chat sessions as JSON
  - `--with-preview`: include last message preview (slower)
  - `--pretty`: pretty-print JSON
- `ccm doctor`: print diagnostics about detected `cursor-agent` storage + CLI
- `ccm open <chatId> --workspace <path>`: resume a chat session in the terminal
  - `--dry-run`: print command instead of executing

Note: global flags must come before the subcommand:

```bash
ccm --config-dir <dir> doctor
```

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

## Configuration (optional)

- `CURSOR_AGENT_PATH`: override the `cursor-agent` executable path
- `CURSOR_AGENT_CONFIG_DIR`: override the config dir (default: `~/.cursor`)
- `--config-dir <dir>`: override the config dir (same effect as `CURSOR_AGENT_CONFIG_DIR`)

## Versioning policy

Starting from **v0.5.0**, **every change must bump the version** in both:

- `pyproject.toml` (`[project].version`)
- `cursor_cli_manager/__init__.py` (`__version__`)

## Notes

- UI chrome is **English-only**. Session titles and previews are shown as stored/extracted.
- Workspaces are keyed by `md5(cwd)` in `~/.cursor/chats/<hash>/...`, which is not reversible. `ccm` auto-learns
  a best-effort mapping and stores it in `~/.cursor/ccm-workspaces.json` (or your overridden config dir) so
  “Unknown (<hash>)” entries can become real folder names after you run `ccm` in that folder once.
- The TUI hides workspaces whose mapped folder no longer exists.
- We intentionally avoid third-party dependencies; everything uses the Python standard library.

