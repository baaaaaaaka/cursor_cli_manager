import builtins
import json
import os
import posixpath
import tempfile
import threading
import unittest
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Callable
from unittest.mock import patch
from urllib.parse import unquote
import zipfile

import cursor_cli_manager.windows_deps as wd


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

        def log_message(self, _format: str, *_args: object) -> None:  # silence test output
            return

    return Handler


class _LocalHttpServer:
    def __init__(self, root: Path) -> None:
        self._root = root
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


class TestWindowsDeps(unittest.TestCase):
    def test_ensure_windows_deps_noop_non_windows(self) -> None:
        with patch("cursor_cli_manager.windows_deps.sys.platform", "linux"), patch(
            "cursor_cli_manager.windows_deps.ensure_windows_curses"
        ) as ensure_curses, patch("cursor_cli_manager.windows_deps.ensure_ripgrep") as ensure_rg:
            wd.ensure_windows_deps()
            ensure_curses.assert_not_called()
            ensure_rg.assert_not_called()

    def test_ensure_windows_deps_calls_on_windows(self) -> None:
        with patch("cursor_cli_manager.windows_deps.sys.platform", "win32"), patch(
            "cursor_cli_manager.windows_deps.ensure_windows_curses"
        ) as ensure_curses, patch("cursor_cli_manager.windows_deps.ensure_ripgrep") as ensure_rg:
            wd.ensure_windows_deps()
            ensure_curses.assert_called_once()
            ensure_rg.assert_called_once()

    def test_default_windows_bin_dir_prefers_localappdata(self) -> None:
        with patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Temp"}):
            expected = Path(r"C:\Temp") / "ccm" / "bin"
            self.assertEqual(wd._default_windows_bin_dir(), expected)

    def test_prepend_to_path_inserts_once(self) -> None:
        with patch.dict(os.environ, {"PATH": r"C:\A;C:\B"}):
            wd._prepend_to_path(Path(r"C:\Tools"))
            self.assertTrue(os.environ["PATH"].startswith(r"C:\Tools"))
            before = os.environ["PATH"]
            wd._prepend_to_path(Path(r"C:\tools"))
            self.assertEqual(os.environ["PATH"], before)

    def test_ripgrep_arch_suffix(self) -> None:
        with patch("cursor_cli_manager.windows_deps.platform.machine", return_value="AMD64"):
            self.assertEqual(wd._ripgrep_arch_suffix(), "x86_64-pc-windows-msvc")
        with patch("cursor_cli_manager.windows_deps.platform.machine", return_value="ARM64"):
            self.assertEqual(wd._ripgrep_arch_suffix(), "aarch64-pc-windows-msvc")
        with patch("cursor_cli_manager.windows_deps.platform.machine", return_value="i686"):
            self.assertEqual(wd._ripgrep_arch_suffix(), "i686-pc-windows-msvc")
        with patch("cursor_cli_manager.windows_deps.platform.machine", return_value="mips"):
            self.assertIsNone(wd._ripgrep_arch_suffix())

    def test_ensure_windows_curses_no_install_when_import_ok(self) -> None:
        with patch("cursor_cli_manager.windows_deps.sys.platform", "win32"), patch(
            "cursor_cli_manager.windows_deps._run_pip_install"
        ) as run_pip:
            self.assertTrue(wd.ensure_windows_curses())
            run_pip.assert_not_called()

    def test_ensure_windows_curses_pip_failure(self) -> None:
        orig_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "curses":
                raise ImportError("missing curses")
            return orig_import(name, *args, **kwargs)

        with patch("cursor_cli_manager.windows_deps.sys.platform", "win32"), patch(
            "cursor_cli_manager.windows_deps._run_pip_install", return_value=False
        ) as run_pip:
            with patch("builtins.__import__", side_effect=fake_import):
                self.assertFalse(wd.ensure_windows_curses())
            run_pip.assert_called_once()

    def test_fetch_ripgrep_asset_from_local_server(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            asset_name = "ripgrep-13.0.0-x86_64-pc-windows-msvc.zip"
            (root / asset_name).write_bytes(b"zipdata")
            with _LocalHttpServer(root) as srv:
                release = {
                    "assets": [
                        {
                            "name": asset_name,
                            "browser_download_url": f"{srv.base_url}/{asset_name}",
                        }
                    ]
                }
                (root / "release.json").write_text(json.dumps(release), encoding="utf-8")
                with patch("cursor_cli_manager.windows_deps._RIPGREP_API_URL", f"{srv.base_url}/release.json"):
                    asset = wd._fetch_ripgrep_asset("x86_64-pc-windows-msvc")
                    self.assertEqual(asset, (asset_name, f"{srv.base_url}/{asset_name}"))

    def test_download_and_extract_rg(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            asset_name = "ripgrep-13.0.0-x86_64-pc-windows-msvc.zip"
            zip_path = root / asset_name
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("rg.exe", b"rg")
            with _LocalHttpServer(root) as srv:
                dest_zip = root / "download.zip"
                url = f"{srv.base_url}/{asset_name}"
                self.assertTrue(wd._download_file(url, dest_zip))
                out = root / "rg.exe"
                self.assertTrue(wd._extract_rg(dest_zip, out))
                self.assertEqual(out.read_bytes(), b"rg")

    def test_ensure_ripgrep_downloads_and_installs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            install_dir = root / "bin"
            asset_name = "ripgrep-13.0.0-x86_64-pc-windows-msvc.zip"
            zip_path = root / asset_name
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("rg.exe", b"rg")

            with _LocalHttpServer(root) as srv:
                release = {
                    "assets": [
                        {
                            "name": asset_name,
                            "browser_download_url": f"{srv.base_url}/{asset_name}",
                        }
                    ]
                }
                (root / "release.json").write_text(json.dumps(release), encoding="utf-8")

                with patch("cursor_cli_manager.windows_deps.sys.platform", "win32"), patch(
                    "cursor_cli_manager.windows_deps.platform.machine", return_value="AMD64"
                ), patch(
                    "cursor_cli_manager.windows_deps.shutil.which", return_value=None
                ), patch(
                    "cursor_cli_manager.windows_deps._RIPGREP_API_URL", f"{srv.base_url}/release.json"
                ), patch.dict(os.environ, {"PATH": ""}):
                    rg = wd.ensure_ripgrep(bin_dir=install_dir)
                    self.assertEqual(rg, install_dir / "rg.exe")
                    self.assertTrue((install_dir / "rg.exe").exists())
                    self.assertTrue(os.environ["PATH"].startswith(str(install_dir)))
