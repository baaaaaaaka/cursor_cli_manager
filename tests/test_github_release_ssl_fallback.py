import os
import unittest
from unittest.mock import patch

import urllib.error


class _Resp:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._data


class TestGithubReleaseSslFallback(unittest.TestCase):
    def test_default_fetch_retries_with_bundled_cafile_on_cert_verify_error_when_frozen(self) -> None:
        from cursor_cli_manager import github_release as gr

        calls = {"n": 0}

        def fake_urlopen(req, timeout=0.0, context=None):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("CERTIFICATE_VERIFY_FAILED")
            self.assertIsNotNone(context)
            return _Resp(b"ok")

        def fake_create_default_context(*, cafile=None, capath=None, cadata=None):  # noqa: ANN001
            self.assertEqual(cafile, "/tmp/ca.pem")
            return object()

        with patch.object(gr, "is_frozen_binary", return_value=True), patch.dict(
            os.environ, {}, clear=True
        ), patch.object(gr, "_bundled_cafile", return_value="/tmp/ca.pem"), patch(
            "cursor_cli_manager.github_release.urllib.request.urlopen", side_effect=fake_urlopen
        ), patch(
            "cursor_cli_manager.github_release.ssl.create_default_context", side_effect=fake_create_default_context
        ):
            b = gr._default_fetch("https://example.invalid", 0.1, {})
        self.assertEqual(b, b"ok")
        self.assertEqual(calls["n"], 2)

    def test_default_fetch_does_not_retry_when_not_frozen(self) -> None:
        from cursor_cli_manager import github_release as gr

        def fake_urlopen(req, timeout=0.0, context=None):  # noqa: ANN001
            raise urllib.error.URLError("CERTIFICATE_VERIFY_FAILED")

        with patch.object(gr, "is_frozen_binary", return_value=False), patch(
            "cursor_cli_manager.github_release.urllib.request.urlopen", side_effect=fake_urlopen
        ):
            with self.assertRaises(urllib.error.URLError):
                gr._default_fetch("https://example.invalid", 0.1, {})

    def test_default_fetch_does_not_override_user_ssl_cert_file(self) -> None:
        from cursor_cli_manager import github_release as gr

        calls = {"n": 0}

        def fake_urlopen(req, timeout=0.0, context=None):  # noqa: ANN001
            calls["n"] += 1
            raise urllib.error.URLError("CERTIFICATE_VERIFY_FAILED")

        with patch.object(gr, "is_frozen_binary", return_value=True), patch.dict(
            os.environ, {"SSL_CERT_FILE": "/custom.pem"}, clear=True
        ), patch(
            "cursor_cli_manager.github_release.urllib.request.urlopen", side_effect=fake_urlopen
        ):
            with self.assertRaises(urllib.error.URLError):
                gr._default_fetch("https://example.invalid", 0.1, {})
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()

