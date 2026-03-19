from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cursor_cli_manager.agent_paths import CursorAgentDirs
from cursor_cli_manager.agent_patching import patch_cursor_agent_models, rollback_cursor_agent_patch
from cursor_cli_manager.github_release import Fetch, _default_fetch, _safe_extract_tar_gz
from cursor_cli_manager.update import _default_runner


ENV_CURSOR_AGENT_PATH = "CURSOR_AGENT_PATH"
ENV_CCM_AUTO_INSTALL_CURSOR_AGENT = "CCM_AUTO_INSTALL_CURSOR_AGENT"
ENV_CCM_CURSOR_AGENT_INSTALLER_URL = "CCM_CURSOR_AGENT_INSTALLER_URL"
ENV_CCM_CURSOR_AGENT_DOWNLOAD_BASE_URL = "CCM_CURSOR_AGENT_DOWNLOAD_BASE_URL"
ENV_CCM_CURSOR_AGENT_INSTALL_ROOT = "CCM_CURSOR_AGENT_INSTALL_ROOT"
ENV_CCM_CURSOR_AGENT_BIN_DIR = "CCM_CURSOR_AGENT_BIN_DIR"
ENV_CCM_CURSOR_AGENT_INSTALL_TIMEOUT = "CCM_CURSOR_AGENT_INSTALL_TIMEOUT"
ENV_CCM_CURSOR_AGENT_POSTINSTALL_PATCH = "CCM_CURSOR_AGENT_POSTINSTALL_PATCH"

DEFAULT_INSTALLER_URL = "https://cursor.com/install"
DEFAULT_INSTALL_TIMEOUT_S = 30.0
DEFAULT_INSTALL_LOCK_WAIT_S = 10.0
_INSTALL_LOCK_DIRNAME = ".ccm-cursor-agent-install.lock"
_INSTALLER_VERSION_RE = re.compile(r"downloads\.cursor\.com/lab/([^/]+)/\$\{OS\}/\$\{ARCH\}/agent-cli-package\.tar\.gz")


@dataclass(frozen=True)
class InstallerMetadata:
    version: str
    installer_url: str


@dataclass(frozen=True)
class CursorAgentInstallSpec:
    version: str
    system: str
    arch: str
    archive_kind: str
    download_url: str
    install_root: Path
    bin_dir: Path


@dataclass(frozen=True)
class CursorAgentResolveResult:
    path: Optional[str] = None
    source: Optional[str] = None
    error: Optional[str] = None
    is_explicit_override: bool = False
    repairable_default_install: bool = False


@dataclass(frozen=True)
class CursorAgentInstallResult:
    installed_path: str
    version: str
    performed_download: bool
    repaired_launchers: bool
    applied_compat_patch: bool
    notes: Tuple[str, ...] = field(default_factory=tuple)


def _is_truthy(v: Optional[str]) -> bool:
    if v is None:
        return False
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def auto_install_enabled() -> bool:
    v = os.environ.get(ENV_CCM_AUTO_INSTALL_CURSOR_AGENT)
    if v is None:
        return True
    return _is_truthy(v)


def default_install_root_dir() -> Path:
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "cursor-agent"
        return Path.home() / "AppData" / "Local" / "cursor-agent"
    return Path.home() / ".local" / "share" / "cursor-agent"


def default_install_bin_dir(*, install_root: Optional[Path] = None) -> Path:
    if sys.platform.startswith("win"):
        return (install_root or default_install_root_dir()) / "bin"
    return Path.home() / ".local" / "bin"


def get_cursor_agent_install_root() -> Path:
    v = os.environ.get(ENV_CCM_CURSOR_AGENT_INSTALL_ROOT)
    if isinstance(v, str) and v.strip():
        return Path(v).expanduser()
    return default_install_root_dir()


def get_cursor_agent_bin_dir(*, install_root: Optional[Path] = None) -> Path:
    v = os.environ.get(ENV_CCM_CURSOR_AGENT_BIN_DIR)
    if isinstance(v, str) and v.strip():
        return Path(v).expanduser()
    return default_install_bin_dir(install_root=install_root or get_cursor_agent_install_root())


def get_cursor_agent_install_timeout_s() -> float:
    raw = os.environ.get(ENV_CCM_CURSOR_AGENT_INSTALL_TIMEOUT)
    if not raw:
        return DEFAULT_INSTALL_TIMEOUT_S
    try:
        value = float(raw)
    except Exception:
        return DEFAULT_INSTALL_TIMEOUT_S
    return max(1.0, value)


def get_cursor_agent_installer_url() -> str:
    v = os.environ.get(ENV_CCM_CURSOR_AGENT_INSTALLER_URL)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return DEFAULT_INSTALLER_URL


def get_cursor_agent_download_base_url() -> str:
    v = os.environ.get(ENV_CCM_CURSOR_AGENT_DOWNLOAD_BASE_URL)
    if isinstance(v, str) and v.strip():
        return v.strip().rstrip("/")
    return "https://downloads.cursor.com"


def manual_install_hint() -> str:
    if sys.platform.startswith("win"):
        return "Install Cursor Agent manually from Cursor's official download flow or https://cursor.com/install from WSL."
    return "Install Cursor Agent manually with: curl https://cursor.com/install -fsS | bash"


def _normalize_system(value: Optional[str] = None) -> str:
    sysname = (value or platform.system() or "").lower()
    if sysname.startswith("linux"):
        return "linux"
    if sysname.startswith("darwin"):
        return "darwin"
    if sysname.startswith("windows") or sysname.startswith("win"):
        return "windows"
    return sysname


def _normalize_arch(value: Optional[str] = None) -> str:
    arch = (value or platform.machine() or "").lower()
    if arch in ("x86_64", "amd64"):
        return "x64"
    if arch in ("arm64", "aarch64"):
        return "arm64"
    return arch


def _path_exists_file(p: Path) -> bool:
    try:
        return p.exists() and p.is_file()
    except Exception:
        return False


def _candidate_names() -> List[str]:
    if sys.platform.startswith("win"):
        return ["cursor-agent.cmd", "agent.cmd", "cursor-agent.ps1", "agent.ps1", "cursor-agent", "agent"]
    return ["cursor-agent", "agent"]


def _bin_candidates(bin_dir: Path) -> List[Path]:
    return [bin_dir / name for name in _candidate_names()]


def _is_valid_cursor_agent_alias(path: str) -> bool:
    p = Path(path).expanduser()
    if not _path_exists_file(p):
        return False
    try:
        resolved = p.resolve()
    except Exception:
        resolved = p
    if resolved.name.lower().startswith("cursor-agent"):
        return True
    sibling_names = ("cursor-agent", "cursor-agent.cmd", "cursor-agent.ps1")
    for sibling in sibling_names:
        if _path_exists_file(p.parent / sibling):
            return True
    return False


def _resolve_explicit_path(value: Optional[str], *, source: str, explicit: bool) -> Optional[CursorAgentResolveResult]:
    if not value:
        return None
    p = Path(value).expanduser()
    if _path_exists_file(p):
        return CursorAgentResolveResult(path=str(p), source=source, is_explicit_override=explicit)
    return CursorAgentResolveResult(
        path=None,
        source=source,
        error=f"{source} points to a missing or non-file path: {p}",
        is_explicit_override=explicit,
    )


def _prepend_to_path(dir_path: Path) -> None:
    try:
        current = os.environ.get("PATH", "")
        sep = ";" if sys.platform.startswith("win") else os.pathsep
        parts = [p for p in current.split(sep) if p]
        lowered = {p.lower() for p in parts}
        if str(dir_path).lower() in lowered:
            return
        os.environ["PATH"] = str(dir_path) + (sep + current if current else "")
    except Exception:
        return


def _versions_dir(install_root: Path) -> Path:
    return install_root / "versions"


def _discover_installed_version_dirs(install_root: Path) -> List[Path]:
    versions_dir = _versions_dir(install_root)
    try:
        dirs = [p for p in versions_dir.iterdir() if p.is_dir() and not p.name.startswith(".tmp-")]
    except Exception:
        return []
    dirs.sort(key=lambda p: (p.name, str(p)), reverse=True)
    return dirs


def _version_dir_executable(version_dir: Path) -> Optional[Path]:
    candidates = ["cursor-agent.cmd", "cursor-agent.ps1"] if sys.platform.startswith("win") else ["cursor-agent"]
    for name in candidates:
        p = version_dir / name
        if _path_exists_file(p):
            return p
    return None


def _latest_installed_executable(install_root: Path) -> Optional[Path]:
    for version_dir in _discover_installed_version_dirs(install_root):
        exe = _version_dir_executable(version_dir)
        if exe is not None:
            return exe
    return None


def latest_cursor_agent_executable_in_versions_dir(versions_dir: Path) -> Optional[str]:
    try:
        version_dirs = [p for p in versions_dir.iterdir() if p.is_dir()]
    except Exception:
        return None
    version_dirs.sort(key=lambda p: (p.name, str(p)), reverse=True)
    for version_dir in version_dirs:
        exe = _version_dir_executable(version_dir)
        if exe is not None:
            return str(exe)
    return None


def resolve_cursor_agent_installation(*, explicit: Optional[str] = None) -> CursorAgentResolveResult:
    explicit_res = _resolve_explicit_path(explicit, source="explicit path", explicit=True)
    if explicit_res is not None:
        return explicit_res

    env_value = os.environ.get(ENV_CURSOR_AGENT_PATH)
    env_res = _resolve_explicit_path(env_value, source=f"${ENV_CURSOR_AGENT_PATH}", explicit=True)
    if env_res is not None:
        return env_res

    found = shutil.which("cursor-agent")
    if found:
        return CursorAgentResolveResult(path=found, source="PATH:cursor-agent")

    install_root = get_cursor_agent_install_root()
    bin_dir = get_cursor_agent_bin_dir(install_root=install_root)
    for candidate in _bin_candidates(bin_dir):
        if _path_exists_file(candidate):
            return CursorAgentResolveResult(path=str(candidate), source=f"default-bin:{candidate.name}")

    found_agent = shutil.which("agent")
    if found_agent and _is_valid_cursor_agent_alias(found_agent):
        return CursorAgentResolveResult(path=found_agent, source="PATH:agent")

    latest = _latest_installed_executable(install_root)
    if latest is not None:
        return CursorAgentResolveResult(
            path=None,
            source="default-install",
            error=f"installed versions exist under {install_root}, but launcher is missing or broken",
            repairable_default_install=True,
        )

    return CursorAgentResolveResult(
        path=None,
        source="missing",
        error="cursor-agent not found",
        repairable_default_install=False,
    )


def fetch_official_installer_metadata(
    *,
    timeout_s: Optional[float] = None,
    fetch: Fetch = _default_fetch,
    installer_url: Optional[str] = None,
) -> InstallerMetadata:
    url = installer_url or get_cursor_agent_installer_url()
    raw = fetch(url, timeout_s or get_cursor_agent_install_timeout_s(), {"User-Agent": "cursor-cli-manager"})
    txt = raw.decode("utf-8", "replace")
    match = _INSTALLER_VERSION_RE.search(txt)
    if not match:
        raise RuntimeError(f"failed to parse Cursor installer version from {url}")
    version = match.group(1).strip()
    if not version:
        raise RuntimeError(f"missing Cursor installer version in {url}")
    return InstallerMetadata(version=version, installer_url=url)


def select_cursor_agent_install_spec(
    metadata: InstallerMetadata,
    *,
    system: Optional[str] = None,
    machine: Optional[str] = None,
    install_root: Optional[Path] = None,
    bin_dir: Optional[Path] = None,
) -> CursorAgentInstallSpec:
    sysname = _normalize_system(system)
    arch = _normalize_arch(machine)
    root = (install_root or get_cursor_agent_install_root()).expanduser()
    bindir = (bin_dir or get_cursor_agent_bin_dir(install_root=root)).expanduser()

    if sysname in ("linux", "darwin"):
        if arch not in ("x64", "arm64"):
            raise RuntimeError(f"unsupported Cursor Agent architecture for {sysname}: {arch}")
        base = get_cursor_agent_download_base_url()
        url = f"{base}/lab/{metadata.version}/{sysname}/{arch}/agent-cli-package.tar.gz"
        return CursorAgentInstallSpec(
            version=metadata.version,
            system=sysname,
            arch=arch,
            archive_kind="tar.gz",
            download_url=url,
            install_root=root,
            bin_dir=bindir,
        )

    if sysname == "windows":
        if arch not in ("x64", "arm64"):
            raise RuntimeError(f"unsupported Cursor Agent architecture for Windows: {arch}")
        base = get_cursor_agent_download_base_url()
        url = f"{base}/lab/{metadata.version}/windows/{arch}/agent-cli-package.zip"
        return CursorAgentInstallSpec(
            version=metadata.version,
            system=sysname,
            arch=arch,
            archive_kind="zip",
            download_url=url,
            install_root=root,
            bin_dir=bindir,
        )

    raise RuntimeError(f"unsupported operating system for Cursor Agent install: {platform.system() or sysname}")


def _safe_extract_zip(data: bytes, *, dest_dir: Path) -> None:
    import io

    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            s = (name or "").strip()
            if not s:
                continue
            if s.startswith(("/", "\\")):
                raise RuntimeError(f"unsafe zip member path: {s!r}")
            parts = Path(s).parts
            if any(p == ".." for p in parts):
                raise RuntimeError(f"unsafe zip member path: {s!r}")
        zf.extractall(str(dest_dir))


def _validate_payload_dir(payload_dir: Path, *, system: str) -> None:
    required = ["cursor-agent", "index.js", "package.json"] if system != "windows" else [
        "cursor-agent.cmd",
        "cursor-agent.ps1",
        "index.js",
        "package.json",
        "node.exe",
    ]
    missing = [name for name in required if not _path_exists_file(payload_dir / name)]
    if missing:
        raise RuntimeError(f"invalid Cursor Agent package: missing {', '.join(missing)}")


def _payload_dir_from_extract(root: Path) -> Path:
    dist = root / "dist-package"
    if dist.is_dir():
        return dist
    return root


def _atomic_write_text(path: Path, text: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as f:
        f.write(text)
        tmp_path = Path(f.name)
    try:
        os.chmod(str(tmp_path), mode)
    except Exception:
        pass
    os.replace(str(tmp_path), str(path))


def _atomic_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    try:
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
    except Exception:
        pass
    os.symlink(str(target), str(tmp))
    os.replace(str(tmp), str(link))


def _install_posix_launcher(target: Path, link_path: Path) -> bool:
    try:
        _atomic_symlink(target, link_path)
        return True
    except Exception:
        versions_dir = target.parent.parent
        wrapper = (
            "#!/bin/sh\n"
            f'CURSOR_AGENT_VERSIONS_DIR="{versions_dir}"\n'
            "export CURSOR_AGENT_VERSIONS_DIR\n"
            f'exec "{target}" "$@"\n'
        )
        _atomic_write_text(link_path, wrapper, mode=0o755)
        return False


def _windows_wrapper_text(target: Path) -> str:
    return "@echo off\r\ncall \"" + str(target) + "\" %*\r\n"


def _repair_launchers(spec: CursorAgentInstallSpec) -> Tuple[bool, str]:
    latest = _latest_installed_executable(spec.install_root)
    if latest is None:
        raise RuntimeError(f"no installed Cursor Agent versions found under {spec.install_root}")
    target = latest
    spec.bin_dir.mkdir(parents=True, exist_ok=True)
    if spec.system == "windows":
        _atomic_write_text(spec.bin_dir / "cursor-agent.cmd", _windows_wrapper_text(target))
        _atomic_write_text(spec.bin_dir / "agent.cmd", _windows_wrapper_text(target))
        installed_path = spec.bin_dir / "cursor-agent.cmd"
    else:
        _install_posix_launcher(target, spec.bin_dir / "cursor-agent")
        _install_posix_launcher(target, spec.bin_dir / "agent")
        installed_path = spec.bin_dir / "cursor-agent"
    _prepend_to_path(spec.bin_dir)
    return True, str(installed_path)


def _install_launchers(spec: CursorAgentInstallSpec, *, target: Path) -> str:
    spec.bin_dir.mkdir(parents=True, exist_ok=True)
    if spec.system == "windows":
        _atomic_write_text(spec.bin_dir / "cursor-agent.cmd", _windows_wrapper_text(target))
        _atomic_write_text(spec.bin_dir / "agent.cmd", _windows_wrapper_text(target))
        installed_path = spec.bin_dir / "cursor-agent.cmd"
    else:
        _install_posix_launcher(target, spec.bin_dir / "cursor-agent")
        _install_posix_launcher(target, spec.bin_dir / "agent")
        installed_path = spec.bin_dir / "cursor-agent"
    _prepend_to_path(spec.bin_dir)
    return str(installed_path)


def _verify_cursor_agent_command(path: str, *, timeout_s: float = 5.0) -> None:
    p = Path(path)
    if not _path_exists_file(p):
        raise RuntimeError(f"installed cursor-agent path missing after install: {path}")
    if sys.platform.startswith("win") and p.suffix.lower() in (".cmd", ".bat"):
        cmd = ["cmd.exe", "/d", "/s", "/c", subprocess.list2cmdline([path, "--help"])]
    else:
        cmd = [path, "--help"]
    rc, out, err = _default_runner(cmd, timeout_s)
    txt = ((out or "") + "\n" + (err or "")).strip()
    if rc != 0:
        detail = f": {txt}" if txt else ""
        raise RuntimeError(f"installed cursor-agent failed verification (exit {rc}){detail}")


def _load_os_release() -> Dict[str, str]:
    out: Dict[str, str] = {}
    p = Path("/etc/os-release")
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out
    for line in txt.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"')
    return out


def _postinstall_patch_mode() -> str:
    raw = (os.environ.get(ENV_CCM_CURSOR_AGENT_POSTINSTALL_PATCH) or "auto").strip().lower()
    if raw in ("auto", "off", "force"):
        return raw
    return "auto"


def _should_apply_compat_patch() -> bool:
    mode = _postinstall_patch_mode()
    if mode == "off":
        return False
    if mode == "force":
        return True
    if _normalize_system() != "linux":
        return False
    os_release = _load_os_release()
    distro = (os_release.get("ID") or "").lower()
    version_id = (os_release.get("VERSION_ID") or "").strip()
    if distro in ("centos", "rhel", "rocky", "almalinux") and version_id.startswith("7"):
        return True
    return False


def _summarize_patch_report(rep: object) -> str:
    try:
        patched = len(getattr(rep, "patched_files", []) or [])
        repaired = len(getattr(rep, "repaired_files", []) or [])
        errors = len(getattr(rep, "errors", []) or [])
        return f"patched={patched} repaired={repaired} errors={errors}"
    except Exception:
        return "patch report unavailable"


def _snapshot_patch_inputs(versions_dir: Path) -> Dict[str, bytes]:
    snapshots: Dict[str, bytes] = {}
    for path in sorted(versions_dir.glob("*/*.index.js")):
        snapshots[str(path)] = path.read_bytes()
    return snapshots


def _path_is_within(path: Path, root: Path) -> bool:
    candidates = [path.expanduser()]
    root_candidates = [root.expanduser()]
    try:
        candidates.append(candidates[0].resolve())
    except Exception:
        pass
    try:
        root_candidates.append(root_candidates[0].resolve())
    except Exception:
        pass
    for candidate in candidates:
        for root_candidate in root_candidates:
            try:
                candidate.relative_to(root_candidate)
                return True
            except Exception:
                continue
    return False


def _best_effort_rmtree(path: Path) -> None:
    retry_delays = (0.1, 0.2, 0.5) if sys.platform.startswith("win") else ()
    for delay_s in (0.0,) + retry_delays:
        if delay_s > 0.0:
            time.sleep(delay_s)
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except Exception:
            continue


def _target_version_dir_for_cursor_agent(*, versions_dir: Path, cursor_agent_path: str) -> Optional[Path]:
    agent_path = Path(cursor_agent_path).expanduser()
    candidates = [agent_path]
    try:
        candidates.append(agent_path.resolve())
    except Exception:
        pass
    for candidate in candidates:
        parent = candidate.parent
        if parent != versions_dir and _path_is_within(parent, versions_dir):
            return parent
    latest = latest_cursor_agent_executable_in_versions_dir(versions_dir)
    if not latest:
        return None
    return Path(latest).parent


def _relevant_patch_errors(*, versions_dir: Path, cursor_agent_path: str, rep: object) -> List[Tuple[Path, str]]:
    errors = list(getattr(rep, "errors", []) or [])
    if not errors:
        return []
    target_version_dir = _target_version_dir_for_cursor_agent(
        versions_dir=versions_dir,
        cursor_agent_path=cursor_agent_path,
    )
    if target_version_dir is None:
        return errors
    relevant: List[Tuple[Path, str]] = []
    for path, message in errors:
        if path == versions_dir:
            relevant.append((path, message))
            continue
        if _path_is_within(path, versions_dir) and not _path_is_within(path, target_version_dir):
            continue
        relevant.append((path, message))
    return relevant


def _verify_patched_cursor_agent_launch(
    *,
    versions_dir: Path,
    cursor_agent_path: str,
    agent_dirs: Optional[CursorAgentDirs] = None,
) -> None:
    from cursor_cli_manager.opening import run_cursor_agent_launch_smoke

    _verify_cursor_agent_command(cursor_agent_path, timeout_s=5.0)
    # Keep the smoke workspace outside the install root. On Windows, child
    # shells can briefly retain their working directory after shutdown, and
    # leaving the smoke tree under install_root can make outer temp cleanup fail.
    smoke_root = Path(tempfile.mkdtemp(prefix=".ccm-launch-smoke-"))
    try:
        workspace = smoke_root / "workspace"
        config_dir = smoke_root / "cursor-config"
        workspace.mkdir(parents=True, exist_ok=True)
        config_dir.mkdir(parents=True, exist_ok=True)
        smoke = run_cursor_agent_launch_smoke(
            workspace_path=workspace,
            cursor_agent_path=cursor_agent_path,
            agent_dirs=agent_dirs,
            cursor_agent_config_dir=config_dir,
        )
    finally:
        _best_effort_rmtree(smoke_root)
    if smoke.ok and smoke.launch_sustained:
        return
    detail = (smoke.output or "").strip()
    detail_lc = detail.lower()
    suffix = f": {detail}" if detail else ""
    if (
        (not smoke.launch_sustained)
        and detail
        and (
            "authentication required" in detail_lc
            or "agent login" in detail_lc
            or "cursor_api_key" in detail_lc
        )
    ):
        return
    if not smoke.launch_sustained:
        raise RuntimeError(
            f"patched cursor-agent exited before launch verification completed (exit {smoke.exit_code}, elapsed {smoke.elapsed_s:.2f}s){suffix}"
        )
    raise RuntimeError(
        f"patched cursor-agent failed launch verification (exit {smoke.exit_code}, elapsed {smoke.elapsed_s:.2f}s){suffix}"
    )


def _rollback_patched_cursor_agent(
    *,
    versions_dir: Path,
    rep: object,
    snapshots: Optional[Dict[str, bytes]] = None,
) -> str:
    files = list(getattr(rep, "patched_files", []) or [])
    if not files:
        return ""
    errors: List[Tuple[Path, str]] = []
    restored_any = False
    if snapshots:
        for path in files:
            data = snapshots.get(str(path))
            if data is None:
                errors.append((path, "rollback snapshot missing"))
                continue
            try:
                try:
                    st = path.stat()
                except Exception:
                    st = None
                path.write_bytes(data)
                if st is not None:
                    try:
                        os.chmod(path, st.st_mode)
                    except Exception:
                        pass
                restored_any = True
            except Exception as e:
                errors.append((path, f"rollback failed: {e}"))
        if restored_any:
            try:
                (versions_dir / ".ccm-patch-cache.json").unlink()
            except FileNotFoundError:
                pass
            except Exception as e:
                errors.append((versions_dir, f"failed to clear patch cache: {e}"))
    else:
        errors = rollback_cursor_agent_patch(versions_dir=versions_dir, files=files)
    if errors:
        return "; rollback errors: " + "; ".join(f"{p}: {e}" for p, e in errors[:5])
    return "; patch rolled back"


def apply_verified_cursor_agent_patch(
    *,
    versions_dir: Path,
    cursor_agent_path: str,
    agent_dirs: Optional[CursorAgentDirs] = None,
    force: bool = False,
    require_changes: bool = False,
) -> object:
    snapshots = _snapshot_patch_inputs(versions_dir)
    rep = patch_cursor_agent_models(versions_dir=versions_dir, dry_run=False, force=force)
    changed = bool(getattr(rep, "patched_files", None) or getattr(rep, "repaired_files", None))
    relevant_errors = _relevant_patch_errors(
        versions_dir=versions_dir,
        cursor_agent_path=cursor_agent_path,
        rep=rep,
    )
    if relevant_errors:
        rollback_note = (
            _rollback_patched_cursor_agent(versions_dir=versions_dir, rep=rep, snapshots=snapshots) if changed else ""
        )
        raise RuntimeError(f"cursor-agent patch failed ({_summarize_patch_report(rep)}){rollback_note}")
    if require_changes and not changed:
        raise RuntimeError(f"cursor-agent patch made no changes ({_summarize_patch_report(rep)})")
    if not changed:
        return rep
    try:
        _verify_patched_cursor_agent_launch(
            versions_dir=versions_dir,
            cursor_agent_path=cursor_agent_path,
            agent_dirs=agent_dirs,
        )
        return rep
    except Exception as e:
        rollback_note = _rollback_patched_cursor_agent(versions_dir=versions_dir, rep=rep, snapshots=snapshots)
        try:
            _verify_cursor_agent_command(cursor_agent_path, timeout_s=5.0)
        except Exception as rollback_exc:
            rollback_note += f"; rollback verification failed: {rollback_exc}"
        raise RuntimeError(f"{e}{rollback_note}")


def maybe_apply_postinstall_compat_patch(
    *,
    install_root: Optional[Path] = None,
    cursor_agent_path: Optional[str] = None,
) -> bool:
    if not _should_apply_compat_patch():
        return False
    versions_dir = _versions_dir(install_root or get_cursor_agent_install_root())
    agent_path = cursor_agent_path or str(_latest_installed_executable(install_root or get_cursor_agent_install_root()) or "")
    if not agent_path:
        raise RuntimeError("cursor-agent executable not found for postinstall patch verification")
    rep = apply_verified_cursor_agent_patch(
        versions_dir=versions_dir,
        cursor_agent_path=agent_path,
    )
    return bool(rep.patched_files or rep.repaired_files)


@contextmanager
def _install_lock(*, install_root: Path, wait_s: float = 0.0) -> "object":
    root = install_root.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    lock_dir = root / _INSTALL_LOCK_DIRNAME
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        try:
            lock_dir.mkdir(mode=0o700)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Cursor Agent install already in progress (lock: {lock_dir})")
            time.sleep(0.1)
    try:
        yield object()
    finally:
        try:
            shutil.rmtree(lock_dir)
        except Exception:
            pass


def install_cursor_agent_from_spec(
    spec: CursorAgentInstallSpec,
    *,
    timeout_s: Optional[float] = None,
    fetch: Fetch = _default_fetch,
) -> CursorAgentInstallResult:
    install_root = spec.install_root.expanduser()
    versions_dir = _versions_dir(install_root)
    versions_dir.mkdir(parents=True, exist_ok=True)
    notes: List[str] = []
    timeout = timeout_s or get_cursor_agent_install_timeout_s()

    with _install_lock(install_root=install_root, wait_s=min(DEFAULT_INSTALL_LOCK_WAIT_S, timeout)):
        repaired = False
        for candidate in _bin_candidates(spec.bin_dir):
            if _path_exists_file(candidate):
                return CursorAgentInstallResult(
                    installed_path=str(candidate),
                    version=spec.version,
                    performed_download=False,
                    repaired_launchers=False,
                    applied_compat_patch=False,
                    notes=("existing installation already available",),
                )
        if _latest_installed_executable(install_root) is not None:
            repaired, installed_path = _repair_launchers(spec)
            _verify_cursor_agent_command(installed_path, timeout_s=min(5.0, timeout))
            applied_patch = maybe_apply_postinstall_compat_patch(
                install_root=install_root,
                cursor_agent_path=installed_path,
            )
            return CursorAgentInstallResult(
                installed_path=installed_path,
                version=spec.version,
                performed_download=False,
                repaired_launchers=repaired,
                applied_compat_patch=applied_patch,
                notes=("repaired existing launchers",),
            )

        data = fetch(spec.download_url, timeout, {"User-Agent": "cursor-cli-manager"})
        staging_root = Path(tempfile.mkdtemp(prefix=f".cursor-agent-{spec.version}-", dir=str(versions_dir)))
        try:
            if spec.archive_kind == "tar.gz":
                _safe_extract_tar_gz(data, dest_dir=staging_root)
            elif spec.archive_kind == "zip":
                _safe_extract_zip(data, dest_dir=staging_root)
            else:
                raise RuntimeError(f"unsupported Cursor Agent archive kind: {spec.archive_kind}")

            payload_dir = _payload_dir_from_extract(staging_root)
            _validate_payload_dir(payload_dir, system=spec.system)
            exe_name = "cursor-agent.cmd" if spec.system == "windows" else "cursor-agent"
            target_exe = payload_dir / exe_name
            try:
                st = target_exe.stat()
                os.chmod(str(target_exe), st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except Exception:
                pass

            version_dir = versions_dir / spec.version
            if version_dir.exists():
                try:
                    shutil.rmtree(version_dir)
                except Exception as e:
                    raise RuntimeError(f"failed to replace existing Cursor Agent version dir {version_dir}: {e}")
            os.replace(str(payload_dir), str(version_dir))

            installed_path = _install_launchers(spec, target=version_dir / exe_name)
            _verify_cursor_agent_command(installed_path, timeout_s=min(5.0, timeout))
            applied_patch = maybe_apply_postinstall_compat_patch(
                install_root=install_root,
                cursor_agent_path=installed_path,
            )
            notes.append(f"installed {spec.version}")
            return CursorAgentInstallResult(
                installed_path=installed_path,
                version=spec.version,
                performed_download=True,
                repaired_launchers=False,
                applied_compat_patch=applied_patch,
                notes=tuple(notes),
            )
        finally:
            if staging_root.exists():
                try:
                    shutil.rmtree(staging_root)
                except Exception:
                    pass


def ensure_cursor_agent_available(
    *,
    explicit: Optional[str] = None,
    auto_install: bool = True,
    timeout_s: Optional[float] = None,
    fetch: Fetch = _default_fetch,
) -> str:
    resolved = resolve_cursor_agent_installation(explicit=explicit)
    if resolved.path:
        return resolved.path
    if resolved.is_explicit_override:
        raise RuntimeError(f"{resolved.error}. Fix the override and retry.")
    if not auto_install or not auto_install_enabled():
        msg = resolved.error or "cursor-agent not found"
        raise RuntimeError(f"{msg}. {manual_install_hint()}")

    try:
        metadata = fetch_official_installer_metadata(timeout_s=timeout_s, fetch=fetch)
        spec = select_cursor_agent_install_spec(metadata)
        result = install_cursor_agent_from_spec(spec, timeout_s=timeout_s, fetch=fetch)
        return result.installed_path
    except Exception as e:
        raise RuntimeError(
            f"automatic Cursor Agent install failed: {e}. "
            f"Installer source: {get_cursor_agent_installer_url()}. {manual_install_hint()}"
        )
