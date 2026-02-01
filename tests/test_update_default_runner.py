import os
import subprocess
import unittest
from unittest.mock import patch


class _FakeProc:
    def __init__(self, *, pid: int = 123, communicate_side_effect=None, communicate_result=("out", "err"), returncode: int = 0):
        self.pid = pid
        self._communicate_side_effect = communicate_side_effect
        self._communicate_result = communicate_result
        self.returncode = returncode
        self.killed = False
        self.communicate_calls = []

    def communicate(self, timeout=None):
        self.communicate_calls.append(timeout)
        if self._communicate_side_effect is not None:
            raise self._communicate_side_effect
        return self._communicate_result

    def kill(self):
        self.killed = True


class TestUpdateDefaultRunner(unittest.TestCase):
    def test_default_runner_uses_new_session_and_devnull_stdin(self) -> None:
        from cursor_cli_manager import update as upd

        captured = {}

        def fake_popen(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return _FakeProc(returncode=0)

        with patch.object(upd.subprocess, "Popen", side_effect=fake_popen):
            rc, out, err = upd._default_runner(["git", "status"], 0.1)

        self.assertEqual(rc, 0)
        self.assertEqual(out, "out")
        self.assertEqual(err, "err")

        kw = captured["kwargs"]
        self.assertIs(kw.get("stdin"), subprocess.DEVNULL)
        self.assertEqual(kw.get("stdout"), subprocess.PIPE)
        self.assertEqual(kw.get("stderr"), subprocess.PIPE)
        self.assertTrue(kw.get("text"))
        self.assertTrue(kw.get("start_new_session"))

        env = kw.get("env") or {}
        self.assertEqual(env.get("GIT_TERMINAL_PROMPT"), "0")
        self.assertEqual(env.get("PIP_DISABLE_PIP_VERSION_CHECK"), "1")

    def test_default_runner_timeout_kills_process_group(self) -> None:
        from cursor_cli_manager import update as upd

        if not hasattr(os, "killpg"):
            self.skipTest("os.killpg is not available on this platform")

        fake = _FakeProc(communicate_side_effect=subprocess.TimeoutExpired(cmd="x", timeout=0.01))

        with patch.object(upd.subprocess, "Popen", return_value=fake), patch.object(upd.os, "killpg") as killpg:
            rc, out, err = upd._default_runner(["git", "status"], 0.01)

        self.assertEqual(rc, 124)
        self.assertTrue(err)  # "timeout" or similar
        self.assertTrue(killpg.called)


if __name__ == "__main__":
    unittest.main()

