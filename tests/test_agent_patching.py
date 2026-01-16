import os
import tempfile
import unittest
from pathlib import Path

from cursor_cli_manager.agent_patching import (
    ENV_CCM_CURSOR_AGENT_VERSIONS_DIR,
    ENV_CCM_PATCH_CURSOR_AGENT_MODELS,
    ENV_CURSOR_AGENT_VERSIONS_DIR,
    patch_cursor_agent_models,
    resolve_cursor_agent_versions_dir,
    should_patch_models,
)


SAMPLE_JS = """
var __awaiter = (this && this.__awaiter) || function () {};

function fetchModelData(aiServerClient) {
    return __awaiter(this, void 0, void 0, function* () {
        const [modelsResult, defaultResult] = yield Promise.allSettled([
            fetchUsableModels(aiServerClient),
            fetchDefaultModel(aiServerClient),
        ]);
        return {};
    });
}

function fetchUsableModels(aiServerClient) {
    return __awaiter(this, void 0, void 0, function* () {
        const { models } = yield aiServerClient.getUsableModels(new KD({}));
        return models.length > 0 ? models : undefined;
    });
}
function fetchDefaultModel(aiServerClient) {
    return __awaiter(this, void 0, void 0, function* () {
        return null;
    });
}
"""


class TestAgentPatching(unittest.TestCase):
    def test_should_patch_models_env(self) -> None:
        old = os.environ.get(ENV_CCM_PATCH_CURSOR_AGENT_MODELS)
        try:
            os.environ[ENV_CCM_PATCH_CURSOR_AGENT_MODELS] = "1"
            self.assertTrue(should_patch_models(explicit=None))
            os.environ[ENV_CCM_PATCH_CURSOR_AGENT_MODELS] = "0"
            self.assertFalse(should_patch_models(explicit=None))
        finally:
            if old is None:
                os.environ.pop(ENV_CCM_PATCH_CURSOR_AGENT_MODELS, None)
            else:
                os.environ[ENV_CCM_PATCH_CURSOR_AGENT_MODELS] = old

    def test_resolve_versions_dir_explicit_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            versions = Path(td) / "versions"
            versions.mkdir(parents=True, exist_ok=True)
            self.assertEqual(resolve_cursor_agent_versions_dir(explicit=str(versions)), versions)

            old1 = os.environ.get(ENV_CCM_CURSOR_AGENT_VERSIONS_DIR)
            old2 = os.environ.get(ENV_CURSOR_AGENT_VERSIONS_DIR)
            try:
                os.environ[ENV_CCM_CURSOR_AGENT_VERSIONS_DIR] = str(versions)
                self.assertEqual(resolve_cursor_agent_versions_dir(explicit=None), versions)
                os.environ.pop(ENV_CCM_CURSOR_AGENT_VERSIONS_DIR, None)
                os.environ[ENV_CURSOR_AGENT_VERSIONS_DIR] = str(versions)
                self.assertEqual(resolve_cursor_agent_versions_dir(explicit=None), versions)
            finally:
                if old1 is None:
                    os.environ.pop(ENV_CCM_CURSOR_AGENT_VERSIONS_DIR, None)
                else:
                    os.environ[ENV_CCM_CURSOR_AGENT_VERSIONS_DIR] = old1
                if old2 is None:
                    os.environ.pop(ENV_CURSOR_AGENT_VERSIONS_DIR, None)
                else:
                    os.environ[ENV_CURSOR_AGENT_VERSIONS_DIR] = old2

    def test_resolve_versions_dir_infers_from_cursor_agent_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            versions_dir = root / "somewhere" / "versions"
            v = versions_dir / "2026.01.09-231024f"
            v.mkdir(parents=True, exist_ok=True)
            (v / "cursor-agent").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
            (v / "8658.index.js").write_text(SAMPLE_JS, encoding="utf-8")

            inferred = resolve_cursor_agent_versions_dir(cursor_agent_path=str(v / "cursor-agent"))
            self.assertIsNotNone(inferred)
            # macOS commonly exposes /var as a symlink to /private/var; normalize via resolve().
            self.assertEqual(inferred.resolve(), versions_dir.resolve())

    def test_patch_cursor_agent_models_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            versions_dir = Path(td) / "versions"
            v1 = versions_dir / "2026.01.09-231024f"
            v1.mkdir(parents=True, exist_ok=True)
            js = v1 / "8658.index.js"
            js.write_text(SAMPLE_JS, encoding="utf-8")

            rep1 = patch_cursor_agent_models(versions_dir=versions_dir, dry_run=False)
            self.assertTrue(rep1.ok)
            self.assertEqual(rep1.scanned_files, 1)
            self.assertEqual(len(rep1.patched_files), 1)

            patched = js.read_text(encoding="utf-8")
            self.assertIn("CCM_PATCH_AVAILABLE_MODELS_NORMALIZED", patched)
            self.assertIn("availableModels", patched)
            self.assertIn("supportsAgent === true", patched)
            self.assertIn("displayModelId", patched)
            self.assertIn("getUsableModels", patched)

            bak = js.with_suffix(js.suffix + ".ccm.bak")
            self.assertTrue(bak.exists())
            self.assertIn("getUsableModels", bak.read_text(encoding="utf-8"))

            rep2 = patch_cursor_agent_models(versions_dir=versions_dir, dry_run=False)
            self.assertTrue(rep2.ok)
            self.assertEqual(rep2.scanned_files, 1)
            self.assertEqual(len(rep2.patched_files), 0)
            self.assertEqual(rep2.skipped_already_patched, 1)

    def test_patch_upgrades_v1_marker(self) -> None:
        # Simulate a previously patched file using the old marker.
        v1_patched = SAMPLE_JS.replace(
            "yield aiServerClient.getUsableModels(new KD({}));",
            "yield (aiServerClient.getAvailableModels ? aiServerClient.getAvailableModels(new KD({})) : aiServerClient.getUsableModels(new KD({})));",
        ) + "\n/* CCM_PATCH_AVAILABLE_MODELS */\n"
        with tempfile.TemporaryDirectory() as td:
            versions_dir = Path(td) / "versions"
            vdir = versions_dir / "2026.01.09-231024f"
            vdir.mkdir(parents=True, exist_ok=True)
            js = vdir / "8658.index.js"
            js.write_text(v1_patched, encoding="utf-8")

            rep = patch_cursor_agent_models(versions_dir=versions_dir, dry_run=False)
            self.assertTrue(rep.ok)
            self.assertEqual(len(rep.patched_files), 1)
            new_txt = js.read_text(encoding="utf-8")
            self.assertIn("CCM_PATCH_AVAILABLE_MODELS_NORMALIZED", new_txt)
            self.assertIn("supportsAgent === true", new_txt)

    def test_patch_upgrades_v2_marker(self) -> None:
        # V2 inserted a marker comment between functions; ensure our block matcher still upgrades.
        v2_patched = SAMPLE_JS.replace(
            "yield aiServerClient.getUsableModels(new KD({}));",
            "yield (aiServerClient.availableModels ? aiServerClient.availableModels(new KD({})) : aiServerClient.getUsableModels(new KD({})));",
        ) + "\n/* CCM_PATCH_AVAILABLE_MODELS_V2 */\n"
        with tempfile.TemporaryDirectory() as td:
            versions_dir = Path(td) / "versions"
            vdir = versions_dir / "2026.01.09-231024f"
            vdir.mkdir(parents=True, exist_ok=True)
            js = vdir / "8658.index.js"
            js.write_text(v2_patched, encoding="utf-8")

            rep = patch_cursor_agent_models(versions_dir=versions_dir, dry_run=False)
            self.assertTrue(rep.ok)
            self.assertEqual(len(rep.patched_files), 1)
            new_txt = js.read_text(encoding="utf-8")
            self.assertIn("CCM_PATCH_AVAILABLE_MODELS_NORMALIZED", new_txt)
            self.assertIn("displayModelId", new_txt)


if __name__ == "__main__":
    unittest.main()

