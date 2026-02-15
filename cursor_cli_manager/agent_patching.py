from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cursor_cli_manager.agent_paths import CursorAgentDirs, get_cursor_agent_dirs
from cursor_cli_manager.ccm_config import has_legacy_install


ENV_CCM_PATCH_CURSOR_AGENT_MODELS = "CCM_PATCH_CURSOR_AGENT_MODELS"
ENV_CCM_CURSOR_AGENT_VERSIONS_DIR = "CCM_CURSOR_AGENT_VERSIONS_DIR"
ENV_CURSOR_AGENT_VERSIONS_DIR = "CURSOR_AGENT_VERSIONS_DIR"

_PATCH_MARKER = "CCM_PATCH_AVAILABLE_MODELS_NORMALIZED"
_PATCH_SIGNATURE = "CCM_PATCH_MODELDETAILS_ONLY"
_PATCH_AUTORUN_MARKER = "CCM_PATCH_AUTORUN_CONTROLS_DISABLED"
_PATCH_CACHE_FILENAME = ".ccm-patch-cache.json"
_PATCH_CACHE_VERSION = 1
_PATCH_AUTORUN_V2 = "promise"  # bumped to invalidate cache after .catch fix
_PATCH_CACHE_SIGNATURE = "|".join([_PATCH_MARKER, _PATCH_SIGNATURE, _PATCH_AUTORUN_MARKER, _PATCH_AUTORUN_V2])
_PATCH_CACHE_STATUS_PATCHED = "already_patched"
_PATCH_CACHE_STATUS_NOT_APPLICABLE = "not_applicable"


def _is_truthy(v: Optional[str]) -> bool:
    if v is None:
        return False
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def should_patch_models(
    *,
    agent_dirs: Optional[CursorAgentDirs] = None,
    explicit: Optional[bool] = None,
) -> bool:
    """
    Decide whether to patch cursor-agent bundles (model enumeration + autoRunControls).

    Priority:
    - legacy gate (must be enabled)
    - explicit arg (if not None)
    - $CCM_PATCH_CURSOR_AGENT_MODELS (if set; truthy/falsey)
    - default: True
    """
    dirs = agent_dirs or get_cursor_agent_dirs()
    if not has_legacy_install(dirs):
        return False
    if explicit is not None:
        return bool(explicit)
    env = os.environ.get(ENV_CCM_PATCH_CURSOR_AGENT_MODELS)
    if env is not None:
        return _is_truthy(env)
    return True


def resolve_cursor_agent_versions_dir(
    *,
    explicit: Optional[str] = None,
    cursor_agent_path: Optional[str] = None,
) -> Optional[Path]:
    """
    Locate the cursor-agent `versions/` directory.

    Priority:
    - explicit arg
    - $CCM_CURSOR_AGENT_VERSIONS_DIR / $CURSOR_AGENT_VERSIONS_DIR
    - infer from `cursor-agent` executable location (no hard-coded paths)
    """
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() and p.is_dir() else None

    for k in (ENV_CCM_CURSOR_AGENT_VERSIONS_DIR, ENV_CURSOR_AGENT_VERSIONS_DIR):
        v = os.environ.get(k)
        if v:
            p = Path(v).expanduser()
            if p.exists() and p.is_dir():
                return p

    # Best-effort inference from the installed cursor-agent executable.
    agent = cursor_agent_path
    if not agent:
        try:
            # Local import to avoid import-time coupling.
            from cursor_cli_manager.opening import resolve_cursor_agent_path  # type: ignore

            agent = resolve_cursor_agent_path()
        except Exception:
            agent = None

    if agent:
        inferred = _infer_versions_dir_from_cursor_agent_executable(agent)
        if inferred is not None:
            return inferred
    return None


def _infer_versions_dir_from_cursor_agent_executable(cursor_agent_path: str) -> Optional[Path]:
    """
    Infer the versions directory from a cursor-agent executable path.

    We avoid hard-coded absolute locations and instead look for a directory that:
    - contains subdirectories, and
    - at least one subdirectory looks like a cursor-agent "version dir" (runner + `*.index.js`).
    """
    p = Path(cursor_agent_path).expanduser()
    if not p.exists():
        return None
    try:
        p = p.resolve()
    except Exception:
        # Still usable even if resolve fails.
        pass

    start = p.parent if p.is_file() else p
    for d in [start] + list(start.parents)[:8]:
        if _looks_like_versions_dir(d):
            return d
        candidate = d / "versions"
        if _looks_like_versions_dir(candidate):
            return candidate
    return None


def _looks_like_version_subdir(vdir: Path) -> bool:
    if not vdir.exists() or not vdir.is_dir():
        return False
    try:
        if not any(vdir.glob("*.index.js")):
            return False
        if (vdir / "cursor-agent").exists():
            return True
        if sys.platform.startswith("win"):
            if (vdir / "cursor-agent.exe").exists():
                return True
            if (vdir / "node.exe").exists() or (vdir / "index.js").exists():
                return True
    except Exception:
        return False
    return False


def _looks_like_versions_dir(d: Path) -> bool:
    if not d.exists() or not d.is_dir():
        return False
    try:
        children = [p for p in d.iterdir() if p.is_dir()]
    except Exception:
        return False
    # Heuristic: a versions dir should contain at least one "version dir" with expected files.
    for vdir in children[:200]:
        if _looks_like_version_subdir(vdir):
            return True
    return False


def _patch_cache_path(versions_dir: Path) -> Path:
    return versions_dir / _PATCH_CACHE_FILENAME


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _coerce_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _cache_key(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except Exception:
        return path.as_posix()


def _cache_stat_values(st: os.stat_result) -> Tuple[int, int]:
    mtime_ns = getattr(st, "st_mtime_ns", None)
    if not isinstance(mtime_ns, int):
        mtime_ns = int(st.st_mtime * 1_000_000_000)
    return int(mtime_ns), int(st.st_size)


def _cache_entry_from_stat(status: str, st: os.stat_result) -> Dict[str, Any]:
    mtime_ns, size = _cache_stat_values(st)
    return {"mtime_ns": mtime_ns, "size": size, "status": status}


def _cache_entry_matches(entry: Dict[str, Any], st: os.stat_result) -> bool:
    mtime_ns, size = _cache_stat_values(st)
    return entry.get("mtime_ns") == mtime_ns and entry.get("size") == size


def _load_patch_cache(versions_dir: Path) -> Optional[Dict[str, Dict[str, Any]]]:
    p = _patch_cache_path(versions_dir)
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("version") != _PATCH_CACHE_VERSION:
        return None
    if obj.get("signature") != _PATCH_CACHE_SIGNATURE:
        return None
    files = obj.get("files")
    if not isinstance(files, dict):
        return None
    out: Dict[str, Dict[str, Any]] = {}
    for k, v in files.items():
        if not isinstance(k, str) or not isinstance(v, dict):
            continue
        mtime_ns = _coerce_int(v.get("mtime_ns"))
        size = _coerce_int(v.get("size"))
        status = v.get("status")
        if mtime_ns is None or size is None:
            continue
        if status == "patched":
            status = _PATCH_CACHE_STATUS_PATCHED
        if status not in (_PATCH_CACHE_STATUS_PATCHED, _PATCH_CACHE_STATUS_NOT_APPLICABLE):
            continue
        out[k] = {"mtime_ns": mtime_ns, "size": size, "status": status}
    return out


def _save_patch_cache(versions_dir: Path, files: Dict[str, Dict[str, Any]]) -> None:
    payload = {"version": _PATCH_CACHE_VERSION, "signature": _PATCH_CACHE_SIGNATURE, "files": files}
    _atomic_write_json(_patch_cache_path(versions_dir), payload)


@dataclass
class PatchReport:
    versions_dir: Path
    scanned_files: int = 0
    patched_files: List[Path] = field(default_factory=list)
    skipped_already_patched: int = 0
    skipped_not_applicable: int = 0
    skipped_cached: int = 0
    errors: List[Tuple[Path, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


_RE_FETCH_USABLE_BLOCK = re.compile(
    r"function\s+fetchUsableModels\(aiServerClient\)\s*\{[\s\S]*?\}\s*(?=(?:\s|/\*[\s\S]*?\*/)*function\s+fetchDefaultModel)",
    flags=re.MULTILINE,
)

_RE_AUTORUN_CONTROLS_ANY = re.compile(r"\bconst\s+autoRunControls\b")
_RE_AUTORUN_CONTROLS_ASSIGN = re.compile(
    r"const\s+autoRunControls\b\s*=(?!\s*null\b)\s*[^;\n\r]*(?=;|\r?\n|$)"
)
# cursor cli 2026.1.13+: auto-run checks inline via getAutoRunControls()
_RE_AUTORUN_CONTROLS_CALL = re.compile(
    r"\b[\w$\.]+(?:\?\.|\.)getAutoRunControls\s*\(\s*\)"
)


def _patch_auto_run_controls(txt: str) -> Tuple[str, int]:
    """
    Replace any single-line `const autoRunControls = ...` assignment with `const autoRunControls = null`.
    Also force getAutoRunControls() calls to return a permissive object.

    This is best-effort and intentionally avoids touching lines already set to `null`.
    """
    out, n_assign = _RE_AUTORUN_CONTROLS_ASSIGN.subn("const autoRunControls = null", txt)
    replacement = "Promise.resolve({ enabled: false })/* " + _PATCH_AUTORUN_MARKER + " */"
    out, n_call = _RE_AUTORUN_CONTROLS_CALL.subn(replacement, out)

    # Upgrade old patches that returned a plain object instead of a Promise.
    # The old replacement broke code like `getAutoRunControls().catch(...)`.
    _OLD_BROKEN = "({ enabled: false })/* " + _PATCH_AUTORUN_MARKER + " */"
    n_upgrade = out.count(_OLD_BROKEN)
    if n_upgrade:
        out = out.replace(_OLD_BROKEN, replacement)

    return out, n_assign + n_call + n_upgrade

def _extract_call_arg(block: str) -> Optional[str]:
    """
    Best-effort extraction of the first argument passed to an aiServerClient.*Models(...) call.

    We prefer parsing over regex so we can also upgrade already-patched blocks.
    """
    m = re.search(r"aiServerClient\.(?:getUsableModels|getAvailableModels|availableModels)\s*\(", block)
    if not m:
        return None
    i = m.end()  # position after "("
    depth = 1
    in_s = False
    in_d = False
    in_b = False
    esc = False
    while i < len(block):
        ch = block[i]
        if esc:
            esc = False
            i += 1
            continue
        if ch == "\\":
            esc = True
            i += 1
            continue
        if in_s:
            if ch == "'":
                in_s = False
            i += 1
            continue
        if in_d:
            if ch == '"':
                in_d = False
            i += 1
            continue
        if in_b:
            if ch == "`":
                in_b = False
            i += 1
            continue
        # not in string
        if ch == "'":
            in_s = True
            i += 1
            continue
        if ch == '"':
            in_d = True
            i += 1
            continue
        if ch == "`":
            in_b = True
            i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                arg = block[m.end() : i].strip()
                return arg or None
            i += 1
            continue
        i += 1
    return None


def _patch_fetch_usable_models_block(block: str) -> Optional[str]:
    """
    Return a patched replacement block, or None if not patchable.
    """
    # Idempotence / upgrade:
    # - If both marker + signature exist, we assume it's already at the current patch level.
    # - If marker exists but signature doesn't, treat as an older patch and allow upgrade.
    if _PATCH_MARKER in block and _PATCH_SIGNATURE in block:
        return None
    if "getUsableModels" not in block and "availableModels" not in block and "getAvailableModels" not in block:
        return None

    # Prefer parsing so we can also upgrade old patched blocks.
    arg = _extract_call_arg(block)
    if not arg:
        return None

    # We intentionally keep the request expression the same:
    # many protobuf "empty" requests serialize to an empty payload, so this
    # often works even if the request class differs between RPCs.
    return (
        "function fetchUsableModels(aiServerClient) {\n"
        "    return __awaiter(this, void 0, void 0, function* () {\n"
        f"        const _ccm_r = yield (aiServerClient.availableModels\n"
        f"            ? aiServerClient.availableModels({arg})\n"
        f"            : aiServerClient.getAvailableModels\n"
        f"                ? aiServerClient.getAvailableModels({arg})\n"
        f"                : aiServerClient.getUsableModels({arg}));\n"
        "        const _ccm_models = (_ccm_r && (_ccm_r.models || _ccm_r.availableModels || _ccm_r.usableModels)) || [];\n"
        "        const _ccm_normalized = _ccm_models\n"
        "            .map(m => {\n"
        "            if (!m)\n"
        "                return null;\n"
        "            if (m.supportsAgent === false || m.supports_agent === false)\n"
        "                return null;\n"
        "            // Normalize shapes across GetUsableModels vs AvailableModels.\n"
        "            const modelId = (m.modelId || m.name || m.serverModelName || m.server_model_name || \"\");\n"
        "            const displayModelId = (m.displayModelId || m.inputboxShortModelName || m.inputbox_short_model_name || m.clientDisplayName || m.client_display_name || m.name || modelId || \"\");\n"
        "            const displayName = (m.displayName || m.display_name || m.clientDisplayName || m.client_display_name || m.name || displayModelId || modelId || \"\");\n"
        "            const displayNameShort = (m.displayNameShort || m.display_name_short || m.inputboxShortModelName || m.inputbox_short_model_name);\n"
        "            if (!modelId || !displayModelId)\n"
        "                return null;\n"
        "            // Return only agent.v1.ModelDetails-compatible keys (avoid persisting unknown keys into config).\n"
        "            const aliases = Array.isArray(m.aliases)\n"
        "                ? m.aliases.filter(a => typeof a === \"string\")\n"
        "                : [];\n"
        "            const out = { modelId, displayModelId, displayName, displayNameShort: (displayNameShort || displayName), aliases };\n"
        "            if (m.maxMode === true || m.max_mode === true)\n"
        "                out.maxMode = true;\n"
        "            if (m.thinkingDetails)\n"
        "                out.thinkingDetails = m.thinkingDetails;\n"
        "            return out;\n"
        "        })\n"
        "            .filter(Boolean);\n"
        "        const models = _ccm_normalized;\n"
        "        return models.length > 0 ? models : undefined;\n"
        "    });\n"
        "}\n"
        f"/* {_PATCH_MARKER} {_PATCH_SIGNATURE} */\n"
    )


def patch_cursor_agent_models(
    *,
    versions_dir: Path,
    dry_run: bool = False,
    force: bool = False,
) -> PatchReport:
    """
    Patch cursor-agent bundles:
    - prefer "AvailableModels" for model enumeration, and
    - disable team auto-run restrictions by forcing `autoRunControls` to null.

    This is a best-effort patch:
    - It only touches files that contain either `fetchUsableModels(aiServerClient)` or `const autoRunControls = ...`.
    - It is idempotent (skips files already patched).
    - It caches scan results to avoid re-reading unchanged files (unless force/dry_run).
    """
    rep = PatchReport(versions_dir=versions_dir)
    cache: Optional[Dict[str, Dict[str, Any]]] = None
    if (not dry_run) and (not force):
        cache = _load_patch_cache(versions_dir)
    cache_files = cache or {}
    new_cache: Optional[Dict[str, Dict[str, Any]]] = {} if not dry_run else None
    try:
        version_dirs = [p for p in versions_dir.iterdir() if p.is_dir()]
    except Exception as e:
        rep.errors.append((versions_dir, f"failed to list versions dir: {e}"))
        return rep

    for vdir in sorted(version_dirs, key=lambda p: p.name):
        try:
            js_files = sorted(vdir.glob("*.index.js"), key=lambda p: p.name)
        except Exception as e:
            rep.errors.append((vdir, f"failed to list js files: {e}"))
            continue
        for p in js_files:
            cache_key = _cache_key(p, versions_dir)
            st: Optional[os.stat_result] = None
            if cache is not None:
                try:
                    st = p.stat()
                except Exception as e:
                    rep.errors.append((p, f"stat failed: {e}"))
                    continue
                cached = cache_files.get(cache_key)
                if isinstance(cached, dict) and _cache_entry_matches(cached, st):
                    rep.skipped_cached += 1
                    status = cached.get("status")
                    if status == _PATCH_CACHE_STATUS_PATCHED:
                        rep.skipped_already_patched += 1
                    elif status == _PATCH_CACHE_STATUS_NOT_APPLICABLE:
                        rep.skipped_not_applicable += 1
                    if new_cache is not None:
                        new_cache[cache_key] = cached
                    continue

            rep.scanned_files += 1
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                rep.errors.append((p, f"read failed: {e}"))
                continue

            new_txt = txt
            changed = False

            # Patch #1: model enumeration (AvailableModels).
            model_file_already_patched = _PATCH_MARKER in new_txt and _PATCH_SIGNATURE in new_txt
            model_match = None if model_file_already_patched else _RE_FETCH_USABLE_BLOCK.search(new_txt)
            model_found = model_file_already_patched or (model_match is not None)
            model_unpatchable = False
            model_already_patched = model_file_already_patched
            if model_match is not None:
                old_block = model_match.group(0)
                new_block = _patch_fetch_usable_models_block(old_block)
                if new_block:
                    candidate = new_txt[: model_match.start()] + new_block + new_txt[model_match.end() :]
                    if candidate != new_txt:
                        new_txt = candidate
                        changed = True
                else:
                    model_unpatchable = True

            # Patch #2: autoRunControls -> null.
            auto_found = (
                _RE_AUTORUN_CONTROLS_ANY.search(new_txt) is not None
                or _RE_AUTORUN_CONTROLS_CALL.search(new_txt) is not None
                or _PATCH_AUTORUN_MARKER in new_txt
            )
            if auto_found:
                new_txt2, n_auto = _patch_auto_run_controls(new_txt)
                if n_auto and new_txt2 != new_txt:
                    new_txt = new_txt2
                    changed = True

            if not changed:
                status = _PATCH_CACHE_STATUS_NOT_APPLICABLE
                if auto_found or model_already_patched:
                    rep.skipped_already_patched += 1
                    status = _PATCH_CACHE_STATUS_PATCHED
                elif model_found and model_unpatchable:
                    rep.skipped_not_applicable += 1
                else:
                    rep.skipped_not_applicable += 1
                if new_cache is not None:
                    if st is None:
                        try:
                            st = p.stat()
                        except Exception:
                            st = None
                    if st is not None:
                        new_cache[cache_key] = _cache_entry_from_stat(status, st)
                continue

            if dry_run:
                rep.patched_files.append(p)
                continue

            if st is None:
                try:
                    st = p.stat()
                except Exception:
                    st = None

            # Best-effort backup once.
            bak = p.with_suffix(p.suffix + ".ccm.bak")
            try:
                if not bak.exists():
                    bak.write_text(txt, encoding="utf-8")
            except Exception:
                # Backup failure should not prevent patching.
                pass

            try:
                st = p.stat()
            except Exception:
                st = None

            try:
                p.write_text(new_txt, encoding="utf-8")
                if st is not None:
                    try:
                        os.chmod(p, st.st_mode)
                    except Exception:
                        pass
                rep.patched_files.append(p)
                if new_cache is not None:
                    try:
                        st_after = p.stat()
                    except Exception:
                        st_after = None
                    if st_after is not None:
                        new_cache[cache_key] = _cache_entry_from_stat(_PATCH_CACHE_STATUS_PATCHED, st_after)
            except Exception as e:
                rep.errors.append((p, f"write failed: {e}"))
                continue

    if new_cache is not None:
        try:
            _save_patch_cache(versions_dir, new_cache)
        except Exception:
            pass
    return rep

