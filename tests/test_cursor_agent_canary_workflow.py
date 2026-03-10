import unittest
from pathlib import Path


class TestCursorAgentCanaryWorkflow(unittest.TestCase):
    def test_canary_workflow_covers_expected_platforms_and_schedule(self) -> None:
        root = Path(__file__).resolve().parent.parent
        txt = (root / ".github" / "workflows" / "cursor_agent_installer_canary.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "0 */4 * * *"', txt)
        self.assertIn("ubuntu-latest", txt)
        self.assertIn("macos-15-intel", txt)
        self.assertIn("macos-latest", txt)
        self.assertIn("windows-latest", txt)
        self.assertIn("scripts/run_cursor_agent_patch_canary.py", txt)

    def test_canary_workflow_manages_failure_issue(self) -> None:
        root = Path(__file__).resolve().parent.parent
        txt = (root / ".github" / "workflows" / "cursor_agent_installer_canary.yml").read_text(encoding="utf-8")
        self.assertIn("Upstream cursor cli patch canary failing", txt)
        self.assertIn("actions/github-script@v7", txt)
        self.assertIn("listJobsForWorkflowRun", txt)


if __name__ == "__main__":
    unittest.main()
