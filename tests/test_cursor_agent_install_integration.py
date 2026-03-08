import io
import os
import posixpath
import tarfile
import tempfile
import threading
import unittest
import zipfile
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Callable
from unittest.mock import patch
from urllib.parse import unquote

import cursor_cli_manager.cursor_agent_install as cai


def _make_static_handler(root: Path) -> Callable[..., SimpleHTTPRequestHandler]:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=str(root), **kwargs)

        def translate_path(self, path: str) -> str:
            clean = path.split("?", 1)[0].split("#", 1)[0]
            clean = unquote(clean)
            clean = posixpath.normpath(clean)
            parts = [p for p in clean.split("/") if p and p not in (".", "..")]
            return str(root.joinpath(*parts))

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


class _LocalHttpServer:
    def __init__(self, root: Path) -> None:
        handler = _make_static_handler(root)
        self._httpd = HTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "_LocalHttpServer":
        self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=1)


def _tar_bytes(entries: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, payload in entries.items():
            data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _zip_bytes(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, payload in entries.items():
            data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
            zf.writestr(name, data)
    return buf.getvalue()


class TestCursorAgentInstallIntegration(unittest.TestCase):
    def _write_fixture_tree(self, root: Path, *, system_name: str, arch_name: str) -> str:
        version = "2026.02.27-e7d2ef6"
        installer = root / "install.sh"
        installer.write_text(
            "#!/usr/bin/env bash\n"
            f'DOWNLOAD_URL="https://downloads.cursor.com/lab/{version}/${{OS}}/${{ARCH}}/agent-cli-package.tar.gz"\n',
            encoding="utf-8",
        )

        asset_dir = root / "lab" / version / system_name / arch_name
        asset_dir.mkdir(parents=True, exist_ok=True)
        if system_name == "windows":
            payload = _zip_bytes(
                {
                    "dist-package/cursor-agent.cmd": "@echo off\r\nexit /b 0\r\n",
                    "dist-package/cursor-agent.ps1": "exit 0\n",
                    "dist-package/index.js": "console.log('ok')\n",
                    "dist-package/package.json": "{}\n",
                    "dist-package/node.exe": b"PE",
                }
            )
            (asset_dir / "agent-cli-package.zip").write_bytes(payload)
        else:
            payload = _tar_bytes(
                {
                    "dist-package/cursor-agent": "#!/bin/sh\nexit 0\n",
                    "dist-package/index.js": "console.log('ok')\n",
                    "dist-package/package.json": "{}\n",
                }
            )
            (asset_dir / "agent-cli-package.tar.gz").write_bytes(payload)
        return version

    def test_auto_install_from_local_fixture_server(self) -> None:
        system_name = cai._normalize_system()
        arch_name = cai._normalize_arch()
        if system_name not in ("linux", "darwin", "windows"):
            self.skipTest(f"unsupported test platform: {system_name}")
        if arch_name not in ("x64", "arm64"):
            self.skipTest(f"unsupported test arch: {arch_name}")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            version = self._write_fixture_tree(root, system_name=system_name, arch_name=arch_name)
            install_root = root / "install-root"
            bin_dir = root / "bin"
            with _LocalHttpServer(root) as srv, patch.dict(
                os.environ,
                {
                    cai.ENV_CCM_CURSOR_AGENT_INSTALLER_URL: f"{srv.base_url}/install.sh",
                    cai.ENV_CCM_CURSOR_AGENT_DOWNLOAD_BASE_URL: srv.base_url,
                    cai.ENV_CCM_CURSOR_AGENT_INSTALL_ROOT: str(install_root),
                    cai.ENV_CCM_CURSOR_AGENT_BIN_DIR: str(bin_dir),
                    cai.ENV_CCM_CURSOR_AGENT_POSTINSTALL_PATCH: "off",
                },
                clear=False,
            ), patch("cursor_cli_manager.cursor_agent_install.shutil.which", return_value=None):
                installed = cai.ensure_cursor_agent_available(auto_install=True)
                self.assertTrue(Path(installed).exists())
                if system_name == "windows":
                    self.assertTrue(str(installed).endswith(".cmd"))
                    self.assertTrue((install_root / "versions" / version / "cursor-agent.cmd").exists())
                else:
                    self.assertTrue((install_root / "versions" / version / "cursor-agent").exists())
                second = cai.ensure_cursor_agent_available(auto_install=True)
                self.assertEqual(second, installed)


if __name__ == "__main__":
    unittest.main()
