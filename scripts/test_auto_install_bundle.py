#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import os
import posixpath
import shutil
import subprocess
import tarfile
import tempfile
import threading
import zipfile
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Callable
from urllib.parse import unquote


VERSION = "2026.02.27-e7d2ef6"


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


def _tar_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def _write_fixture_tree(root: Path, *, system_name: str, arch_name: str) -> None:
    installer = root / "install.sh"
    installer.write_text(
        "#!/usr/bin/env bash\n"
        f'DOWNLOAD_URL="https://downloads.cursor.com/lab/{VERSION}/${{OS}}/${{ARCH}}/agent-cli-package.tar.gz"\n',
        encoding="utf-8",
    )

    asset_dir = root / "lab" / VERSION / system_name / arch_name
    asset_dir.mkdir(parents=True, exist_ok=True)
    if system_name == "windows":
        payload = _zip_bytes(
            {
                "dist-package/cursor-agent.cmd": b"@echo off\r\nexit /b 0\r\n",
                "dist-package/cursor-agent.ps1": b"exit 0\n",
                "dist-package/index.js": b"console.log('ok')\n",
                "dist-package/package.json": b"{}\n",
                "dist-package/node.exe": b"MZ",
            }
        )
        (asset_dir / "agent-cli-package.zip").write_bytes(payload)
    else:
        payload = _tar_bytes(
            {
                "dist-package/cursor-agent": b"#!/bin/sh\nexit 0\n",
                "dist-package/index.js": b"console.log('ok')\n",
                "dist-package/package.json": b"{}\n",
            }
        )
        (asset_dir / "agent-cli-package.tar.gz").write_bytes(payload)


def _sanitized_path(system_name: str) -> str:
    if system_name == "windows":
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        candidates = [system_root / "System32", system_root]
        return ";".join(str(p) for p in candidates if p.exists())
    candidates = [Path("/usr/bin"), Path("/bin"), Path("/usr/sbin"), Path("/sbin")]
    return os.pathsep.join(str(p) for p in candidates if p.exists())


def _seed_windows_rg(localappdata: Path) -> None:
    rg = localappdata / "ccm" / "bin" / "rg.exe"
    rg.parent.mkdir(parents=True, exist_ok=True)
    rg.write_bytes(b"MZ")


def _assert_installed(install_root: Path, bin_dir: Path, *, system_name: str) -> None:
    if system_name == "windows":
        version_exe = install_root / "versions" / VERSION / "cursor-agent.cmd"
        bin_exe = bin_dir / "cursor-agent.cmd"
    else:
        version_exe = install_root / "versions" / VERSION / "cursor-agent"
        bin_exe = bin_dir / "cursor-agent"
    if not version_exe.exists():
        raise SystemExit(f"missing installed agent bundle: {version_exe}")
    if not bin_exe.exists():
        raise SystemExit(f"missing installed launcher: {bin_exe}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--os", dest="system_name", required=True, choices=("linux", "darwin", "windows"))
    parser.add_argument("--arch", dest="arch_name", required=True, choices=("x64", "arm64"))
    parser.add_argument("--ccm-bin", required=True)
    args = parser.parse_args()

    ccm_bin = Path(args.ccm_bin).expanduser()
    if not ccm_bin.exists():
        raise SystemExit(f"ccm binary not found: {ccm_bin}")

    with tempfile.TemporaryDirectory(prefix="ccm-auto-install.") as td:
        base = Path(td)
        fixture = base / "fixture"
        install_root = base / "install-root"
        bin_dir = base / "install-bin"
        cfg_dir = base / "config"
        ws_dir = base / "workspace"
        localappdata = base / "localappdata"
        for path in (fixture, install_root, bin_dir, cfg_dir, ws_dir):
            path.mkdir(parents=True, exist_ok=True)
        _write_fixture_tree(fixture, system_name=args.system_name, arch_name=args.arch_name)

        env = dict(os.environ)
        env["CCM_CURSOR_AGENT_INSTALL_ROOT"] = str(install_root)
        env["CCM_CURSOR_AGENT_BIN_DIR"] = str(bin_dir)
        env["CCM_CURSOR_AGENT_POSTINSTALL_PATCH"] = "off"
        env["PATH"] = _sanitized_path(args.system_name)
        env.pop("CURSOR_AGENT_PATH", None)
        if args.system_name == "windows":
            env["LOCALAPPDATA"] = str(localappdata)
            _seed_windows_rg(localappdata)

        with _LocalHttpServer(fixture) as srv:
            env["CCM_CURSOR_AGENT_INSTALLER_URL"] = f"{srv.base_url}/install.sh"
            env["CCM_CURSOR_AGENT_DOWNLOAD_BASE_URL"] = srv.base_url
            proc = subprocess.run(
                [str(ccm_bin), "--config-dir", str(cfg_dir), "open", "abc123", "--workspace", str(ws_dir)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
        if proc.returncode != 0:
            raise SystemExit(
                f"ccm auto-install smoke failed with exit {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        _assert_installed(install_root, bin_dir, system_name=args.system_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
