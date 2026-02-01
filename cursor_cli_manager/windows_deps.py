from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable, Optional, Tuple


_RIPGREP_API_URL = "https://api.github.com/repos/BurntSushi/ripgrep/releases/latest"
_RIPGREP_TIMEOUT_S = 20


def is_windows() -> bool:
    return sys.platform.startswith("win")


def _in_virtualenv() -> bool:
    return getattr(sys, "base_prefix", sys.prefix) != sys.prefix or bool(os.environ.get("VIRTUAL_ENV"))


def _run_pip_install(packages: Iterable[str]) -> bool:
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if not _in_virtualenv():
        cmd.append("--user")
    cmd.extend(packages)
    try:
        subprocess.run(cmd, check=True)
        return True
    except Exception as e:
        try:
            print(f"pip install failed: {e}", file=sys.stderr)
        except Exception:
            pass
        return False


def ensure_windows_curses() -> bool:
    if not is_windows():
        return True
    try:
        import curses  # noqa: F401

        return True
    except Exception:
        pass

    try:
        print("windows-curses not found; installing...", file=sys.stderr)
    except Exception:
        pass
    if not _run_pip_install(["windows-curses"]):
        return False
    try:
        import curses  # noqa: F401

        return True
    except Exception:
        return False


def _default_windows_bin_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if root:
        base = Path(root)
    else:
        base = Path.home() / "AppData" / "Local"
    return base / "ccm" / "bin"


def _path_separator_for_prepend(current: str, dir_path: Path) -> str:
    if is_windows():
        return ";"
    if ";" in current:
        return ";"
    s = str(dir_path)
    if ":\\" in s or ":\\" in current:
        return ";"
    return os.pathsep


def _prepend_to_path(dir_path: Path) -> None:
    try:
        current = os.environ.get("PATH", "")
        sep = _path_separator_for_prepend(current, dir_path)
        parts = [p for p in current.split(sep) if p]
        lower = {p.lower() for p in parts}
        if str(dir_path).lower() not in lower:
            if current:
                os.environ["PATH"] = str(dir_path) + sep + current
            else:
                os.environ["PATH"] = str(dir_path)
    except Exception:
        return


def _ripgrep_arch_suffix() -> Optional[str]:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "x86_64-pc-windows-msvc"
    if m in ("arm64", "aarch64"):
        return "aarch64-pc-windows-msvc"
    if m in ("x86", "i386", "i686"):
        return "i686-pc-windows-msvc"
    return None


def _fetch_ripgrep_asset(arch_suffix: str) -> Optional[Tuple[str, str]]:
    try:
        req = urllib.request.Request(_RIPGREP_API_URL, headers={"User-Agent": "ccm"})
        with urllib.request.urlopen(req, timeout=_RIPGREP_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        try:
            print(f"ripgrep release fetch failed: {e}", file=sys.stderr)
        except Exception:
            pass
        return None

    assets = data.get("assets")
    if not isinstance(assets, list):
        return None
    expected_suffix = f"{arch_suffix}.zip"
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        url = asset.get("browser_download_url")
        if not isinstance(name, str) or not isinstance(url, str):
            continue
        if name.startswith("ripgrep-") and name.endswith(expected_suffix):
            return name, url
    return None


def _download_file(url: str, dest: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ccm"})
        with urllib.request.urlopen(req, timeout=_RIPGREP_TIMEOUT_S) as resp:
            dest.write_bytes(resp.read())
        return True
    except Exception as e:
        try:
            print(f"ripgrep download failed: {e}", file=sys.stderr)
        except Exception:
            pass
        return False


def _extract_rg(zip_path: Path, dest: Path) -> bool:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            target = None
            for info in zf.infolist():
                name = Path(info.filename).name
                if name.lower() == "rg.exe":
                    target = info
                    break
            if target is None:
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(target) as src, dest.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        return True
    except Exception as e:
        try:
            print(f"ripgrep extract failed: {e}", file=sys.stderr)
        except Exception:
            pass
        return False


def ensure_ripgrep(*, bin_dir: Optional[Path] = None) -> Optional[Path]:
    if not is_windows():
        return None
    existing = shutil.which("rg") or shutil.which("rg.exe")
    if existing:
        return Path(existing)
    install_dir = bin_dir or _default_windows_bin_dir()
    rg_path = install_dir / "rg.exe"
    if rg_path.exists():
        _prepend_to_path(install_dir)
        return rg_path

    arch_suffix = _ripgrep_arch_suffix()
    if not arch_suffix:
        return None
    asset = _fetch_ripgrep_asset(arch_suffix)
    if not asset:
        return None
    name, url = asset

    try:
        print(f"ripgrep not found; downloading {name}...", file=sys.stderr)
    except Exception:
        pass

    with tempfile.TemporaryDirectory() as td:
        zip_path = Path(td) / name
        if not _download_file(url, zip_path):
            return None
        if not _extract_rg(zip_path, rg_path):
            return None

    _prepend_to_path(install_dir)
    return rg_path if rg_path.exists() else None


def ensure_windows_deps() -> None:
    if not is_windows():
        return
    ensure_windows_curses()
    ensure_ripgrep()
