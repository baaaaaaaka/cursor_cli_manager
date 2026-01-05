# Cursor Agent Chat Manager (ccm)

`ccm` is a terminal UI manager for **`cursor-agent`** chats (terminal-only). It helps you:

- Discover folders that have **cursor-agent chat sessions**
- Browse folders + sessions in a responsive **TUI** with a **preview pane**
- Resume a selected session via **`cursor-agent --resume <chatId>`**

This project targets **macOS first**, with **Linux** support planned.

## Requirements

- Python **3.11+**
- Cursor installed (for local data + `cursor-agent`)

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

## Commands

- `ccm tui` (default): interactive TUI
- `ccm list`: print discovered workspaces and chat sessions as JSON
- `ccm doctor`: print diagnostics about detected `cursor-agent` storage + CLI
- `ccm open <chatId> --workspace <path>`: resume a chat session in the terminal

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

## Configuration (optional)

- `CURSOR_AGENT_PATH`: override the `cursor-agent` executable path
- `CURSOR_AGENT_CONFIG_DIR`: override the config dir (default: `~/.cursor`)

## Notes

- UI chrome is **English-only**. Session titles and previews are shown as stored/extracted.
- Workspaces are keyed by `md5(cwd)` in `~/.cursor/chats/<hash>/...`, which is not reversible. `ccm` auto-learns
  a best-effort mapping and stores it in `~/.cursor/ccm-workspaces.json` (or your overridden config dir) so
  “Unknown (<hash>)” entries can become real folder names after you run `ccm` in that folder once.
- We intentionally avoid third-party dependencies; everything uses the Python standard library.

