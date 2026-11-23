from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cursor_cli_manager import cli, opening, proxy
from cursor_cli_manager.agent_paths import CursorAgentDirs


ENDPOINT = {"verified": True, "url": "http://127.0.0.1:34567"}


class ProxyTest(unittest.TestCase):
    def test_local_commands_do_not_acquire_proxy(self):
        with tempfile.TemporaryDirectory() as root, patch.object(proxy, "_call") as call:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["--config-dir", root, "--proxy-profile", "remote", "list"]), 0)
            call.assert_not_called()
            self.assertIsNone(proxy.selected_profile())

    def test_browsing_tui_defers_cursor_install_and_proxy_start(self):
        with tempfile.TemporaryDirectory() as root, contextlib.ExitStack() as stack:
            stack.enter_context(proxy.selected_proxy("remote"))
            stack.enter_context(patch.object(cli, "curses", object()))
            acquire = stack.enter_context(patch.object(proxy, "_call"))
            install = stack.enter_context(patch.object(cli, "_ensure_cursor_agent_for_command"))
            stack.enter_context(patch.object(cli, "has_legacy_install", return_value=False))
            stack.enter_context(patch.object(cli, "discover_agent_workspaces", return_value=[]))
            stack.enter_context(patch.object(cli, "_pin_cwd_workspace", return_value=[object()]))
            stack.enter_context(patch.object(cli, "_run_tui", return_value=None))
            self.assertEqual(cli.cmd_tui(CursorAgentDirs(Path(root))), 0)
            acquire.assert_not_called()
            install.assert_not_called()

    def test_managed_dry_run_preserves_config_and_workspace(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Path(root) / "custom config"
            workspace = Path(root) / "my 'workspace"
            output = io.StringIO()
            with contextlib.ExitStack() as stack:
                call = stack.enter_context(patch.object(proxy, "_call"))
                build = stack.enter_context(patch.object(cli, "build_resume_command"))
                learn = stack.enter_context(patch.object(cli, "learn_workspace_path"))
                stack.enter_context(proxy.selected_proxy("lab"))
                stack.enter_context(contextlib.redirect_stdout(output))
                self.assertEqual(
                    cli.cmd_open(CursorAgentDirs(cfg), "chat-42", dry_run=True, workspace_path=workspace),
                    0,
                )
            call.assert_not_called()
            build.assert_not_called()
            learn.assert_not_called()
            if os.name == "nt":
                self.assertIn("'--proxy-profile' 'lab'", output.getvalue())
                self.assertIn("my ''workspace'", output.getvalue())
            else:
                argv = shlex.split(output.getvalue())
                self.assertEqual(argv[:6], ["ccm", "--proxy-profile", "lab", "--config-dir", str(cfg), "--no-patch-models"])
                self.assertEqual(argv[6:], ["open", "chat-42", "--workspace", str(workspace)])

    def test_proxy_env_sets_both_spellings_on_posix(self):
        original = {"HttP_Proxy": "wrong", "https_proxy": "wrong", "NO_PROXY": "*,127.0.0.2", "OTHER": "ok"}
        with patch.object(proxy, "_call", return_value=ENDPOINT), proxy.selected_proxy("remote"):
            with patch.object(proxy.sys, "platform", "linux"):
                env = proxy.proxy_environment(original, cursor=True, package_manager=True)
            self.assertEqual(env["HTTP_PROXY"], ENDPOINT["url"])
            self.assertEqual(env["HTTPS_PROXY"], env["https_proxy"])
            self.assertEqual(env["NO_PROXY"], "localhost,127.0.0.1,::1,127.0.0.2")
            self.assertEqual(env["no_proxy"], env["NO_PROXY"])
            self.assertEqual(env["NODE_USE_ENV_PROXY"], "1")
            self.assertEqual(env["PIP_PROXY"], env["HTTPS_PROXY"])
            self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.proxy")
            self.assertEqual(env["OTHER"], "ok")
            self.assertNotIn("HttP_Proxy", env)
            self.assertEqual(original["NO_PROXY"], "*,127.0.0.2")

    def test_windows_proxy_env_contains_one_canonical_key(self):
        with patch.object(proxy, "_call", return_value=ENDPOINT), proxy.selected_proxy("remote"):
            with patch.object(proxy.sys, "platform", "win32"):
                env = proxy.proxy_environment({"HTTP_PROXY": "wrong", "hTtP_PrOxY": "old"})
            self.assertEqual(sorted(env), ["ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"])

    def test_launch_reuses_agent_flags_and_passes_verified_env(self):
        command = ["cursor-agent", "resume", "example"]
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(proxy, "_call", return_value=ENDPOINT))
            stack.enter_context(patch.object(opening, "_should_use_windows_interactive_runner", return_value=False))
            execvpe = stack.enter_context(patch.object(opening.os, "execvpe", side_effect=SystemExit(0)))
            execvp = stack.enter_context(patch.object(opening.os, "execvp"))
            stack.enter_context(proxy.selected_proxy("remote"))
            with self.assertRaises(SystemExit):
                opening._exec_or_run_cursor_agent(command)
        self.assertEqual(execvpe.call_args[0][:2], (command[0], command))
        self.assertEqual(execvpe.call_args[0][2]["HTTP_PROXY"], ENDPOINT["url"])
        execvp.assert_not_called()

    def test_launch_fails_closed_when_helper_is_unavailable(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(proxy, "_call", side_effect=proxy.ProxyError("offline")))
            stack.enter_context(patch.object(opening, "_should_use_windows_interactive_runner", return_value=False))
            execvpe = stack.enter_context(patch.object(opening.os, "execvpe"))
            execvp = stack.enter_context(patch.object(opening.os, "execvp"))
            stack.enter_context(proxy.selected_proxy("remote"))
            with self.assertRaisesRegex(proxy.ProxyError, "offline"):
                opening._exec_or_run_cursor_agent(["cursor-agent"])
        execvpe.assert_not_called()
        execvp.assert_not_called()

    def test_rejects_an_unverified_external_url(self):
        bad = {"verified": True, "url": "http://remote.org:80"}
        with patch.object(proxy, "_call", return_value=bad), proxy.selected_proxy("remote"):
            with self.assertRaisesRegex(proxy.ProxyError, "verified"):
                proxy.proxy_environment()

    def test_helper_rejects_protocol_mismatch(self):
        proc = subprocess.CompletedProcess([], 0, stdout=json.dumps({"version": 2, "result": ENDPOINT}).encode())
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"CCM_PROXY_STATE_DIR": root}), patch.object(
            proxy, "_helper", return_value=Path(root) / "helper"
        ), patch.object(proxy.subprocess, "run", return_value=proc):
            with self.assertRaisesRegex(proxy.ProxyError, "unsupported interface"):
                proxy._call(["list"])
