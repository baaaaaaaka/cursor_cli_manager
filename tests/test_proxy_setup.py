from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cursor_cli_manager import cli, proxy, proxy_setup


UNSET = {"mode": "unset", "revision": 0, "profiles": []}
LAB = {"name": "lab", "id": "e" * 32}
OTHER = {"name": "other", "id": "a" * 32}


class ProxySetupTest(unittest.TestCase):
    def test_preference_resolution(self):
        cases = [
            (UNSET, (None, None)),
            ({**UNSET, "profiles": [LAB]}, (LAB["name"], LAB["id"])),
            ({**UNSET, "mode": "direct", "revision": 1, "profiles": [LAB, OTHER]}, (None, None)),
            ({**UNSET, "mode": "ssh", "revision": 1, "id": LAB["id"], "profiles": [LAB, OTHER]},
             (LAB["name"], LAB["id"])),
            ({**UNSET, "profiles": [LAB, OTHER]}, "multiple"),
            ({**UNSET, "mode": "ssh", "revision": 1, "id": OTHER["id"], "profiles": [LAB]}, "removed"),
        ]
        for state, expected in cases:
            with self.subTest(state=state), patch.object(proxy_setup, "_call", return_value=state) as call, patch.object(
                proxy_setup, "_interactive", return_value=False,
            ), patch.object(proxy_setup, "_prompt", side_effect=AssertionError("unexpected stdin read")):
                if isinstance(expected, str):
                    with self.assertRaisesRegex(proxy.ProxyError, expected):
                        proxy_setup.resolve_selection(None, prompt=True)
                else:
                    self.assertEqual(proxy_setup.resolve_selection(None, prompt=True), expected)
                call.assert_called_once_with(["profile", "list", "--setup"])
                call.reset_mock()
                self.assertEqual(proxy_setup.resolve_selection("explicit", prompt=True), ("explicit", None))
                call.assert_not_called()

    def test_pipe_with_no_input_does_not_block_or_create_state(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "proxy"
            env = dict(os.environ, CCM_PROXY_STATE_DIR=str(state))
            startup = (
                "import sys; from cursor_cli_manager import cli, proxy_setup; "
                "proxy_setup._setup_state = lambda: proxy_setup.SetupState('unset', 0, None, ()); "
                "cli.cmd_tui = lambda *args, **kwargs: 0; "
                "sys.exit(cli.main(['--config-dir', sys.argv[1], 'tui']))"
            )
            cases = [
                (["-c", startup, str(Path(root) / "cursor")], 0, b""),
                (["-m", "cursor_cli_manager", "proxy", "init"], 1, b"needs an interactive terminal"),
            ]
            for args, code, message in cases:
                with subprocess.Popen([sys.executable, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=env) as proc:
                    try:
                        self.assertEqual(proc.wait(timeout=8), code)
                        if message:
                            self.assertIn(message, proc.stderr.read())
                    finally:
                        if proc.poll() is None:
                            proc.kill()
            self.assertFalse(state.exists())

    def test_first_question_and_subprompts_cancel_at_eof(self):
        for answers in ("", "y", "y\n", "y\n0\n", "y\n0\nfixture\n", "y\n0\nfixture\n\n",
                        "y\na\n", "y\na\n/path/to/config\n"):
            with self.subTest(answers=answers), patch.object(
                proxy_setup, "_call", return_value=UNSET,
            ) as call, patch.object(proxy_setup, "_interactive", return_value=True), patch.object(
                proxy_setup, "_host_aliases", return_value=[],
            ), patch.object(sys, "stdin", io.StringIO(answers)), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(proxy.ProxyError, "input ended"):
                    proxy_setup.resolve_selection(None, prompt=True)
                call.assert_called_once_with(["profile", "list", "--setup"])

    def test_default_no_is_saved_before_tui(self):
        saved = {**UNSET, "mode": "direct", "revision": 1}
        with tempfile.TemporaryDirectory() as root, patch.object(
            proxy_setup, "_call", side_effect=[UNSET, None, saved],
        ) as call, patch.object(proxy_setup, "_interactive", return_value=True), patch.object(
            sys, "stdin", io.StringIO("\n"),
        ), contextlib.redirect_stderr(io.StringIO()) as out:
            def tui(*args, **kwargs):
                self.assertIn("Use an SSH proxy", out.getvalue())
                self.assertEqual(call.call_args_list[1][0][0], ["setup", "direct", "0"])
                self.assertIsNone(proxy.selected_profile())
                return 0

            with patch.object(cli, "cmd_tui", side_effect=tui) as launch:
                self.assertEqual(cli.main(["--config-dir", root]), 0)
                launch.assert_called_once()

    def test_winning_process_choice_is_resolved_after_concurrent_change(self):
        saved = {**UNSET, "mode": "ssh", "revision": 1, "id": LAB["id"], "profiles": [LAB]}
        conflict = proxy.ProxyError("settings changed", code="setup_conflict")
        with patch.object(proxy_setup, "_call", side_effect=[UNSET, conflict, saved]), patch.object(
            proxy_setup, "_interactive", return_value=True,
        ), patch.object(sys, "stdin", io.StringIO("n\n")), contextlib.redirect_stderr(io.StringIO()) as out:
            self.assertEqual(proxy_setup.resolve_selection(None, prompt=True), (LAB["name"], LAB["id"]))
            self.assertIn("another CCM process", out.getvalue())

    def test_saved_proxy_reaches_the_windows_bootstrap(self):
        saved = {**UNSET, "mode": "ssh", "revision": 1, "id": LAB["id"], "profiles": [LAB]}
        with tempfile.TemporaryDirectory() as root, patch.object(
            proxy_setup, "_call", return_value=saved,
        ), patch.object(cli, "curses", None), contextlib.redirect_stderr(io.StringIO()):
            def install_curses():
                self.assertEqual(proxy.selected_profile(), LAB["name"])
                self.assertEqual(proxy.selected_profile_id(), LAB["id"])
                return False

            with patch.object(cli, "ensure_windows_curses", side_effect=install_curses) as install:
                self.assertEqual(cli.main(["--config-dir", root, "tui"]), 1)
                install.assert_called_once()

    def test_dry_run_replays_through_ccm_without_starting_proxy(self):
        saved = {**UNSET, "mode": "ssh", "revision": 1, "id": LAB["id"], "profiles": [LAB]}
        with tempfile.TemporaryDirectory() as root, patch.object(
            proxy_setup, "_call", return_value=saved,
        ) as call, patch.object(proxy, "_call") as runtime, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["--config-dir", root, "open", "session", "--workspace", root, "--dry-run"]), 0)
            self.assertIn("ccm", out.getvalue())
            self.assertNotIn("--proxy-profile", out.getvalue())
            call.assert_called_once_with(["profile", "list", "--setup"])
            runtime.assert_not_called()
        with proxy.selected_proxy(LAB["name"], LAB["id"]), patch.object(
            proxy, "_call", side_effect=proxy.ProxyError("changed"),
        ) as call:
            with self.assertRaisesRegex(proxy.ProxyError, "changed"):
                proxy.proxy_environment(cursor=True)
            call.assert_called_once_with(["ensure", LAB["name"], LAB["id"]])

    def test_source_install_without_helper_keeps_direct_commands_working(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"CCM_PROXY_STATE_DIR": root}), patch.object(
            proxy_setup, "_call", side_effect=proxy.ProxyHelperMissing("missing"),
        ):
            self.assertEqual(proxy_setup.resolve_selection(None, prompt=True), (None, None))
            (Path(root) / "proxy-v1.sqlite3").touch()
            with self.assertRaisesRegex(proxy.ProxyError, "missing"):
                proxy_setup.resolve_selection(None, prompt=False)

    def test_alias_discovery_leaves_wildcards_and_includes_to_openssh(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "ssh config"
            path.write_text(
                'Host * !excluded # only patterns\nInclude config.d/*\n'
                'Host="Office" lab-1 # two aliases\nHost office\nMatch all\n',
                encoding="utf-8",
            )
            self.assertEqual(proxy_setup._host_aliases(path), ["Office", "lab-1"])
