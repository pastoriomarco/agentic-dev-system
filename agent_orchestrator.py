"""
Agent Orchestrator - Main agent that processes issues and creates PRs
"""

import os
import sys
import subprocess
import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).parent))


@dataclass
class AgentSession:
    """Represents an agent session for processing an issue"""
    session_id: str
    issue_number: int
    repo_name: str
    issue_title: str
    issue_body: str
    is_pr: bool
    status: str = "pending"
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    output_pr_number: Optional[int] = None
    output_pr_url: Optional[str] = None
    logs: List[str] = None
    errors: List[str] = None
    working_dir: str = ""
    
    def __post_init__(self):
        if self.logs is None:
            self.logs = []
        if self.errors is None:
            self.errors = []
        if not self.created_at:
            self.created_at = datetime.utcnow().isoformat()
    
    def to_dict(self):
        return asdict(self)


class AgentOrchestrator:
    """Main orchestrator for agent workflow"""
    
    def __init__(self, github_token: str, repo_url: str, working_base: str = "/tmp/agent-work"):
        self.github_token = github_token
        self.repo_url = repo_url
        self.working_base = Path(working_base)
        self.working_base.mkdir(parents=True, exist_ok=True)
        self.llm_api_url = os.environ.get("LLM_API_URL", "http://localhost:8080/v1/chat/completions")
        self.llm_model = os.environ.get("LLM_MODEL", "qwen3-coder-next")
        self.base_branch = os.environ.get("GITHUB_BASE_BRANCH", "main")

    def _authenticated_repo_url(self) -> str:
        if not self.github_token:
            return self.repo_url
        if self.repo_url.startswith("https://github.com/"):
            suffix = self.repo_url[len("https://github.com/") :]
            return f"https://x-access-token:{self.github_token}@github.com/{suffix}"
        return self.repo_url

    def _owner_repo(self) -> tuple[str, str]:
        parsed = urlparse(self.repo_url)
        path = parsed.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        parts = path.split("/")
        if len(parts) < 2:
            raise Exception(f"Unable to parse owner/repo from URL: {self.repo_url}")
        return parts[0], parts[1]
    
    def _log(self, session: AgentSession, message: str):
        timestamp = datetime.utcnow().isoformat()
        log_entry = f"[{timestamp}] {message}"
        session.logs.append(log_entry)
        print(log_entry)
    
    async def process_issue(self, issue_data: Dict[str, Any]) -> AgentSession:
        session_id = str(uuid.uuid4())
        session = AgentSession(
            session_id=session_id,
            issue_number=issue_data["issue_number"],
            repo_name=issue_data.get("repo_name", self.repo_url.split("/")[-1]),
            issue_title=issue_data.get("title", ""),
            issue_body=issue_data.get("body", ""),
            is_pr=issue_data.get("is_pr", False),
        )
        try:
            session.status = "in_progress"
            session.started_at = datetime.utcnow().isoformat()
            self._log(session, f"Fetching repository: {self.repo_url}")
            await self._clone_repository(session)
            self._log(session, f"Analyzing issue #{issue_data['issue_number']}")
            plan = await self._plan_changes(session, issue_data)
            self._log(session, f"Plan: {plan[:200]}...")
            self._log(session, "Implementing changes...")
            implementation = await self._implement_changes(session, plan, issue_data)
            self._log(session, f"Implementation complete: {len(implementation)} files modified")
            self._log(session, "Creating commit...")
            commit_result = await self._create_commit(session, plan, issue_data)
            if not commit_result.get("created_commit"):
                session.status = "completed"
                session.completed_at = datetime.utcnow().isoformat()
                self._log(session, "No code changes were created; skipping PR creation.")
                return session
            self._log(session, "Creating pull request...")
            pr_result = await self._create_pull_request(session, plan, issue_data)
            session.status = "completed"
            session.completed_at = datetime.utcnow().isoformat()
            session.output_pr_number = pr_result.get("number")
            session.output_pr_url = pr_result.get("url")
        except Exception as e:
            session.status = "failed"
            session.errors.append(str(e))
            self._log(session, f"Error: {e}")
        return session
    
    async def _clone_repository(self, session: AgentSession) -> str:
        work_dir = self.working_base / session.session_id
        work_dir.mkdir(parents=True, exist_ok=True)
        session.working_dir = str(work_dir)
        clone_url = self._authenticated_repo_url()
        result = subprocess.run(
            ["git", "clone", "--depth", "1", clone_url, "."],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise Exception(f"Git clone failed: {result.stderr}")
        return str(work_dir)
    
    async def _plan_changes(self, session: AgentSession, issue_data: Dict[str, Any]) -> str:
        plan = f"""
Based on issue #{issue_data['issue_number']}:
Title: {issue_data['title']}

Analysis:
- Review the codebase structure
- Identify relevant files
- Determine implementation approach

Implementation Steps:
1. Analyze existing code
2. Create necessary changes
3. Test the changes
4. Commit with clear message
5. Create PR with description

This plan will be generated by your LLM model."""
        return plan
    
    async def _implement_changes(self, session: AgentSession, plan: str, issue_data: Dict[str, Any]) -> List[str]:
        work_dir = Path(session.working_dir)
        report_dir = work_dir / "agentic_changes"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"issue_{issue_data['issue_number']}.md"
        report_content = (
            f"# Agent Execution Report for Issue #{issue_data['issue_number']}\n\n"
            f"## Title\n{issue_data.get('title', '')}\n\n"
            f"## Original Description\n{issue_data.get('body', '')}\n\n"
            f"## Plan\n{plan}\n\n"
            f"## Generated At\n{datetime.utcnow().isoformat()}Z\n"
        )
        report_path.write_text(report_content, encoding="utf-8")
        self._log(session, f"Wrote MVP change artifact: {report_path}")
        return [str(report_path.relative_to(work_dir))]
    
    async def _create_commit(self, session: AgentSession, plan: str, issue_data: Dict[str, Any]) -> Dict[str, Any]:
        work_dir = session.working_dir
        subprocess.run(["git", "config", "user.email", "agent@github.local"], cwd=work_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "AI Agent"], cwd=work_dir, capture_output=True, check=True)
        branch_name = f"agent/{issue_data['issue_number']}"
        checkout = subprocess.run(["git", "checkout", "-b", branch_name], cwd=work_dir, capture_output=True, text=True)
        if checkout.returncode != 0:
            raise Exception(f"Git checkout failed: {checkout.stderr}")
        add = subprocess.run(["git", "add", "."], cwd=work_dir, capture_output=True, text=True)
        if add.returncode != 0:
            raise Exception(f"Git add failed: {add.stderr}")
        commit_msg = f"Agent fix: Issue #{issue_data['issue_number']}\n\n{plan[:500]}"
        result = subprocess.run(["git", "commit", "-m", commit_msg], cwd=work_dir, capture_output=True, text=True)
        if result.returncode != 0:
            if "nothing to commit" in result.stderr or "nothing to commit" in result.stdout:
                return {"success": True, "created_commit": False, "message": "No changes needed"}
            raise Exception(f"Git commit failed: {result.stderr}")
        push = subprocess.run(
            ["git", "push", "-u", "origin", branch_name],
            cwd=work_dir,
            capture_output=True,
            text=True,
        )
        if push.returncode != 0:
            raise Exception(f"Git push failed: {push.stderr}")
        return {"success": True, "created_commit": True, "branch": branch_name}
    
    async def _create_pull_request(self, session: AgentSession, plan: str, issue_data: Dict[str, Any]) -> Dict[str, Any]:
        branch_name = f"agent/{issue_data['issue_number']}"
        pr_title = f"Agent fix: {issue_data['title']}"
        pr_body = f"""
Fix for issue #{issue_data['issue_number']}: {issue_data['title']}

## Description
This PR was generated by an AI developer agent.

## Changes Made
{plan}

## Testing
Manual testing recommended before merging.

---
Generated by AI Developer Agent
"""
        if not self.github_token:
            raise Exception("GITHUB_TOKEN is required to create pull requests.")
        owner, repo = self._owner_repo()
        gh_url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
        headers = {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json"
        }
        pr_data = {
            "title": pr_title,
            "body": pr_body,
            "head": branch_name,
            "base": self.base_branch
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(gh_url, headers=headers, json=pr_data)
            if response.status_code != 201:
                raise Exception(f"Failed to create PR: {response.text}")
            pr_info = response.json()
            return {"number": pr_info.get("number"), "url": pr_info.get("html_url")}


if __name__ == "__main__":
    orchestrator = AgentOrchestrator(
        github_token="your-token-here",
        repo_url="https://github.com/user/repo.git"
    )
    
    issue_data = {
        "issue_number": 1,
        "title": "Add feature",
        "body": "Implement a new feature"
    }
    
    async def main():
        session = await orchestrator.process_issue(issue_data)
        print(f"Session completed: {session.status}")

    # import asyncio
    # asyncio.run(main())
