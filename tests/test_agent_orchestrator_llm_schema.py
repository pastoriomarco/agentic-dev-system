import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_orchestrator import AgentOrchestrator, AgentSession, NeedsHumanError


class AgentOrchestratorLLMSchemaTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.tempdir.name)
        self._init_git_repo()
        self.orchestrator = AgentOrchestrator(
            github_token="",
            repo_url="https://github.com/pastoriomarco/agentic-dev-system.git",
        )
        self.orchestrator._count_diff_lines = lambda _work_dir: 0
        self.orchestrator.max_edit_actions = 3

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    def _run_git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=self.repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )

    def _init_git_repo(self) -> None:
        (self.repo_dir / "src").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
        self._run_git("init")
        self._run_git("config", "user.email", "test@example.com")
        self._run_git("config", "user.name", "Test User")
        self._run_git("add", ".")
        self._run_git("commit", "-m", "Initial commit")

    async def _clone_into_fixture_repo(self, session: AgentSession, issue_data: dict) -> str:
        session.working_dir = str(self.repo_dir)
        return str(self.repo_dir)

    def _base_issue_data(self) -> dict:
        return {
            "issue_number": 41,
            "title": "Update app value",
            "body": "Set the value to 2",
            "subject_kind": "issue",
            "is_pr": False,
            "task_id": "task-41",
        }

    async def test_process_issue_marks_needs_human_when_plan_summary_missing(self):
        async def fake_call_llm(_system_prompt: str, _user_prompt: str) -> str:
            return '{"rationale":"missing summary"}'

        self.orchestrator._clone_repository = self._clone_into_fixture_repo
        self.orchestrator._call_llm = fake_call_llm

        session = await self.orchestrator.process_issue(self._base_issue_data())

        self.assertEqual(session.status, "needs_human")
        self.assertIn("non-empty summary", session.errors[0])

    def test_validate_plan_response_accepts_valid_payload(self):
        parsed = self.orchestrator._validate_plan_response(
            {"summary": "Update the constant", "rationale": "Small fix", "risk_level": "low"}
        )

        self.assertEqual(parsed["summary"], "Update the constant")
        self.assertEqual(parsed["rationale"], "Small fix")
        self.assertEqual(parsed["risk_level"], "low")

    def test_validate_edit_response_rejects_invalid_payload_shapes(self):
        invalid_payloads = [
            (
                {"summary": "x", "edits": [{"path": "src/app.py", "action": "invalid", "content": "x"}]},
                "unsupported action",
            ),
            (
                {"summary": "x", "edits": [{"action": "overwrite", "content": "x"}]},
                "non-empty string path",
            ),
            (
                {"summary": "x", "edits": [{"path": "src/app.py", "action": "append"}]},
                "requires a string content field",
            ),
            (
                {"summary": "x", "edits": [{"path": "src/app.py", "action": "replace", "find": "", "replace": "y"}]},
                "non-empty string find",
            ),
            (
                {"summary": "x", "edits": [{"path": "src/app.py", "action": "overwrite", "content": "y"}] * 4},
                "AGENT_MAX_EDIT_ACTIONS",
            ),
            (
                {
                    "summary": "x",
                    "edits": [{"path": "src/app.py", "action": "overwrite", "content": "y"}],
                    "quality_commands": ["echo bad"],
                },
                "unsupported top-level keys",
            ),
        ]

        for payload, expected_error in invalid_payloads:
            with self.subTest(expected_error=expected_error):
                with self.assertRaises(NeedsHumanError) as context:
                    self.orchestrator._validate_edit_response(payload)
                self.assertIn(expected_error, str(context.exception))

    async def test_valid_edit_response_produces_tracked_changes(self):
        async def fake_call_llm(_system_prompt: str, _user_prompt: str) -> str:
            return '{"summary":"Update app","edits":[{"path":"src/app.py","action":"overwrite","content":"value = 2\\n"}]}'

        self.orchestrator._call_llm = fake_call_llm
        session = AgentSession(
            session_id="session-valid-edit",
            issue_number=41,
            repo_name="agentic-dev-system",
            issue_title="Update app value",
            issue_body="Set the value to 2",
            is_pr=False,
            working_dir=str(self.repo_dir),
        )

        changed_files = await self.orchestrator._implement_changes(session, "Update value", self._base_issue_data())

        self.assertEqual(changed_files, ["src/app.py"])
        self.assertEqual((self.repo_dir / "src" / "app.py").read_text(encoding="utf-8"), "value = 2\n")

    async def test_process_issue_marks_needs_human_when_edit_schema_is_invalid(self):
        responses = iter(
            [
                '{"summary":"Update the constant","rationale":"Simple change","risk_level":"low"}',
                '{"summary":"Apply edit","edits":[{"path":"src/app.py","action":"overwrite","content":"value = 2\\n"}],"quality_commands":["echo no"]}',
            ]
        )

        async def fake_call_llm(_system_prompt: str, _user_prompt: str) -> str:
            return next(responses)

        self.orchestrator._clone_repository = self._clone_into_fixture_repo
        self.orchestrator._call_llm = fake_call_llm

        session = await self.orchestrator.process_issue(self._base_issue_data())

        self.assertEqual(session.status, "needs_human")
        self.assertIn("unsupported top-level keys", session.errors[0])


if __name__ == "__main__":
    unittest.main()
