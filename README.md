# Cursor Agent Chat Manager (ccm)

Install (recommended):

macOS/Linux (binary):

```bash
curl -fsSL https://raw.githubusercontent.com/baaaaaaaka/cursor_cli_manager/main/scripts/install_ccm.sh | sh
```

Windows (pip + HTTPS):

```bash
pip3 install "cursor-cli-manager @ git+https://github.com/baaaaaaaka/cursor_cli_manager@main"
```

`ccm` is a terminal UI manager for **`cursor-agent`** (or cursor cli) chats (terminal-only). It helps you:

- Discover folders that have **cursor-agent chat sessions**
- Browse folders + sessions in a responsive **TUI** with a **preview pane** (message history)
- Resume a selected session via **`cursor-agent --resume <chatId>`**

This project targets **macOS** and **Linux**, with best-effort Windows support.

## Requirements

- Python **3.7+**
- Cursor installed
  - [Cursor](https://cursor.com/download), [Cursor CLI](https://cursor.com/cli)

## Install

### macOS/Linux: one-line install (GitHub Releases binary)

Use the one-line install above. It will:

This will:

- Detect your OS/arch and download the matching `ccm` bundle from GitHub Releases
- Extract it into `~/.local/lib/ccm` and create symlinks in `~/.local/bin`:
  - `ccm`
  - `cursor-cli-manager`
- When `ccm` later starts a command that needs `cursor-agent`, it can auto-install Cursor Agent into:
  - POSIX: `~/.local/share/cursor-agent/versions/<version>`
  - Windows: `%LOCALAPPDATA%\\cursor-agent\\versions\\<version>`

Tip: for stability/security, pin the installer to a tag (or commit SHA):

```bash
curl -fsSL https://raw.githubusercontent.com/baaaaaaaka/cursor_cli_manager/v0.7.0/scripts/install_ccm.sh | sh
```

Notes:

- You can override the GitHub repo via `CCM_GITHUB_REPO=owner/name`
- Choose install dir via `CCM_INSTALL_DEST=/some/dir`
- Choose bundle install root via `CCM_INSTALL_ROOT=/some/dir`

After installing:

- If installed via **binary**, you will have `ccm` and `cursor-cli-manager`
- If installed via **pip**, you will have `ccm` and `cursor-cli-manager`

### Windows: pip install from Git (HTTPS)

To pin to a commit:

```bash
pip3 install "cursor-cli-manager @ git+https://github.com/baaaaaaaka/cursor_cli_manager.git@<commit_sha>"
```

### From source

```bash
git clone https://github.com/baaaaaaaka/cursor_cli_manager.git
cd cursor_cli_manager

pip3 install -e .

cd ..
rm -rf cursor_cli_manager
```

## Run

After installing as a package:
```bash
ccm
# or
cursor-cli-manager
```

Or from the repo root:

```bash
python3 -m cursor_cli_manager
```

When you start `ccm` or run `ccm open` and no usable `cursor-agent` is found, `ccm` will try to install it automatically.
This auto-install path is built into `ccm` itself, so it does not require an external `python`, `curl`, `wget`, `tar`,
or `unzip` on the user's machine.

## TUI controls

- **Navigation**: Up/Down, PageUp/PageDown
- **Switch pane**: Tab / Left / Right (Workspaces → Chats → Preview)
- **Search**: `/` then type, Enter apply, Esc cancel
- **Open**: Enter, or double-click a row with the mouse
  - Select **`(New Agent)`** at the top of a workspace to start a brand-new terminal chat in that folder
- **Quit**: `q`
- **Preview scroll** (when Preview pane is focused): Up/Down, PageUp/PageDown (mouse wheel best-effort)
- **In-app update**: `Ctrl+U` (works for **GitHub-release binaries** and **pip VCS installs with PEP 610** metadata)
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
- `ccm upgrade`: upgrade in-place (GitHub-release binary or PEP 610 VCS install)

Note: global flags must come before the subcommand:

```bash
ccm --config-dir <dir> doctor
```

## SSH proxy

CCM can run a loopback HTTP proxy backed by an OpenSSH dynamic tunnel. Use a
release bundle or a platform wheel that contains the `ccm-proxy` helper. On a
source checkout, run `python scripts/build_proxy.py` before `pip install -e .`.
Building the helper requires Go 1.24 or newer. Running it requires OpenSSH and
an SSH server that permits TCP forwarding.

On the first interactive `ccm`, `ccm tui`, or `ccm open` with no proxy profiles,
CCM asks whether to use an SSH proxy. Press Enter to save the default No, or
choose Yes to use an OpenSSH alias or enter a host, port and user. CCM verifies
the tunnel before saving the profile. `ccm proxy init` opens the same guide
to change the saved choice. Canceling setup leaves the choice as it was.

An existing single profile is used when no choice is saved. With multiple
profiles, select one in `ccm proxy init` or pass `--proxy-profile NAME` for one
invocation. Non-interactive commands never ask. With no choice and no profile,
they run without CCM's SSH proxy and leave the first interactive choice open.
If a saved profile has been removed, run `ccm proxy init` to select another
profile or turn off the proxy.

```sh
ccm proxy init
ccm proxy init --ssh-config /path/to/ssh_config
ccm proxy profile add work --host work-jump
ccm --proxy-profile work tui
ccm --proxy-profile work open CHAT_ID --workspace /path/to/workspace
ccm --proxy-profile work upgrade
```

`--host` accepts an OpenSSH alias or hostname. Specify `--user` and `--port`
when you need to override the SSH configuration. To use a custom OpenSSH file
in `profile add`, pass `--ssh-config` with an absolute path and leave the user
and port to OpenSSH. If an alias from an `Include` file is absent from the
guide's list, enter it using the SSH config file and alias option. The tunnel
uses existing SSH keys, agents, known hosts and
`ProxyJump` settings. Verify and trust the host key with OpenSSH before starting
the profile. `--route-target HOST:PORT` on `init` or `profile add` adds a TCP
test through the tunnel. Domain names in proxied requests are resolved on the
SSH server.

| Command | Behavior |
| --- | --- |
| `ccm proxy init` | Choose and verify the default SSH profile, or save the choice to run without it. |
| `ccm proxy profile list` | Read stored profiles. |
| `ccm proxy start NAME` | Start or reuse a gateway and print its verified HTTP URL. |
| `ccm proxy start NAME --foreground` | Keep the monitor attached to the terminal. |
| `ccm proxy list` | Check local listener identity and display gateway state. |
| `ccm proxy doctor NAME` | Report local status, forwarding counters and the log path. |
| `ccm proxy stop NAME` | Stop the gateway and confirm SSH cleanup. |
| `ccm proxy prune` | Clear expired ownership records when their processes have released their locks. |
| `ccm proxy reset NAME` | Stop the profile and clear ordinary recovery limits. Keep its settings and saved port. |
| `ccm proxy profile remove NAME` | Stop the gateway and remove the saved profile. |

The gateway keeps its public port during SSH recovery. It also reuses that port
after stop and restart. If another process owns the saved port, the profile
reports a bind error. Recovery budgets limit SSH retries and gateway restarts
independently. A cleanup failure stays in the profile for diagnosis.

With a saved or explicit profile, CCM uses the gateway for HTTP(S) downloads and for
Cursor Agent and HTTP(S)-based pip/Git subprocesses. Python downloads always
use the selected gateway. Child applications receive standard proxy variables
and a loopback bypass list. Those applications control which requests honor
the variables. Browsing and exporting history and `open --dry-run` do not start
the gateway. With a profile selected, press Ctrl+U to start an explicit TUI
upgrade. Windows standalone ZIP releases are updated by installing a new ZIP.

Profiles and logs are stored under `$XDG_STATE_HOME/ccm/proxy` (default
`~/.local/state/ccm/proxy`) on POSIX and `%LOCALAPPDATA%\ccm\proxy` on Windows.
Set `CCM_PROXY_STATE_DIR` to an absolute path to use separate state for tests.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
go -C proxy test -race ./...
```

## Configuration (optional)

- `CURSOR_AGENT_PATH`: override the `cursor-agent` executable path
- `--config-dir <dir>` or `CURSOR_AGENT_CONFIG_DIR`: override the config directory for CCM and the Cursor CLI it launches
- Without an override, CCM uses Cursor's directory order: `CURSOR_CONFIG_DIR`, `$XDG_CONFIG_HOME/cursor`, then `~/.cursor`
- `CCM_GITHUB_REPO=owner/name`: override the GitHub repo used by `ccm upgrade` for GitHub-release binaries
- `CCM_AUTO_INSTALL_CURSOR_AGENT=0`: disable automatic Cursor Agent installation
- `CCM_CURSOR_AGENT_INSTALLER_URL=<url>`: override the installer metadata source (default: `https://cursor.com/install`)
- `CCM_CURSOR_AGENT_INSTALL_ROOT=<dir>`: override the Cursor Agent install root
- `CCM_CURSOR_AGENT_BIN_DIR=<dir>`: override the Cursor Agent launcher directory
- `CCM_CURSOR_AGENT_INSTALL_TIMEOUT=<seconds>`: override the installer network timeout
- `CCM_CURSOR_AGENT_POSTINSTALL_PATCH=auto|off|force`: control the EL7/CentOS compatibility patch

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
