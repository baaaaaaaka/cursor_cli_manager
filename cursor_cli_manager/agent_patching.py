from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


ENV_CCM_PATCH_CURSOR_AGENT_MODELS = "CCM_PATCH_CURSOR_AGENT_MODELS"
ENV_CCM_CURSOR_AGENT_VERSIONS_DIR = "CCM_CURSOR_AGENT_VERSIONS_DIR"
ENV_CURSOR_AGENT_VERSIONS_DIR = "CURSOR_AGENT_VERSIONS_DIR"

_PATCH_MARKER = "CCM_PATCH_AVAILABLE_MODELS_NORMALIZED"
_PATCH_SIGNATURE = "CCM_PATCH_MODELDETAILS_ONLY"


def _is_truthy(v: Optional[str]) -> bool:
    if v is None:
        return False
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def should_patch_models(*, explicit: Optional[bool] = None) -> bool:
    """
    Decide whether to patch cursor-agent model enumeration.

    Priority:
    - explicit arg (if not None)
    - $CCM_PATCH_CURSOR_AGENT_MODELS (if set; truthy/falsey)
    - default: True
    """
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
    - at least one subdirectory looks like a cursor-agent "version dir" (has `cursor-agent` + `*.index.js`).
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
    return None


def _looks_like_versions_dir(d: Path) -> bool:
    if not d.exists() or not d.is_dir():
        return False
    try:
        children = [p for p in d.iterdir() if p.is_dir()]
    except Exception:
        return False
    # Heuristic: a versions dir should contain at least one "version dir" with expected files.
    for vdir in children[:200]:
        try:
            if not vdir.is_dir():
                continue
            # Must have the runner and at least one webpack chunk.
            if not (vdir / "cursor-agent").exists():
                continue
            if any(vdir.glob("*.index.js")):
                return True
        except Exception:
            continue
    return False


@dataclass
class PatchReport:
    versions_dir: Path
    scanned_files: int = 0
    patched_files: List[Path] = field(default_factory=list)
    skipped_already_patched: int = 0
    skipped_not_applicable: int = 0
    errors: List[Tuple[Path, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


_RE_FETCH_USABLE_BLOCK = re.compile(
    r"function\s+fetchUsableModels\(aiServerClient\)\s*\{[\s\S]*?\}\s*(?=(?:\s|/\*[\s\S]*?\*/)*function\s+fetchDefaultModel)",
    flags=re.MULTILINE,
)

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
) -> PatchReport:
    """
    Patch cursor-agent bundles so model enumeration prefers "AvailableModels".

    This is a best-effort patch:
    - It only touches files that contain `fetchUsableModels(aiServerClient)`.
    - It is idempotent (skips files already patched).
    """
    rep = PatchReport(versions_dir=versions_dir)
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
            rep.scanned_files += 1
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                rep.errors.append((p, f"read failed: {e}"))
                continue

            # Skip only if we detect the current patch signature; otherwise allow upgrades.
            if _PATCH_MARKER in txt and _PATCH_SIGNATURE in txt:
                rep.skipped_already_patched += 1
                continue

            m = _RE_FETCH_USABLE_BLOCK.search(txt)
            if not m:
                rep.skipped_not_applicable += 1
                continue

            old_block = m.group(0)
            new_block = _patch_fetch_usable_models_block(old_block)
            if not new_block:
                rep.skipped_not_applicable += 1
                continue

            new_txt = txt[: m.start()] + new_block + txt[m.end() :]
            if new_txt == txt:
                rep.skipped_not_applicable += 1
                continue

            if dry_run:
                rep.patched_files.append(p)
                continue

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
            except Exception as e:
                rep.errors.append((p, f"write failed: {e}"))
                continue

    return rep

