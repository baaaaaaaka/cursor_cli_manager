from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import platform
import stat
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import shutil
import ssl
import time


ENV_CCM_GITHUB_REPO = "CCM_GITHUB_REPO"  # e.g. "baaaaaaaka/cursor_cli_manager"
DEFAULT_GITHUB_REPO = "baaaaaaaka/cursor_cli_manager"
ENV_CCM_INSTALL_DEST = "CCM_INSTALL_DEST"
ENV_CCM_INSTALL_ROOT = "CCM_INSTALL_ROOT"
ENV_CCM_NCURSES_VARIANT = "CCM_NCURSES_VARIANT"

LINUX_ASSET_COMMON = "ccm-linux-x86_64-glibc217.tar.gz"
LINUX_ASSET_NC5 = "ccm-linux-x86_64-nc5.tar.gz"
LINUX_ASSET_NC6 = "ccm-linux-x86_64-nc6.tar.gz"


Fetch = Callable[[str, float, Dict[str, str]], bytes]


def _looks_like_cert_verify_error(err: BaseException) -> bool:
    # urllib wraps SSL errors in URLError(reason=...).
    if isinstance(err, ssl.SSLCertVerificationError):
        return True
    if isinstance(err, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(err):
        return True
    if isinstance(err, urllib.error.URLError):
        r = err.reason
        if isinstance(r, BaseException):
            return _looks_like_cert_verify_error(r)
        return "CERTIFICATE_VERIFY_FAILED" in str(r)
    return "CERTIFICATE_VERIFY_FAILED" in str(err)


def _bundled_cafile() -> Optional[str]:
    """
    Best-effort CA bundle path for frozen binaries.

    We intentionally do NOT override user-provided SSL_CERT_FILE/SSL_CERT_DIR.
    """
    if not is_frozen_binary():
        return None
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return None
    # Prefer certifi when present (PyInstaller hook bundles its cacert.pem).
    try:
        import certifi  # type: ignore[import-not-found]

        p = certifi.where()
        if isinstance(p, str) and p:
            pp = Path(p)
            if pp.exists():
                return str(pp)
    except Exception:
        pass
    # Fallback: look for a top-level cacert.pem in common PyInstaller layouts.
    try:
        mp = getattr(sys, "_MEIPASS", None)
        if isinstance(mp, str) and mp:
            cand = Path(mp) / "cacert.pem"
            if cand.exists():
                return str(cand)
    except Exception:
        pass
    try:
        cand2 = Path(sys.executable).resolve().parent / "cacert.pem"
        if cand2.exists():
            return str(cand2)
    except Exception:
        pass
    return None


def _default_fetch(url: str, timeout_s: float, headers: Dict[str, str]) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.read()
    except Exception as e:
        cafile = _bundled_cafile()
        if not cafile:
            raise
        if not _looks_like_cert_verify_error(e):
            raise
        ctx = ssl.create_default_context(cafile=cafile)
        with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
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


def default_install_bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def default_install_root_dir() -> Path:
    return Path.home() / ".local" / "lib" / "ccm"


def get_install_bin_dir() -> Path:
    v = os.environ.get(ENV_CCM_INSTALL_DEST)
    if isinstance(v, str) and v.strip():
        return Path(v).expanduser()
    return default_install_bin_dir()


def get_install_root_dir() -> Path:
    v = os.environ.get(ENV_CCM_INSTALL_ROOT)
    if isinstance(v, str) and v.strip():
        return Path(v).expanduser()
    return default_install_root_dir()


@contextmanager
def _install_lock(*, install_root: Path, wait_s: float = 0.0) -> "object":
    """
    Cross-process lock for install/upgrade operations.

    We use an atomic mkdir-based lock (more reliable than flock on some network filesystems).
    The installer script uses the same lock directory name so both code paths coordinate.
    """
    root = install_root.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    lock_dir = root / ".ccm.lock"
    deadline = time.monotonic() + max(0.0, float(wait_s or 0.0))

    while True:
        try:
            lock_dir.mkdir(mode=0o700)
            try:
                (lock_dir / "owner.txt").write_text(
                    f"pid={os.getpid()}\nexe={sys.executable}\n",
                    encoding="utf-8",
                )
            except Exception:
                pass
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"ccm install/upgrade already in progress (lock: {lock_dir})")
            time.sleep(0.1)
        except Exception as e:
            raise RuntimeError(f"failed to acquire install lock at {lock_dir}: {e}")

    try:
        yield object()
    finally:
        try:
            shutil.rmtree(lock_dir)
        except Exception:
            pass


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


def _normalize_ncurses_variant(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = value.strip().lower()
    if v in ("nc5", "nc6", "common"):
        return v
    if v == "5":
        return "nc5"
    if v == "6":
        return "nc6"
    return None


def _can_load_shared_lib(names: Tuple[str, ...]) -> bool:
    for name in names:
        try:
            ctypes.CDLL(name)
            return True
        except Exception:
            continue
    return False


def detect_linux_ncurses_variant(*, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """
    Best-effort detection of the preferred ncurses ABI for Linux.

    Returns "nc6", "nc5", or None if unknown.
    """
    if (platform.system() or "").lower() != "linux":
        return None
    environ = env or os.environ
    override = _normalize_ncurses_variant(environ.get(ENV_CCM_NCURSES_VARIANT))
    if override == "common":
        return None
    if override in ("nc5", "nc6"):
        return override
    if _can_load_shared_lib(("libtinfo.so.6", "libncursesw.so.6")):
        return "nc6"
    if _can_load_shared_lib(("libtinfo.so.5", "libncursesw.so.5")):
        return "nc5"
    return None


def linux_asset_name_for_variant(variant: Optional[str]) -> str:
    if variant == "nc6":
        return LINUX_ASSET_NC6
    if variant == "nc5":
        return LINUX_ASSET_NC5
    return LINUX_ASSET_COMMON


def detect_frozen_binary_ncurses_variant() -> Optional[str]:
    """
    Inspect the bundled libs to infer the ncurses ABI of the running binary.
    """
    if not is_frozen_binary():
        return None
    if (platform.system() or "").lower() != "linux":
        return None
    try:
        exe = Path(sys.executable).resolve()
    except Exception:
        return None
    internal = exe.parent / "_internal"
    if (internal / "libtinfo.so.6").exists() or (internal / "libncursesw.so.6").exists():
        return "nc6"
    if (internal / "libtinfo.so.5").exists() or (internal / "libncursesw.so.5").exists():
        return "nc5"
    return None


def select_release_asset_name(
    *,
    system: Optional[str] = None,
    machine: Optional[str] = None,
    linux_variant: Optional[str] = None,
) -> str:
    """
    Choose the Release asset name for the current platform.

    Naming convention (expected on GitHub Releases):
    - ccm-linux-x86_64-glibc217.tar.gz (legacy/common)
    - ccm-linux-x86_64-nc5.tar.gz
    - ccm-linux-x86_64-nc6.tar.gz
    - ccm-macos-x86_64.tar.gz
    - ccm-macos-arm64.tar.gz
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
        variant = _normalize_ncurses_variant(linux_variant)
        if not variant:
            variant = detect_linux_ncurses_variant()
        return linux_asset_name_for_variant(variant)

    if sysname == "darwin":
        if arch == "x86_64":
            return "ccm-macos-x86_64.tar.gz"
        if arch == "arm64":
            return "ccm-macos-arm64.tar.gz"
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


def _resolve_for_compare(p: Path) -> Path:
    try:
        return p.resolve(strict=False)
    except Exception:
        try:
            return p.absolute()
        except Exception:
            return p


def _abspath_for_compare(p: Path) -> Path:
    """
    Return a normalized absolute path without resolving symlinks.
    """
    try:
        return p.expanduser().absolute()
    except Exception:
        try:
            return p.absolute()
        except Exception:
            return p


def _is_within(child: Path, parent: Path) -> bool:
    """
    Return True if `child` is equal to or under `parent`, comparing best-effort resolved paths.
    """
    c = _resolve_for_compare(child)
    p = _resolve_for_compare(parent)
    cp = c.parts
    pp = p.parts
    return len(cp) >= len(pp) and cp[: len(pp)] == pp


def _atomic_symlink(target: Path, link: Path) -> None:
    """
    Atomically replace link with a symlink to target (best-effort).
    """
    # If the link already points to the target, do nothing.
    try:
        if link.is_symlink():
            try:
                raw = os.readlink(link)
                link_target = Path(raw)
                if not link_target.is_absolute():
                    link_target = link.parent / link_target
                if _resolve_for_compare(link_target) == _resolve_for_compare(target):
                    return
            except Exception:
                pass
    except Exception:
        pass
    # Prevent creating self-referential symlinks (can lead to ELOOP).
    if _abspath_for_compare(target) == _abspath_for_compare(link):
        raise RuntimeError(f"refusing to create self-referential symlink: {link} -> {target}")
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    try:
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
    except Exception:
        pass
    os.symlink(str(target), str(tmp))
    os.replace(str(tmp), str(link))


def _safe_extract_tar_gz(data: bytes, *, dest_dir: Path) -> None:
    """
    Extract a .tar.gz payload into dest_dir with basic path traversal checks.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        members = tf.getmembers()
        for m in members:
            name = m.name or ""
            # Disallow absolute paths and path traversal.
            if name.startswith(("/", "\\")):
                raise RuntimeError(f"unsafe tar member path: {name!r}")
            parts = Path(name).parts
            if any(p == ".." for p in parts):
                raise RuntimeError(f"unsafe tar member path: {name!r}")
            if m.issym() or m.islnk():
                ln = m.linkname or ""
                if ln.startswith(("/", "\\")) or any(p == ".." for p in Path(ln).parts):
                    raise RuntimeError(f"unsafe tar link target: {name!r} -> {ln!r}")
        # Python 3.14 changes tar extraction defaults; we already validate members,
        # so prefer preserving metadata when supported.
        try:
            tf.extractall(path=str(dest_dir), filter="fully_trusted")  # type: ignore[call-arg]
        except TypeError:
            tf.extractall(path=str(dest_dir))


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
    return get_install_bin_dir() / "ccm"


def download_and_install_release_bundle(
    *,
    repo: str,
    tag: str,
    asset_name: str,
    install_root: Path,
    bin_dir: Path,
    timeout_s: float = 30.0,
    fetch: Fetch = _default_fetch,
    verify_checksums: bool = True,
) -> Path:
    """
    Download and install an onedir bundle (tar.gz) from GitHub Releases.

    Layout:
      <install_root>/versions/<tag>/ccm/ccm   (executable inside bundle)
      <install_root>/current -> versions/<tag>
      <bin_dir>/ccm -> <install_root>/current/ccm/ccm
      <bin_dir>/cursor-cli-manager -> ccm
    """
    if not asset_name.endswith(".tar.gz"):
        raise RuntimeError(f"unsupported bundle asset: {asset_name} (expected .tar.gz)")

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

    if verify_checksums and checksums:
        expected = checksums.get(asset_name)
        if expected:
            actual = hashlib.sha256(data).hexdigest()
            if actual.lower() != expected.lower():
                raise RuntimeError(f"checksum mismatch for {asset_name}: expected {expected}, got {actual}")

    install_root = install_root.expanduser()
    bin_dir = bin_dir.expanduser()

    with _install_lock(install_root=install_root, wait_s=0.0):
        # Safety: never write the "ccm" entrypoint symlink into our own bundle directories.
        # This prevents corruption like current/ccm/ccm becoming a self-referential symlink.
        root_cmp = _resolve_for_compare(install_root)
        if _is_within(bin_dir, root_cmp / "current") or _is_within(bin_dir, root_cmp / "versions"):
            raise RuntimeError(
                f"refusing to install into {bin_dir}: it is inside the ccm bundle root {install_root}. "
                "Set CCM_INSTALL_DEST to a directory outside the bundle (e.g. ~/.local/bin)."
            )

        versions_dir = install_root / "versions"
        versions_dir.mkdir(parents=True, exist_ok=True)

        tmp_dir = Path(tempfile.mkdtemp(prefix=".ccm-extract-", dir=str(versions_dir)))
        try:
            _safe_extract_tar_gz(data, dest_dir=tmp_dir)
            exe = tmp_dir / "ccm" / "ccm"
            if not exe.exists():
                raise RuntimeError(f"invalid bundle: missing {exe}")
            try:
                st = exe.stat()
                os.chmod(str(exe), st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except Exception:
                pass

            version_dir = versions_dir / tag
            # Replace existing version directory (best-effort).
            if version_dir.exists():
                try:
                    shutil.rmtree(version_dir)
                except Exception:
                    pass
            os.replace(str(tmp_dir), str(version_dir))

            current = install_root / "current"
            _atomic_symlink(version_dir, current)

            target_exe = current / "ccm" / "ccm"
            bin_dir.mkdir(parents=True, exist_ok=True)
            _atomic_symlink(target_exe, bin_dir / "ccm")

            # pip-style alias
            alias = bin_dir / "cursor-cli-manager"
            try:
                if alias.exists() or alias.is_symlink():
                    alias.unlink()
            except Exception:
                pass
            try:
                os.symlink("ccm", str(alias))
            except Exception:
                # Fallback: point alias directly at target (may break if moved).
                try:
                    _atomic_symlink(target_exe, alias)
                except Exception:
                    pass

            return target_exe
        finally:
            if tmp_dir.exists():
                try:
                    shutil.rmtree(tmp_dir)
                except Exception:
                    pass

