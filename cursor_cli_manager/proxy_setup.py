from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from cursor_cli_manager.proxy import ProxyError, ProxyHelperMissing, _call, _state_dir


@dataclass(frozen=True)
class SavedProfile:
    name: str
    id: str


@dataclass(frozen=True)
class SetupState:
    mode: str
    revision: int
    profile_id: Optional[str]
    profiles: Tuple[SavedProfile, ...]


Selection = Tuple[Optional[str], Optional[str]]
_DIRECT = (None, None)


def _has_state() -> bool:
    try:
        (_state_dir() / "proxy-v1.sqlite3").lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ProxyError("could not inspect CCM proxy state: {}".format(exc)) from exc
    return True


def _setup_state(*, required: bool = False) -> Optional[SetupState]:
    # Using the existing read-only list command also keeps older helpers from
    # creating a database when they receive an unfamiliar option.
    try:
        result = _call(["profile", "list", "--setup"])
    except ProxyHelperMissing:
        if required or _has_state():
            raise
        return None
    except ProxyError as exc:
        if str(exc) != "invalid proxy command or arguments: list":
            raise
        raise ProxyError("this ccm-proxy helper cannot read proxy preferences; rebuild or reinstall CCM") from exc
    if result == []:
        if required or _has_state():
            raise ProxyError("this ccm-proxy helper cannot read proxy preferences; rebuild or reinstall CCM")
        return None
    if isinstance(result, dict):
        mode, rev, profile_id = result.get("mode"), result.get("revision"), result.get("id")
        profiles = result.get("profiles")
        if (
            mode in ("unset", "direct", "ssh") and type(rev) is int and rev >= 0
            and (profile_id is None if mode != "ssh" else isinstance(profile_id, str) and bool(profile_id))
            and isinstance(profiles, list)
            and all(isinstance(p, dict) and isinstance(p.get("name"), str)
                    and isinstance(p.get("id"), str) and p["name"] and p["id"] for p in profiles)
        ):
            return SetupState(mode, rev, profile_id, tuple(SavedProfile(p["name"], p["id"]) for p in profiles))
    raise ProxyError("ccm-proxy returned invalid proxy preferences")


def _saved_selection(state: SetupState) -> Selection:
    if state.mode == "direct":
        return _DIRECT
    if state.mode == "ssh":
        for profile in state.profiles:
            if profile.id == state.profile_id:
                return profile.name, profile.id
        raise ProxyError("the selected SSH profile was removed; run ccm proxy init or specify --proxy-profile")
    if len(state.profiles) == 1:
        profile = state.profiles[0]
        return profile.name, profile.id
    if len(state.profiles) > 1:
        raise ProxyError("multiple SSH profiles are configured; run ccm proxy init or specify --proxy-profile")
    return _DIRECT


def _interactive() -> bool:
    if sys.stdin is None or sys.stderr is None or not sys.stdin.isatty() or not sys.stderr.isatty():
        return False
    if hasattr(os, "tcgetpgrp"):
        try:
            return os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp()
        except OSError:
            return False
    return True


def resolve_selection(explicit: Optional[str], *, prompt: bool) -> Selection:
    if explicit is not None:
        # Keep explicit selection lazy for browsing and dry-runs.
        return explicit, None
    state = _setup_state()
    if state is None:
        return _DIRECT
    if (
        prompt and _interactive()
        and state.mode == "unset" and len(state.profiles) != 1
    ):
        return _guide(state)
    return _saved_selection(state)


def _prompt(label: str, default: Optional[str] = None) -> str:
    suffix = " [{}]".format(default) if default is not None else ""
    print("{}{}: ".format(label, suffix), end="", file=sys.stderr, flush=True)
    try:
        if os.name != "nt" and sys.stdin.isatty():
            # A canonical terminal returns one line per read. A partial line
            # followed by Ctrl-D must not make buffered readline wait again.
            answer = os.read(sys.stdin.fileno(), 4096).decode(sys.stdin.encoding or "utf-8")
        else:
            answer = sys.stdin.readline()
    except (OSError, UnicodeError) as exc:
        raise ProxyError("could not read proxy setup input: {}".format(exc)) from exc
    if not answer.endswith("\n"):
        raise ProxyError("proxy setup canceled (input ended)")
    return answer.strip() or default or ""


def _yes_no(default: bool) -> bool:
    while True:
        answer = _prompt("Use an SSH proxy for Cursor CLI? (y/n)", "y" if default else "n").casefold()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        if answer in ("q", "quit"):
            raise ProxyError("proxy setup canceled")


def _required(label: str) -> str:
    while True:
        value = _prompt(label)
        if value:
            return value


def _host_aliases(path: Path) -> List[str]:
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            aliases = {}
            for line in stream:
                match = re.match(r"^\s*Host(?:\s*=\s*|\s+)(.*)$", line, re.IGNORECASE)
                if match:
                    try:
                        fields = shlex.split(match.group(1), comments=True)
                    except ValueError:
                        continue
                    for value in fields:
                        if re.fullmatch(r"[a-zA-Z0-9_.][a-zA-Z0-9_.-]*", value):
                            aliases.setdefault(value.casefold(), value)
            return list(aliases.values())
    except FileNotFoundError:
        return []
    except OSError as exc:
        print(_safe_display("Could not list SSH aliases from {} ({})".format(path, exc)), file=sys.stderr)
        return []


def _new_name(host: str, profiles: Tuple[SavedProfile, ...]) -> str:
    base = re.sub(r"[^a-zA-Z0-9_.-]", "_", host.strip("[]")).lstrip(".-")[:55] or "ssh"
    used = {p.name for p in profiles}
    name, suffix = base, 2
    while name in used:
        name = "{}-{}".format(base, suffix)
        suffix += 1
    return name


def _manual_args() -> Tuple[str, List[str]]:
    host = _required("SSH host")
    while True:
        port = _prompt("SSH port", "22")
        if re.fullmatch(r"[0-9]{1,5}", port) and 1 <= int(port) <= 65535:
            break
        print("Enter a port from 1 to 65535.", file=sys.stderr)
    return host, ["--host", host, "--port", port, "--user", _required("SSH user")]


def _safe_display(value: str) -> str:
    return "".join(c for c in value if c.isprintable() or c == "\n")


def _probe_and_save(
    state: SetupState, action: Sequence[str], *, inspect: Optional[Sequence[str]] = None,
) -> Optional[Selection]:
    try:
        if inspect is not None:
            result = _call(["setup", "inspect", *inspect])
            if not isinstance(result, str):
                raise ProxyError("ccm-proxy returned invalid OpenSSH configuration")
            info = {}
            for line in result.splitlines():
                key, _, value = line.partition(" ")
                if key in ("hostname", "user", "port"):
                    info[key] = _safe_display(value)
            print("Testing SSH configuration for {}@{}:{}.".format(
                info.get("user", "?"), info.get("hostname", "?"), info.get("port", "?"),
            ), file=sys.stderr)
        _call(["setup", action[0], str(state.revision), *action[1:]])
    except ProxyError as exc:
        if exc.code == "setup_probe":
            print(_safe_display(str(exc)), file=sys.stderr)
            return None
        if exc.code != "setup_conflict":
            raise
        print("Proxy settings changed in another CCM process. Using the saved choice.", file=sys.stderr)
    updated = _setup_state(required=True)
    assert updated is not None
    return _saved_selection(updated)


def _guide(
    state: SetupState, *, ssh_config: Optional[Path] = None, route_target: Optional[str] = None,
) -> Selection:
    print("CCM can use an SSH tunnel for Cursor CLI and CCM downloads.", file=sys.stderr)
    if not _yes_no(state.mode == "ssh"):
        selection = _probe_and_save(state, ["direct"])
        assert selection is not None
        return selection

    config = ssh_config or Path.home() / ".ssh" / "config"
    aliases = _host_aliases(config)
    while True:
        options = [p.name + " (saved profile)" for p in state.profiles] + [
            alias + " (SSH config)" for alias in aliases
        ]
        for index, label in enumerate(options, 1):
            print("  {}. {}".format(index, label), file=sys.stderr)
        print("  0. Enter SSH connection details\n  a. Enter an SSH config file and alias\n  q. Cancel",
              file=sys.stderr)
        default = "1" if options else "0"
        choice = _prompt("Choose an SSH connection", default).lower()
        if choice in ("q", "quit"):
            raise ProxyError("proxy setup canceled")
        if choice == "0":
            host, flags = _manual_args()
            inspect = None
        elif choice == "a":
            path = _prompt("SSH config file", str(config))
            host = _required("SSH config alias")
            flags = ["--host", host, "--ssh-config", str(Path(path).expanduser().absolute())]
            inspect = flags
        elif re.fullmatch(r"[0-9]{1,9}", choice) and 1 <= int(choice) <= len(options):
            index = int(choice) - 1
            if index < len(state.profiles):
                old = state.profiles[index]
                if route_target:
                    print("Choose a new SSH connection to set a route target.", file=sys.stderr)
                    continue
                selection = _probe_and_save(state, ["use", old.name, old.id])
                if selection is not None:
                    return selection
                continue
            host = aliases[index - len(state.profiles)]
            flags = ["--host", host, "--ssh-config", str(config)]
            inspect = flags
        else:
            print("Enter a listed choice.", file=sys.stderr)
            continue
        name = _new_name(host, state.profiles)
        args = [name, *flags]
        if route_target:
            args += ["--route-target", route_target]
        selection = _probe_and_save(state, ["add", *args], inspect=[name, *inspect] if inspect else None)
        if selection is not None:
            return selection


def init_command(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="ccm proxy init", description="Choose whether CCM uses an SSH proxy.")
    parser.add_argument("--ssh-config", type=Path, help="List aliases from this OpenSSH config file.")
    parser.add_argument("--route-target", help="Verify this host:port through the new tunnel.")
    options = parser.parse_args(args)
    if not _interactive():
        raise ProxyError("ccm proxy init needs an interactive terminal; use ccm proxy profile add for scripted setup")
    state = _setup_state(required=True)
    assert state is not None
    config = options.ssh_config.expanduser().absolute() if options.ssh_config is not None else None
    name, _ = _guide(state, ssh_config=config, route_target=options.route_target)
    print("Saved SSH proxy profile {}.".format(name) if name else "Saved preference to run without CCM's SSH proxy.")
    return 0
