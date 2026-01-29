import os
import tempfile
import threading
import time
import unittest
import io
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from cursor_cli_manager.opening import (
    ENV_CURSOR_AGENT_PATH,
    DEFAULT_CURSOR_AGENT_FLAGS,
    build_new_command,
    build_resume_command,
    exec_new_chat,
    exec_resume_chat,
    get_cursor_agent_flags,
    resolve_cursor_agent_path,
    start_cursor_agent_flag_probe,
)


class TestOpening(unittest.TestCase):
    def test_resolve_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cursor-agent"
            p.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
            self.assertEqual(resolve_cursor_agent_path(str(p)), str(p))

    def test_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cursor-agent"
            p.write_text("x", encoding="utf-8")
            with patch("shutil.which", return_value=None):
                old = os.environ.get(ENV_CURSOR_AGENT_PATH)
                try:
                    os.environ[ENV_CURSOR_AGENT_PATH] = str(p)
                    self.assertEqual(resolve_cursor_agent_path(), str(p))
                finally:
                    if old is None:
                        os.environ.pop(ENV_CURSOR_AGENT_PATH, None)
                    else:
                        os.environ[ENV_CURSOR_AGENT_PATH] = old

    def test_build_resume_command(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = Path(td) / "cursor-agent"
            agent.write_text("x", encoding="utf-8")
            cmd = build_resume_command(
                "abc123",
                workspace_path=Path("/tmp/ws"),
                cursor_agent_path=str(agent),
            )
            self.assertEqual(cmd[0], str(agent))
            self.assertIn("--resume", cmd)
            self.assertIn("abc123", cmd)
            for flag in DEFAULT_CURSOR_AGENT_FLAGS:
                self.assertIn(flag, cmd)

    def test_build_new_command(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = Path(td) / "cursor-agent"
            agent.write_text("x", encoding="utf-8")
            cmd = build_new_command(workspace_path=Path("/tmp/ws"), cursor_agent_path=str(agent))
            self.assertEqual(cmd[0], str(agent))
            self.assertNotIn("--resume", cmd)
            self.assertIn("--workspace", cmd)
            for flag in DEFAULT_CURSOR_AGENT_FLAGS:
                self.assertIn(flag, cmd)

    def test_build_commands_use_probed_flags_when_available(self) -> None:
        # Simulate that only one optional flag is supported.
        import cursor_cli_manager.opening as opening

        old_started = opening._PROBE_STARTED
        old_probed = opening._PROBED_CURSOR_AGENT_FLAGS
        try:
            opening._PROBE_STARTED = True
            opening._PROBED_CURSOR_AGENT_FLAGS = ["--browser"]

            with tempfile.TemporaryDirectory() as td:
                agent = Path(td) / "cursor-agent"
                agent.write_text("x", encoding="utf-8")
                cmd = build_resume_command("abc123", workspace_path=Path("/tmp/ws"), cursor_agent_path=str(agent))
                self.assertIn("--browser", cmd)
                self.assertNotIn("--approve-mcps", cmd)
                self.assertNotIn("--force", cmd)
        finally:
            opening._PROBE_STARTED = old_started
            opening._PROBED_CURSOR_AGENT_FLAGS = old_probed

    def test_cursor_agent_flag_probe_is_non_blocking(self) -> None:
        import cursor_cli_manager.opening as opening

        evt = threading.Event()

        def fake_runner(_cmd, _timeout_s):
            # Block until test releases; runs in background thread.
            evt.wait(timeout=2.0)
            return 0, " --browser \n --approve-mcps \n", ""

        with tempfile.TemporaryDirectory() as td:
            agent = Path(td) / "cursor-agent"
            agent.write_text("x", encoding="utf-8")
            with patch("cursor_cli_manager.opening.resolve_cursor_agent_path", return_value=str(agent)), patch(
                "cursor_cli_manager.opening._default_runner", side_effect=fake_runner
            ):
                old_started = opening._PROBE_STARTED
                old_probed = opening._PROBED_CURSOR_AGENT_FLAGS
                try:
                    opening._PROBE_STARTED = False
                    opening._PROBED_CURSOR_AGENT_FLAGS = None

                    t0 = time.monotonic()
                    start_cursor_agent_flag_probe(timeout_s=0.01)
                    self.assertLess(time.monotonic() - t0, 0.2)

                    # Must not block even though probe is still running.
                    self.assertEqual(get_cursor_agent_flags(), DEFAULT_CURSOR_AGENT_FLAGS)
                finally:
                    evt.set()
                    t_wait = time.monotonic()
                    while opening._PROBED_CURSOR_AGENT_FLAGS is None and (time.monotonic() - t_wait) < 1.0:
                        time.sleep(0.01)
                    opening._PROBE_STARTED = old_started
                    opening._PROBED_CURSOR_AGENT_FLAGS = old_probed

    def test_prepare_exec_command_drops_force_when_unsupported(self) -> None:
        import cursor_cli_manager.opening as opening

        old_cache = dict(opening._OPTION_SUPPORT_CACHE)
        try:
            opening._OPTION_SUPPORT_CACHE.clear()

            with patch("cursor_cli_manager.opening._default_runner", return_value=(2, "", "unknown option: --force")):
                cmd = ["/tmp/cursor-agent", "--workspace", "/tmp/ws", "--force", "--resume", "abc123"]
                prepared = opening._prepare_exec_command(cmd)
                self.assertNotIn("--force", prepared)
                self.assertIn("--resume", prepared)
                self.assertIn("abc123", prepared)
        finally:
            opening._OPTION_SUPPORT_CACHE.clear()
            opening._OPTION_SUPPORT_CACHE.update(old_cache)

    def test_prepare_exec_command_filters_optional_flags(self) -> None:
        import cursor_cli_manager.opening as opening

        def supports_flag(_agent, flag):  # noqa: ANN001
            return flag != "--browser"

        with patch("cursor_cli_manager.opening._supports_optional_flag", side_effect=supports_flag):
            cmd = ["/tmp/cursor-agent", "--browser", "--approve-mcps", "--resume", "abc123"]
            prepared = opening._prepare_exec_command(cmd)
            self.assertNotIn("--browser", prepared)
            self.assertIn("--approve-mcps", prepared)
            self.assertIn("--resume", prepared)
            self.assertIn("abc123", prepared)

    def test_prepare_exec_command_drops_short_force_when_unsupported(self) -> None:
        import cursor_cli_manager.opening as opening

        with patch("cursor_cli_manager.opening._supports_optional_flag", return_value=False):
            cmd = ["/tmp/cursor-agent", "--force", "-f", "--resume", "abc123"]
            prepared = opening._prepare_exec_command(cmd)
            self.assertNotIn("--force", prepared)
            self.assertNotIn("-f", prepared)

    def test_supports_optional_flag_caches_result(self) -> None:
        import cursor_cli_manager.opening as opening

        old_cache = dict(opening._OPTION_SUPPORT_CACHE)
        try:
            opening._OPTION_SUPPORT_CACHE.clear()
            calls = []

            def fake_runner(cmd, _timeout_s):  # noqa: ANN001
                calls.append(list(cmd))
                return 0, "", ""

            with patch("cursor_cli_manager.opening._default_runner", side_effect=fake_runner):
                self.assertTrue(opening._supports_optional_flag("/tmp/cursor-agent", "--browser"))
                self.assertTrue(opening._supports_optional_flag("/tmp/cursor-agent", "--browser"))

            self.assertEqual(len(calls), 1)
        finally:
            opening._OPTION_SUPPORT_CACHE.clear()
            opening._OPTION_SUPPORT_CACHE.update(old_cache)

    def test_remove_flag_from_cmd_drops_value_pair(self) -> None:
        import cursor_cli_manager.opening as opening

        cmd = ["/tmp/cursor-agent", "--browser", "1", "--resume", "abc123"]
        self.assertEqual(
            opening._remove_flag_from_cmd(cmd, "--browser"),
            ["/tmp/cursor-agent", "--resume", "abc123"],
        )

    def test_remove_flag_from_cmd_drops_equals_form(self) -> None:
        import cursor_cli_manager.opening as opening

        cmd = ["/tmp/cursor-agent", "--browser=1", "--resume", "abc123"]
        self.assertEqual(
            opening._remove_flag_from_cmd(cmd, "--browser"),
            ["/tmp/cursor-agent", "--resume", "abc123"],
        )

    def test_extract_unknown_option_parses_unrecognized_arguments(self) -> None:
        import cursor_cli_manager.opening as opening

        err = "error: unrecognized arguments --browser --approve-mcps\n"
        self.assertEqual(opening._extract_unknown_option(err), "--browser")

    def test_extract_unknown_option_parses_short_flag(self) -> None:
        import cursor_cli_manager.opening as opening

        err = "error: unknown option -f\n"
        self.assertEqual(opening._extract_unknown_option(err), "-f")

    def test_exec_new_chat_prints_launching_message_before_exec(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            ws = td_path / "ws"
            ws.mkdir(parents=True, exist_ok=True)
            agent = td_path / "cursor-agent"
            agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            err_buf = io.StringIO()
            with patch(
                "cursor_cli_manager.opening.get_cursor_agent_flags", return_value=DEFAULT_CURSOR_AGENT_FLAGS
            ), patch("cursor_cli_manager.opening._supports_optional_flag", return_value=True), patch(
                "cursor_cli_manager.opening._run_cursor_agent", return_value=(0, "")
            ), redirect_stderr(err_buf):
                with self.assertRaises(SystemExit):
                    exec_new_chat(workspace_path=ws, cursor_agent_path=str(agent))

            self.assertIn("Launching cursor-agent", err_buf.getvalue())

    def test_exec_resume_chat_prints_launching_message_before_exec(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            ws = td_path / "ws"
            ws.mkdir(parents=True, exist_ok=True)
            agent = td_path / "cursor-agent"
            agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            err_buf = io.StringIO()
            with patch(
                "cursor_cli_manager.opening.get_cursor_agent_flags", return_value=DEFAULT_CURSOR_AGENT_FLAGS
            ), patch("cursor_cli_manager.opening._supports_optional_flag", return_value=True), patch(
                "cursor_cli_manager.opening._run_cursor_agent", return_value=(0, "")
            ), redirect_stderr(err_buf):
                with self.assertRaises(SystemExit):
                    exec_resume_chat("abc123", workspace_path=ws, cursor_agent_path=str(agent))

            out = err_buf.getvalue()
            self.assertIn("Launching cursor-agent", out)
            self.assertIn("abc123", out)

    def test_exec_new_chat_retries_without_force_when_admin_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            ws = td_path / "ws"
            ws.mkdir(parents=True, exist_ok=True)
            agent = td_path / "cursor-agent"
            agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            err_msg = (
                "Error: Your team administrator has disabled the 'Run Everything' option.\n"
                "Please run without '--force' to approve commands individually.\n"
            )
            captured: dict = {}

            def fake_execvp(_file, args):  # noqa: ANN001
                captured["args"] = list(args)
                raise RuntimeError("exec called")

            with patch(
                "cursor_cli_manager.opening.get_cursor_agent_flags", return_value=DEFAULT_CURSOR_AGENT_FLAGS
            ), patch("cursor_cli_manager.opening._supports_optional_flag", return_value=True), patch(
                "cursor_cli_manager.opening._run_cursor_agent", return_value=(1, err_msg)
            ), patch("cursor_cli_manager.opening.os.execvp", side_effect=fake_execvp):
                with self.assertRaises(RuntimeError):
                    exec_new_chat(workspace_path=ws, cursor_agent_path=str(agent))

            self.assertIn("--workspace", captured["args"])
            self.assertNotIn("--force", captured["args"])

    def test_exec_new_chat_drops_unknown_force_then_execs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            ws = td_path / "ws"
            ws.mkdir(parents=True, exist_ok=True)
            agent = td_path / "cursor-agent"
            agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            err_msg = "error: unknown option '--force'\n"
            captured: dict = {}

            def fake_execvp(_file, args):  # noqa: ANN001
                captured["args"] = list(args)
                raise RuntimeError("exec called")

            with patch(
                "cursor_cli_manager.opening.get_cursor_agent_flags", return_value=DEFAULT_CURSOR_AGENT_FLAGS
            ), patch("cursor_cli_manager.opening._supports_optional_flag", return_value=True), patch(
                "cursor_cli_manager.opening._run_cursor_agent", return_value=(2, err_msg)
            ), patch("cursor_cli_manager.opening.os.execvp", side_effect=fake_execvp):
                with self.assertRaises(RuntimeError):
                    exec_new_chat(workspace_path=ws, cursor_agent_path=str(agent))

            self.assertNotIn("--force", captured["args"])

    def test_exec_new_chat_retries_without_unknown_option(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            ws = td_path / "ws"
            ws.mkdir(parents=True, exist_ok=True)
            agent = td_path / "cursor-agent"
            agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            err_msg = "error: unknown option '--browser'\n"
            calls = []

            def fake_run(cmd):  # noqa: ANN001
                calls.append(list(cmd))
                if len(calls) == 1:
                    return 2, err_msg
                return 0, ""

            with patch(
                "cursor_cli_manager.opening.get_cursor_agent_flags", return_value=DEFAULT_CURSOR_AGENT_FLAGS
            ), patch("cursor_cli_manager.opening._supports_optional_flag", return_value=True), patch(
                "cursor_cli_manager.opening._run_cursor_agent", side_effect=fake_run
            ):
                with self.assertRaises(SystemExit):
                    exec_new_chat(workspace_path=ws, cursor_agent_path=str(agent))

            self.assertIn("--browser", calls[0])
            self.assertNotIn("--browser", calls[1])
            self.assertIn("--approve-mcps", calls[1])

    def test_exec_new_chat_retries_for_multiple_unknown_options(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            ws = td_path / "ws"
            ws.mkdir(parents=True, exist_ok=True)
            agent = td_path / "cursor-agent"
            agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            err_msgs = [
                "error: unknown option '--browser'\n",
                "error: unrecognized option '--approve-mcps'\n",
            ]
            calls = []

            def fake_run(cmd):  # noqa: ANN001
                calls.append(list(cmd))
                if len(calls) <= len(err_msgs):
                    return 2, err_msgs[len(calls) - 1]
                return 0, ""

            with patch(
                "cursor_cli_manager.opening.get_cursor_agent_flags", return_value=DEFAULT_CURSOR_AGENT_FLAGS
            ), patch("cursor_cli_manager.opening._supports_optional_flag", return_value=True), patch(
                "cursor_cli_manager.opening._run_cursor_agent", side_effect=fake_run
            ):
                with self.assertRaises(SystemExit):
                    exec_new_chat(workspace_path=ws, cursor_agent_path=str(agent))

            self.assertIn("--browser", calls[0])
            self.assertNotIn("--browser", calls[1])
            self.assertIn("--approve-mcps", calls[1])
            self.assertNotIn("--approve-mcps", calls[2])


if __name__ == "__main__":
    unittest.main()

