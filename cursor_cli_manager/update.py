from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

import shutil

from cursor_cli_manager import __version__
from cursor_cli_manager.github_release import (
    ENV_CCM_INSTALL_DEST,
    ENV_CCM_INSTALL_ROOT,
    Fetch,
    download_and_install_release_bundle,
    fetch_latest_release,
    get_github_repo,
    get_install_bin_dir,
    get_install_root_dir,
    is_frozen_binary,
    is_version_newer,
    select_release_asset_name,
)


DIST_INFO_GLOB = "cursor_cli_manager-*.dist-info"


@dataclass(frozen=True)
class UpdateStatus:
    supported: bool
    method: Optional[str] = None  # "pep610" | "github_release"
    url: Optional[str] = None
    requested_revision: Optional[str] = None
    installed_commit: Optional[str] = None
    remote_commit: Optional[str] = None
    installed_version: Optional[str] = None
    remote_version: Optional[str] = None
    repo: Optional[str] = None
    asset_name: Optional[str] = None
    update_available: bool = False
    error: Optional[str] = None


Runner = Callable[[Sequence[str], float], Tuple[int, str, str]]


def _default_runner(cmd: Sequence[str], timeout_s: float) -> Tuple[int, str, str]:
    env = dict(os.environ)
    # Prevent git from prompting for credentials (would block the TUI thread).
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    # Reduce noise/latency.
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    p: Optional[subprocess.Popen] = None
    try:
        # Important:
        # - start_new_session=True prevents the child (git/ssh/pip) from inheriting the controlling TTY.
        # - stdin=DEVNULL avoids accidental reads from stdin.
        p = subprocess.Popen(
            list(cmd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        out, err = p.communicate(timeout=timeout_s)
        return p.returncode or 0, out or "", err or ""
    except subprocess.TimeoutExpired:
        # Best-effort: terminate the whole process group so helpers (e.g. ssh) don't linger.
        out = ""
        err = ""
        if p is not None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except Exception:
                pass
            try:
                out, err = p.communicate(timeout=0.2)
            except Exception:
                out, err = "", ""
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            try:
                out2, err2 = p.communicate(timeout=0.2)
                out = (out or "") + (out2 or "")
                err = (err or "") + (err2 or "")
            except Exception:
                pass
        return 124, (out or "").strip(), (err or "timeout").strip() or "timeout"
    except Exception as e:
        return 1, "", str(e)


def _git(args: List[str], *, timeout_s: float, run: Runner) -> Tuple[int, str, str]:
    return run(["git", *args], timeout_s)


@dataclass(frozen=True)
class Pep610InstallInfo:
    url: str
    commit_id: str
    requested_revision: Optional[str] = None
    subdirectory: Optional[str] = None


def _find_direct_url_json(*, package_dir: Optional[Path] = None) -> Optional[Path]:
    """
    Locate PEP 610 `direct_url.json` next to the installed package.

    We intentionally avoid importlib.metadata to keep Python 3.7 support without
    extra dependencies. This only works for normal site-packages installs that
    have a `.dist-info` directory (which is exactly what PEP 610 relies on).
    """
    pkg_dir = package_dir or Path(__file__).resolve().parent
    site = pkg_dir.parent
    try:
        for dist in site.glob(DIST_INFO_GLOB):
            p = dist / "direct_url.json"
            if p.exists():
                return p
    except Exception:
        return None
    return None


def read_pep610_install_info(*, package_dir: Optional[Path] = None) -> Optional[Pep610InstallInfo]:
    p = _find_direct_url_json(package_dir=package_dir)
    if p is None:
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    url = obj.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    vcs = obj.get("vcs_info")
    if not isinstance(vcs, dict):
        return None
    if vcs.get("vcs") not in (None, "git"):
        return None
    commit_id = vcs.get("commit_id")
    if not isinstance(commit_id, str) or not commit_id.strip():
        return None
    requested_revision = vcs.get("requested_revision")
    if not isinstance(requested_revision, str) or not requested_revision.strip():
        requested_revision = None
    subdirectory = obj.get("subdirectory")
    if not isinstance(subdirectory, str) or not subdirectory.strip():
        subdirectory = None
    return Pep610InstallInfo(
        url=url.strip(),
        commit_id=commit_id.strip(),
        requested_revision=requested_revision.strip() if requested_revision else None,
        subdirectory=subdirectory.strip() if subdirectory else None,
    )


def _parse_ls_remote_first_hash(output: str) -> Optional[str]:
    for ln in (output or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        # "<hash>\t<ref>"
        parts = ln.split()
        if parts and len(parts[0]) >= 7:
            return parts[0]
    return None


def _git_ls_remote(url: str, rev: str, *, timeout_s: float, run: Runner) -> Tuple[int, str, str]:
    return _git(["ls-remote", url, rev], timeout_s=timeout_s, run=run)


def build_vcs_requirement(info: Pep610InstallInfo) -> str:
    """
    Build a pip-compatible VCS requirement string from PEP 610 info.
    """
    base = info.url
    if not base.startswith("git+"):
        base = "git+" + base
    rev = info.requested_revision
    if rev:
        base = base + "@" + rev
    if info.subdirectory:
        # Preserve existing fragment if present.
        if "#" in base:
            base = base + "&subdirectory=" + info.subdirectory
        else:
            base = base + "#subdirectory=" + info.subdirectory
    return base


def check_for_update(
    *,
    package_dir: Optional[Path] = None,
    timeout_s: float = 2.0,
    run: Runner = _default_runner,
    fetch: Optional[Fetch] = None,
) -> UpdateStatus:
    """
    Best-effort, non-blocking-friendly update check.

    Methods:
    - Frozen binary: compare current version against GitHub latest release tag.
    - PEP 610 VCS install: compare installed commit_id against remote tip of requested revision.
    """
    if is_frozen_binary():
        repo = get_github_repo()
        try:
            if fetch is None:
                rel = fetch_latest_release(repo, timeout_s=timeout_s)
            else:
                rel = fetch_latest_release(repo, timeout_s=timeout_s, fetch=fetch)
        except Exception as e:
            return UpdateStatus(
                supported=False,
                method="github_release",
                repo=repo,
                installed_version=__version__,
                error=str(e),
            )
        try:
            asset_name = select_release_asset_name()
        except Exception as e:
            return UpdateStatus(
                supported=False,
                method="github_release",
                repo=repo,
                installed_version=__version__,
                remote_version=rel.version,
                error=str(e),
            )

        newer = is_version_newer(rel.version, __version__)
        return UpdateStatus(
            supported=(newer is not None),
            method="github_release",
            repo=repo,
            installed_version=__version__,
            remote_version=rel.version,
            asset_name=asset_name,
            update_available=bool(newer),
            error=None if newer is not None else "failed to parse version",
        )

    info = read_pep610_install_info(package_dir=package_dir)
    if info is None:
        return UpdateStatus(supported=False, error="not a PEP 610 VCS install")

    # Determine which remote ref to compare against.
    rev = info.requested_revision or "HEAD"
    code, out, err = _git_ls_remote(info.url, rev, timeout_s=timeout_s, run=run)
    if code != 0:
        return UpdateStatus(
            supported=False,
            method="pep610",
            url=info.url,
            requested_revision=info.requested_revision,
            installed_commit=info.commit_id,
            error=(err or "ls-remote failed").strip() or "ls-remote failed",
        )
    remote = _parse_ls_remote_first_hash(out)
    if not remote:
        return UpdateStatus(
            supported=False,
            method="pep610",
            url=info.url,
            requested_revision=info.requested_revision,
            installed_commit=info.commit_id,
            error="failed to parse ls-remote output",
        )

    update_available = remote != info.commit_id
    return UpdateStatus(
        supported=True,
        method="pep610",
        url=info.url,
        requested_revision=info.requested_revision,
        installed_commit=info.commit_id,
        remote_commit=remote,
        update_available=update_available,
    )


def perform_update(
    *,
    package_dir: Optional[Path] = None,
    python: str = "python",
    timeout_s: float = 120.0,
    run: Runner = _default_runner,
    fetch: Optional[Fetch] = None,
) -> Tuple[bool, str]:
    """
    Perform an in-place upgrade.

    Methods:
    - Frozen binary: download latest GitHub release asset and replace current executable.
    - PEP 610 VCS install: use pip to upgrade from the recorded VCS URL.

    Returns (ok, combined_output). If this isn't a PEP 610 VCS install, returns
    (False, reason).
    """
    if is_frozen_binary():
        repo = get_github_repo()
        try:
            if fetch is None:
                rel = fetch_latest_release(repo, timeout_s=timeout_s)
            else:
                rel = fetch_latest_release(repo, timeout_s=timeout_s, fetch=fetch)
            asset_name = select_release_asset_name()

            # Determine install dirs (env override > infer from PATH/executable > defaults).
            bin_dir = get_install_bin_dir()
            root_dir = get_install_root_dir()
            # Respect explicit overrides (do not "auto-detect" over them).
            env_dest = os.environ.get(ENV_CCM_INSTALL_DEST)
            env_root = os.environ.get(ENV_CCM_INSTALL_ROOT)

            if not (isinstance(env_dest, str) and env_dest.strip()):
                try:
                    which = shutil.which("ccm")
                    if which:
                        # IMPORTANT: do NOT resolve here. If "ccm" is a symlink into the
                        # onedir bundle, resolve() would move bin_dir into the bundle and
                        # we could overwrite the running executable (potentially creating
                        # a symlink loop).
                        bin_dir = Path(which).expanduser().parent
                except Exception:
                    pass

            if not (isinstance(env_root, str) and env_root.strip()):
                try:
                    # Only infer from sys.executable if it matches our onedir layout:
                    #   <root>/versions/<tag>/ccm/ccm  OR  <root>/current/ccm/ccm
                    exe = Path(sys.executable).resolve()
                    if exe.name == "ccm" and exe.parent.name == "ccm":
                        if exe.parent.parent.name == "current":
                            root_dir = exe.parent.parent.parent
                        elif exe.parent.parent.parent.name == "versions":
                            root_dir = exe.parent.parent.parent.parent
                except Exception:
                    pass

            if fetch is None:
                download_and_install_release_bundle(
                    repo=repo,
                    tag=rel.tag,
                    asset_name=asset_name,
                    install_root=root_dir,
                    bin_dir=bin_dir,
                    timeout_s=timeout_s,
                    verify_checksums=True,
                )
            else:
                download_and_install_release_bundle(
                    repo=repo,
                    tag=rel.tag,
                    asset_name=asset_name,
                    install_root=root_dir,
                    bin_dir=bin_dir,
                    timeout_s=timeout_s,
                    fetch=fetch,
                    verify_checksums=True,
                )
            return True, f"updated to {rel.version}"
        except Exception as e:
            return False, str(e)

    info = read_pep610_install_info(package_dir=package_dir)
    if info is None:
        return False, "not a PEP 610 VCS install"

    req = build_vcs_requirement(info)
    cmd = [python, "-m", "pip", "install", "--upgrade", "--no-deps", "--force-reinstall", req]
    code, out, err = run(cmd, timeout_s)
    txt = ((out or "") + ("\n" if out and err else "") + (err or "")).strip()
    return (code == 0), txt

