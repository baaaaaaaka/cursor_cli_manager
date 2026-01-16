from __future__ import annotations

import json
import os
import platform
import stat
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple


ENV_CCM_GITHUB_REPO = "CCM_GITHUB_REPO"  # e.g. "baaaaaaaka/cursor_cli_manager"
DEFAULT_GITHUB_REPO = "baaaaaaaka/cursor_cli_manager"


Fetch = Callable[[str, float, Dict[str, str]], bytes]


def _default_fetch(url: str, timeout_s: float, headers: Dict[str, str]) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read()


def _http_headers() -> Dict[str, str]:
    # GitHub API requires a User-Agent header.
    return {
        "User-Agent": "cursor-cli-manager",
        "Accept": "application/vnd.github+json",
    }


def get_github_repo() -> str:
    v = os.environ.get(ENV_CCM_GITHUB_REPO)
    return (v.strip() if isinstance(v, str) and v.strip() else DEFAULT_GITHUB_REPO)


def split_repo(repo: str) -> Tuple[str, str]:
    """
    Split "owner/name" into ("owner", "name").
    """
    s = (repo or "").strip()
    if not s or "/" not in s:
        raise ValueError(f"Invalid GitHub repo: {repo!r} (expected 'owner/name').")
    owner, name = s.split("/", 1)
    owner = owner.strip()
    name = name.strip()
    if not owner or not name:
        raise ValueError(f"Invalid GitHub repo: {repo!r} (expected 'owner/name').")
    return owner, name


def _parse_version_tuple(v: str) -> Optional[Tuple[int, ...]]:
    s = (v or "").strip()
    if not s:
        return None
    if s.startswith("v") and len(s) > 1:
        s = s[1:]
    parts = s.split(".")
    out = []
    for p in parts:
        # Stop at the first non-digit segment (e.g. "0.5.6-rc1")
        digits = ""
        for ch in p:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits == "":
            break
        out.append(int(digits))
    return tuple(out) if out else None


def is_version_newer(remote: str, local: str) -> Optional[bool]:
    """
    Compare semantic-ish versions like "0.5.6" (and optional "v" prefix).

    Returns:
    - True if remote > local
    - False if remote <= local
    - None if either cannot be parsed
    """
    rv = _parse_version_tuple(remote)
    lv = _parse_version_tuple(local)
    if not rv or not lv:
        return None
    # Compare with length normalization.
    n = max(len(rv), len(lv))
    rv2 = rv + (0,) * (n - len(rv))
    lv2 = lv + (0,) * (n - len(lv))
    return rv2 > lv2


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str
    version: str


def fetch_latest_release(
    repo: str,
    *,
    timeout_s: float = 2.0,
    fetch: Fetch = _default_fetch,
) -> ReleaseInfo:
    owner, name = split_repo(repo)
    url = f"https://api.github.com/repos/{owner}/{name}/releases/latest"
    raw = fetch(url, timeout_s, _http_headers())
    obj = json.loads(raw.decode("utf-8", "replace"))
    if not isinstance(obj, dict):
        raise ValueError("unexpected GitHub API response shape")
    tag = obj.get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        raise ValueError("missing tag_name in GitHub API response")
    tag = tag.strip()
    ver = tag[1:] if tag.startswith("v") else tag
    return ReleaseInfo(tag=tag, version=ver)


def is_frozen_binary() -> bool:
    # PyInstaller sets sys.frozen; other bundlers may too.
    return bool(getattr(sys, "frozen", False))


def _glibc_version() -> Optional[Tuple[int, int]]:
    """
    Return glibc version as (major, minor), or None if not glibc / unknown.
    """
    if platform.system().lower() != "linux":
        return None
    # Prefer ctypes gnu_get_libc_version when available.
    try:
        import ctypes  # stdlib

        libc = ctypes.CDLL("libc.so.6")
        f = libc.gnu_get_libc_version
        f.restype = ctypes.c_char_p
        s = f()
        if not s:
            return None
        txt = s.decode("ascii", "ignore")
        parts = txt.split(".")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    # Fallback to platform.libc_ver (best-effort).
    try:
        lib, ver = platform.libc_ver()
        if (lib or "").lower() != "glibc":
            return None
        parts = (ver or "").split(".")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]), int(parts[1])
    except Exception:
        return None
    return None


def _normalize_arch(machine: str) -> str:
    m = (machine or "").lower()
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    return m or "unknown"


def select_release_asset_name(*, system: Optional[str] = None, machine: Optional[str] = None) -> str:
    """
    Choose the Release asset name for the current platform.

    Naming convention (expected on GitHub Releases):
    - ccm-linux-x86_64-glibc217
    - ccm-macos-x86_64
    - ccm-macos-arm64
    """
    sysname = (system or platform.system() or "").lower()
    arch = _normalize_arch(machine or platform.machine())

    if sysname == "linux":
        if arch != "x86_64":
            raise RuntimeError(f"Unsupported Linux arch: {arch}")
        gv = _glibc_version()
        if gv is None:
            raise RuntimeError("Unsupported Linux libc: need glibc >= 2.17")
        if gv < (2, 17):
            raise RuntimeError(f"Unsupported glibc: {gv[0]}.{gv[1]} (need >= 2.17)")
        return "ccm-linux-x86_64-glibc217"

    if sysname == "darwin":
        if arch == "x86_64":
            return "ccm-macos-x86_64"
        if arch == "arm64":
            return "ccm-macos-arm64"
        raise RuntimeError(f"Unsupported macOS arch: {arch}")

    raise RuntimeError(f"Unsupported OS: {sysname}")


def build_release_download_url(repo: str, *, tag: str, asset_name: str) -> str:
    owner, name = split_repo(repo)
    return f"https://github.com/{owner}/{name}/releases/download/{tag}/{asset_name}"


def build_checksums_download_url(repo: str, *, tag: str) -> str:
    owner, name = split_repo(repo)
    return f"https://github.com/{owner}/{name}/releases/download/{tag}/checksums.txt"


def parse_checksums_txt(txt: str) -> Dict[str, str]:
    """
    Parse a simple "sha256  filename" format.
    """
    out: Dict[str, str] = {}
    for ln in (txt or "").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) < 2:
            continue
        sha = parts[0].strip().lower()
        name = parts[-1].strip()
        if len(sha) >= 32 and name:
            out[name] = sha
    return out


def sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _atomic_replace(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(src), str(dest))


def download_and_install_release_binary(
    *,
    repo: str,
    tag: str,
    asset_name: str,
    dest_path: Path,
    timeout_s: float = 30.0,
    fetch: Fetch = _default_fetch,
    verify_checksums: bool = True,
) -> None:
    """
    Download a release asset and install it to dest_path (atomic replace).
    """
    url = build_release_download_url(repo, tag=tag, asset_name=asset_name)
    data = fetch(url, timeout_s, _http_headers())

    checksums: Dict[str, str] = {}
    if verify_checksums:
        try:
            c_url = build_checksums_download_url(repo, tag=tag)
            c_raw = fetch(c_url, timeout_s, _http_headers())
            checksums = parse_checksums_txt(c_raw.decode("utf-8", "replace"))
        except Exception:
            checksums = {}

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Optional[Path] = None
    try:
        # Create the temp file in dest dir so os.replace is atomic (and avoids EXDEV).
        with tempfile.NamedTemporaryFile(
            prefix=f".{dest_path.name}.",
            dir=str(dest_path.parent),
            delete=False,
        ) as f:
            f.write(data)
            tmp_path = Path(f.name)

        # Ensure executable bit.
        try:
            st = tmp_path.stat()
            os.chmod(str(tmp_path), st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except Exception:
            pass

        if verify_checksums and checksums:
            expected = checksums.get(asset_name)
            if expected:
                actual = sha256_file(tmp_path)
                if actual.lower() != expected.lower():
                    raise RuntimeError(f"checksum mismatch for {asset_name}: expected {expected}, got {actual}")

        _atomic_replace(tmp_path, dest_path)
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def default_install_path() -> Path:
    return Path.home() / ".local" / "bin" / "ccm"

