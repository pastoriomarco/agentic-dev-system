import asyncio
import unittest
from unittest.mock import AsyncMock

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

    def test_select_candidate_files_for_review_comment_prefers_commented_file(self):
        files = ["src/a.py", "src/b.py", "README.md"]
        issue_data = {
            "subject_kind": "pull_request",
            "trigger_source": "pr_review_comment",
            "title": "Update parser",
            "body": "@agent fix the bug on this line",
            "pr": {"changed_files": ["src/a.py", "src/b.py"]},
            "comment": {"body": "@agent fix the bug on this line", "path": "src/b.py", "line": 17},
        }

        candidates = self.orchestrator._select_candidate_files(files, issue_data)
        self.assertEqual(candidates, ["src/b.py"])

    def test_validate_requested_edit_scope_rejects_review_comment_edits_outside_commented_file(self):
        issue_data = {
            "subject_kind": "pull_request",
            "trigger_source": "pr_review_comment",
            "pr": {"changed_files": ["src/a.py", "src/b.py"]},
            "comment": {"body": "@agent fix this", "path": "src/a.py", "line": 12},
        }
        edits = [{"path": "src/b.py", "action": "overwrite", "content": "value = 2\n"}]

        with self.assertRaises(NeedsHumanError):
            self.orchestrator._validate_requested_edit_scope(issue_data, edits)

    def test_helper_branch_name_is_task_scoped_for_fork_prs(self):
        issue_data = {
            "subject_kind": "pull_request",
            "issue_number": 42,
            "task_id": "delivery-42-abcdef123456",
            "pr": {"number": 42, "same_repo": False},
        }

        self.assertEqual(self.orchestrator._pull_request_push_branch(issue_data), "agent/pr-42-delivery-42-abcd")

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

    def test_change_policy_rejects_review_comment_edits_outside_commented_file(self):
        session = AgentSession(
            session_id="s2",
            issue_number=6,
            repo_name="agentic-dev-system",
            issue_title="PR review task",
            issue_body="@agent",
            is_pr=True,
            subject_kind="pull_request",
            task_id="t2",
            working_dir="/tmp",
        )
        issue_data = {
            "subject_kind": "pull_request",
            "trigger_source": "pr_review_comment",
            "pr": {"changed_files": ["src/in_scope.py", "src/second.py"]},
            "comment": {"body": "@agent fix this", "path": "src/in_scope.py", "line": 14},
        }

        with self.assertRaises(NeedsHumanError):
            self.orchestrator._enforce_change_policies(session, ["src/second.py"], issue_data)

    def test_publish_changes_for_fork_pr_creates_helper_pr_and_comments_on_source(self):
        issue_data = {
            "subject_kind": "pull_request",
            "task_id": "delivery-7-abcdef",
            "title": "Fix fork PR",
            "issue_number": 7,
            "pr": {
                "number": 7,
                "title": "Fork PR",
                "html_url": "https://github.com/pastoriomarco/agentic-dev-system/pull/7",
                "same_repo": False,
                "base_ref": "main",
                "base_repo_full_name": "pastoriomarco/agentic-dev-system",
            },
        }
        session = AgentSession(
            session_id="fork-session",
            issue_number=7,
            repo_name="agentic-dev-system",
            issue_title="Fork PR",
            issue_body="@agent",
            is_pr=True,
            subject_kind="pull_request",
            task_id="delivery-7-abcdef",
            working_dir="/tmp",
        )
        session.changed_files = ["src/in_scope.py"]

        helper_pr = {"number": 99, "url": "https://github.com/pastoriomarco/agentic-dev-system/pull/99"}
        self.orchestrator._create_helper_pull_request = AsyncMock(return_value=helper_pr)
        self.orchestrator._comment_on_fork_source_pull_request = AsyncMock()

        result = asyncio.run(self.orchestrator._publish_changes(session, "Implement fix", issue_data))

        self.assertEqual(result, helper_pr)
        self.orchestrator._create_helper_pull_request.assert_awaited_once_with(issue_data, "Implement fix", ["src/in_scope.py"])
        self.orchestrator._comment_on_fork_source_pull_request.assert_awaited_once_with(
            issue_data,
            helper_pr,
            "Implement fix",
            ["src/in_scope.py"],
        )


if __name__ == "__main__":
    unittest.main()
