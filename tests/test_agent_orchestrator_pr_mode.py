import unittest

from agent_orchestrator import AgentOrchestrator, AgentSession, NeedsHumanError


class PullRequestOrchestratorModeTest(unittest.TestCase):
    def setUp(self):
        self.orchestrator = AgentOrchestrator(github_token="", repo_url="https://github.com/pastoriomarco/agentic-dev-system.git")
        self.orchestrator._count_diff_lines = lambda _work_dir: 0

    def test_select_candidate_files_is_limited_to_pr_changed_files(self):
        files = ["src/a.py", "src/b.py", "README.md"]
        issue_data = {
            "subject_kind": "pull_request",
            "title": "Update parser",
            "body": "@agent update src/a.py",
            "pr": {"changed_files": ["src/a.py"]},
            "comment": {"body": "@agent update src/a.py"},
        }

        candidates = self.orchestrator._select_candidate_files(files, issue_data)
        self.assertEqual(candidates, ["src/a.py"])

    def test_change_policy_rejects_pr_edits_outside_reviewed_diff(self):
        session = AgentSession(
            session_id="s1",
            issue_number=5,
            repo_name="agentic-dev-system",
            issue_title="PR task",
            issue_body="@agent",
            is_pr=True,
            subject_kind="pull_request",
            task_id="t1",
            working_dir="/tmp",
        )
        issue_data = {
            "subject_kind": "pull_request",
            "pr": {"changed_files": ["src/in_scope.py"]},
        }

        with self.assertRaises(NeedsHumanError):
            self.orchestrator._enforce_change_policies(session, ["src/out_of_scope.py"], issue_data)


if __name__ == "__main__":
    unittest.main()
