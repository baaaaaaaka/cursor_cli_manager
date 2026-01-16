import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from cursor_cli_manager.cli import main


class TestCliUpgrade(unittest.TestCase):
    def test_upgrade_success_exit_code_0(self) -> None:
        buf = io.StringIO()
        with patch("cursor_cli_manager.cli.perform_update", return_value=(True, "ok")), redirect_stdout(buf):
            rc = main(["upgrade"])
        self.assertEqual(rc, 0)
        self.assertIn("ok", buf.getvalue())

    def test_upgrade_failure_exit_code_1(self) -> None:
        buf = io.StringIO()
        with patch("cursor_cli_manager.cli.perform_update", return_value=(False, "nope")), redirect_stdout(buf):
            rc = main(["upgrade"])
        self.assertEqual(rc, 1)
        self.assertIn("nope", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

