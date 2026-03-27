from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence
from urllib.error import HTTPError, URLError

from cursor_cli_manager.agent_paths import CursorAgentDirs
from cursor_cli_manager.cursor_agent_install import (
    CursorAgentInstallResult,
    CursorAgentInstallSpec,
    InstallerMetadata,
    apply_verified_cursor_agent_patch,
    fetch_official_installer_metadata,
    install_cursor_agent_from_spec,
    select_cursor_agent_install_spec,
)


DEFAULT_CANARY_RETRY_DELAYS_S = (15.0, 30.0, 60.0)
_RETRYABLE_HTTP_STATUS_CODES = frozenset({403, 404, 408, 425, 429, 500, 502, 503, 504})

LogFn = Callable[[str], None]
SleepFn = Callable[[float], None]


def _print_install_summary(result: CursorAgentInstallResult, *, log: LogFn) -> None:
    log(f"installed_path={result.installed_path}")
    log(f"version={result.version}")
    log(f"performed_download={result.performed_download}")
    log(f"repaired_launchers={result.repaired_launchers}")
    if result.notes:
        log("notes=" + " | ".join(result.notes))


def _iter_exception_chain(exc: BaseException) -> Iterator[BaseException]:
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_retryable_canary_error(exc: BaseException) -> bool:
    for current in _iter_exception_chain(exc):
        if isinstance(current, HTTPError):
            return current.code in _RETRYABLE_HTTP_STATUS_CODES
        if isinstance(current, (URLError, TimeoutError, ConnectionError, socket.timeout)):
            return True
    return False


def _describe_canary_error(exc: BaseException) -> str:
    for current in _iter_exception_chain(exc):
        if isinstance(current, HTTPError):
            reason = str(current.reason or "").strip()
            if reason:
                return f"HTTP {current.code} {reason}"
            return f"HTTP {current.code}"
        if isinstance(current, URLError):
            reason = current.reason
            if reason:
                return f"{type(reason).__name__}: {reason}"
            return "URLError"
        if isinstance(current, socket.timeout):
            return f"{type(current).__name__}: {current}"
        if isinstance(current, (TimeoutError, ConnectionError)):
            return f"{type(current).__name__}: {current}"
    return f"{type(exc).__name__}: {exc}"


def run_cursor_agent_patch_canary(
    *,
    install_root: Path,
    bin_dir: Path,
    agent_dirs: CursorAgentDirs,
    timeout_s: Optional[float] = None,
    retry_delays_s: Sequence[float] = DEFAULT_CANARY_RETRY_DELAYS_S,
    sleep: SleepFn = time.sleep,
    log: LogFn = print,
    fetch_metadata: Callable[..., InstallerMetadata] = fetch_official_installer_metadata,
    select_spec: Callable[..., CursorAgentInstallSpec] = select_cursor_agent_install_spec,
    install_from_spec: Callable[..., CursorAgentInstallResult] = install_cursor_agent_from_spec,
    apply_patch: Callable[..., object] = apply_verified_cursor_agent_patch,
) -> CursorAgentInstallResult:
    retry_delays = tuple(retry_delays_s)
    total_attempts = 1 + len(retry_delays)

    for attempt in range(1, total_attempts + 1):
        log(f"attempt={attempt}/{total_attempts}")
        try:
            meta = fetch_metadata(timeout_s=timeout_s)
            spec = select_spec(
                meta,
                install_root=install_root,
                bin_dir=bin_dir,
            )
            log(f"upstream_version={meta.version}")
            log(f"download_url={spec.download_url}")
            result = install_from_spec(spec, timeout_s=timeout_s)
            _print_install_summary(result, log=log)
            rep = apply_patch(
                versions_dir=install_root / "versions",
                cursor_agent_path=result.installed_path,
                agent_dirs=agent_dirs,
                force=True,
                require_changes=True,
            )
            log(
                "verified_patch="
                f"patched:{len(rep.patched_files)} repaired:{len(rep.repaired_files)} "
                f"already_patched:{rep.skipped_already_patched} not_applicable:{rep.skipped_not_applicable}"
            )
            return result
        except Exception as exc:
            if attempt > len(retry_delays) or not is_retryable_canary_error(exc):
                raise
            delay_s = retry_delays[attempt - 1]
            log(f"transient_error={_describe_canary_error(exc)}")
            log(f"retrying_in_s={delay_s:g}")
            sleep(delay_s)

    raise RuntimeError("cursor-agent canary exhausted retries unexpectedly")
