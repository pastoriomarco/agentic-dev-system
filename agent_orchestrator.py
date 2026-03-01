"""
Agent Orchestrator - processes one issue and opens a PR.
"""

import json
import os
import re
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx


@dataclass
class AgentSession:
    """Represents an agent session for processing an issue."""

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
    """Main orchestrator for agent workflow."""

    def __init__(self, github_token: str, repo_url: str, working_base: str = "/tmp/agent-work"):
        self.github_token = github_token
        self.repo_url = repo_url
        self.working_base = Path(working_base)
        self.working_base.mkdir(parents=True, exist_ok=True)

        self.llm_api_url = os.environ.get("LLM_API_URL", "http://localhost:8080/v1/chat/completions")
        self.llm_model = os.environ.get("LLM_MODEL", "qwen3-coder-next")
        self.base_branch = os.environ.get("GITHUB_BASE_BRANCH", "main")

        self.max_changed_files = int(os.environ.get("AGENT_MAX_CHANGED_FILES", "20"))
        self.max_diff_lines = int(os.environ.get("AGENT_MAX_DIFF_LINES", "1500"))
        self.quality_timeout_seconds = int(os.environ.get("AGENT_QUALITY_TIMEOUT_SECONDS", "600"))
        self.allow_no_quality_gates = os.environ.get("AGENT_ALLOW_NO_QUALITY_GATES", "false").lower() == "true"
        self.agent_permissions_file = os.environ.get("AGENT_PERMISSIONS_FILE", "/app/AGENT_PERMISSIONS.md")

        self.allowed_path_prefixes = self._split_csv_env("AGENT_ALLOWED_PATH_PREFIXES")
        forbidden = self._split_csv_env("AGENT_FORBIDDEN_PATH_PREFIXES")
        self.forbidden_path_prefixes = forbidden or [".git/", ".github/workflows/", ".env", "secrets/"]
        self.quality_commands = self._split_commands_env("AGENT_QUALITY_COMMANDS")
        self.permissions_context = self._load_permissions_context()

    def _split_csv_env(self, env_name: str) -> List[str]:
        raw = os.environ.get(env_name, "")
        return [part.strip() for part in raw.split(",") if part.strip()]

    def _split_commands_env(self, env_name: str) -> List[str]:
        raw = os.environ.get(env_name, "")
        return [part.strip() for part in raw.split(";;") if part.strip()]

    def _load_permissions_context(self) -> str:
        path = Path(self.agent_permissions_file)
        if not path.exists():
            return "Permissions file not found. Follow configured policy checks and quality gates."
        try:
            return path.read_text(encoding="utf-8", errors="ignore")[:4000]
        except Exception:
            return "Failed to read permissions file. Follow configured policy checks and quality gates."

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

    def _run(
        self,
        cmd: List[str] | str,
        cwd: str,
        timeout: int = 120,
        shell: bool = False,
        check: bool = False,
    ) -> subprocess.CompletedProcess:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=shell,
        )
        if check and result.returncode != 0:
            raise Exception(f"Command failed ({cmd}): {result.stderr or result.stdout}")
        return result

    async def process_issue(self, issue_data: Dict[str, Any]) -> AgentSession:
        session = AgentSession(
            session_id=str(uuid.uuid4()),
            issue_number=issue_data["issue_number"],
            repo_name=issue_data.get("repo_name", self.repo_url.split("/")[-1]),
            issue_title=issue_data.get("title", ""),
            issue_body=issue_data.get("body", ""),
            is_pr=issue_data.get("is_pr", False),
        )
        try:
            session.status = "in_progress"
            session.started_at = datetime.utcnow().isoformat()

            self._log(session, f"Cloning repository: {self.repo_url}")
            await self._clone_repository(session)

            self._log(session, f"Planning changes for issue #{issue_data['issue_number']}")
            plan = await self._plan_changes(session, issue_data)
            self._log(session, f"Plan summary: {plan[:200]}")

            self._log(session, "Applying repo-aware edits from LLM plan")
            changed_files = await self._implement_changes(session, plan, issue_data)
            self._log(session, f"Changed files: {len(changed_files)}")

            if not changed_files:
                session.status = "completed"
                session.completed_at = datetime.utcnow().isoformat()
                self._log(session, "No changes created; stopping before commit.")
                return session

            self._log(session, "Running policy checks")
            self._enforce_change_policies(session, changed_files)

            self._log(session, "Running quality gates (lint/test)")
            await self._run_quality_gates(session)

            self._log(session, "Creating commit")
            commit_result = await self._create_commit(session, plan, issue_data)
            if not commit_result.get("created_commit"):
                session.status = "completed"
                session.completed_at = datetime.utcnow().isoformat()
                self._log(session, "Nothing to commit after checks.")
                return session

            self._log(session, "Creating pull request")
            pr_result = await self._create_pull_request(session, plan, issue_data)
            session.status = "completed"
            session.completed_at = datetime.utcnow().isoformat()
            session.output_pr_number = pr_result.get("number")
            session.output_pr_url = pr_result.get("url")
        except Exception as exc:
            session.status = "failed"
            session.errors.append(str(exc))
            self._log(session, f"Error: {exc}")
        return session

    async def _clone_repository(self, session: AgentSession) -> str:
        work_dir = self.working_base / session.session_id
        work_dir.mkdir(parents=True, exist_ok=True)
        session.working_dir = str(work_dir)
        clone_url = self._authenticated_repo_url()
        result = self._run(["git", "clone", "--depth", "1", clone_url, "."], cwd=str(work_dir), timeout=300)
        if result.returncode != 0:
            raise Exception(f"Git clone failed: {result.stderr}")
        return str(work_dir)

    async def _list_repo_files(self, work_dir: Path) -> List[str]:
        result = self._run(["git", "ls-files"], cwd=str(work_dir), timeout=60)
        if result.returncode != 0:
            raise Exception(f"Failed to list files: {result.stderr}")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _extract_keywords(self, issue_data: Dict[str, Any]) -> List[str]:
        text = f"{issue_data.get('title','')} {issue_data.get('body','')}".lower()
        keywords = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{3,}", text)
        stop = {"this", "that", "with", "from", "have", "should", "issue", "please", "need", "into"}
        return [k for k in keywords if k not in stop][:25]

    def _score_file(self, file_path: str, keywords: List[str]) -> int:
        path_l = file_path.lower()
        return sum(3 if kw in path_l else 0 for kw in keywords)

    def _select_candidate_files(self, files: List[str], issue_data: Dict[str, Any]) -> List[str]:
        keywords = self._extract_keywords(issue_data)
        scored = sorted(files, key=lambda path: self._score_file(path, keywords), reverse=True)
        strong = [f for f in scored if self._score_file(f, keywords) > 0]
        fallback = [f for f in files if f.endswith((".py", ".md", ".yaml", ".yml", ".json", ".toml"))]
        candidates = (strong[:20] + fallback[:20])[:25]
        return list(dict.fromkeys(candidates))

    def _read_file_snippet(self, work_dir: Path, rel_path: str, max_lines: int = 120) -> str:
        path = work_dir / rel_path
        if not path.exists() or not path.is_file():
            return ""
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
        lines = content.splitlines()
        return "\n".join(lines[:max_lines])

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        full_system_prompt = (
            system_prompt.strip()
            + "\n\nRuntime permissions and constraints:\n"
            + self.permissions_context.strip()
            + "\n\nAlways comply with these constraints."
        )
        payload = {
            "model": self.llm_model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": full_system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(self.llm_api_url, json=payload)
        if response.status_code >= 300:
            raise Exception(f"LLM API failed: {response.status_code} {response.text[:400]}")
        data = response.json()
        try:
            return data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise Exception(f"Unexpected LLM response shape: {exc}")

    def _extract_json_object(self, text: str) -> Dict[str, Any]:
        text = text.strip()
        # Support fenced JSON responses.
        if "```" in text:
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
            if match:
                text = match.group(1)
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise Exception("No JSON object found in LLM output.")
        return json.loads(text[start : end + 1])

    async def _plan_changes(self, session: AgentSession, issue_data: Dict[str, Any]) -> str:
        work_dir = Path(session.working_dir)
        files = await self._list_repo_files(work_dir)
        candidates = self._select_candidate_files(files, issue_data)
        snippets = []
        for rel in candidates[:12]:
            snippet = self._read_file_snippet(work_dir, rel, max_lines=60)
            if snippet:
                snippets.append(f"## {rel}\n{snippet}")
        system_prompt = (
            "You are a senior software engineer. Return JSON only with keys: "
            "summary, rationale, risk_level. Keep summary under 200 chars."
        )
        user_prompt = (
            f"Issue #{issue_data['issue_number']}\n"
            f"Title: {issue_data.get('title','')}\n"
            f"Body:\n{issue_data.get('body','')}\n\n"
            f"Candidate files:\n" + "\n".join(candidates[:30]) + "\n\n"
            f"Snippets:\n{chr(10).join(snippets[:8])}"
        )
        response_text = await self._call_llm(system_prompt, user_prompt)
        parsed = self._extract_json_object(response_text)
        return parsed.get("summary", "Implement requested issue changes")

    def _validate_rel_path(self, rel_path: str) -> str:
        normalized = rel_path.replace("\\", "/").lstrip("./")
        if not normalized:
            raise Exception("Invalid empty edit path.")
        if normalized.startswith("../") or "/../" in normalized:
            raise Exception(f"Path traversal blocked: {rel_path}")
        if any(normalized == p or normalized.startswith(p) for p in self.forbidden_path_prefixes):
            raise Exception(f"Path blocked by forbidden policy: {normalized}")
        if self.allowed_path_prefixes and not any(
            normalized == p or normalized.startswith(p.rstrip("/") + "/") for p in self.allowed_path_prefixes
        ):
            raise Exception(f"Path not in allowed prefixes: {normalized}")
        return normalized

    def _apply_edit(self, work_dir: Path, edit: Dict[str, Any]) -> str:
        rel_path = self._validate_rel_path(str(edit.get("path", "")))
        action = str(edit.get("action", "")).lower()
        target = work_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        current = target.read_text(encoding="utf-8", errors="ignore") if target.exists() else ""

        if action == "overwrite":
            content = str(edit.get("content", ""))
            target.write_text(content, encoding="utf-8")
        elif action == "append":
            content = str(edit.get("content", ""))
            prefix = "" if current.endswith("\n") or not current else "\n"
            target.write_text(current + prefix + content, encoding="utf-8")
        elif action == "replace":
            find = str(edit.get("find", ""))
            replace = str(edit.get("replace", ""))
            if not find:
                raise Exception(f"Replace action missing 'find' for {rel_path}")
            if find not in current:
                raise Exception(f"Replace target not found in {rel_path}")
            target.write_text(current.replace(find, replace), encoding="utf-8")
        else:
            raise Exception(f"Unsupported edit action '{action}' for {rel_path}")
        return rel_path

    async def _request_edits_from_llm(
        self,
        issue_data: Dict[str, Any],
        plan: str,
        candidate_files: List[str],
        file_snippets: Dict[str, str],
    ) -> Dict[str, Any]:
        system_prompt = (
            "You generate minimal code edits. Return JSON only with keys: "
            "summary, edits, quality_commands. "
            "Each edit item: {path, action, content/find/replace}. "
            "Allowed actions: overwrite, append, replace."
        )
        snippets_blob = "\n\n".join(
            [f"## {path}\n{snippet}" for path, snippet in file_snippets.items() if snippet.strip()]
        )
        user_prompt = (
            f"Issue #{issue_data['issue_number']}\n"
            f"Title: {issue_data.get('title','')}\n"
            f"Body:\n{issue_data.get('body','')}\n\n"
            f"Plan summary:\n{plan}\n\n"
            f"Candidate files:\n" + "\n".join(candidate_files[:40]) + "\n\n"
            f"File snippets:\n{snippets_blob}\n\n"
            "Return minimal edits to satisfy the issue."
        )
        response_text = await self._call_llm(system_prompt, user_prompt)
        return self._extract_json_object(response_text)

    async def _implement_changes(self, session: AgentSession, plan: str, issue_data: Dict[str, Any]) -> List[str]:
        work_dir = Path(session.working_dir)
        files = await self._list_repo_files(work_dir)
        candidates = self._select_candidate_files(files, issue_data)
        file_snippets = {path: self._read_file_snippet(work_dir, path, max_lines=120) for path in candidates[:12]}

        llm_edits = await self._request_edits_from_llm(issue_data, plan, candidates, file_snippets)
        edits = llm_edits.get("edits", [])
        if not isinstance(edits, list) or not edits:
            raise Exception("LLM produced no edits.")

        touched = []
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            touched_path = self._apply_edit(work_dir, edit)
            touched.append(touched_path)

        status = self._run(["git", "status", "--porcelain"], cwd=str(work_dir), timeout=60, check=True)
        changed_files = []
        for line in status.stdout.splitlines():
            if len(line) > 3:
                changed_files.append(line[3:].strip())
        changed_files = sorted(set(changed_files))
        if not changed_files:
            self._log(session, "LLM edits did not produce tracked file changes.")
        return changed_files

    def _count_diff_lines(self, work_dir: str) -> int:
        diff = self._run(["git", "diff", "--unified=0"], cwd=work_dir, timeout=60, check=True)
        return len(diff.stdout.splitlines())

    def _enforce_change_policies(self, session: AgentSession, changed_files: List[str]) -> None:
        if len(changed_files) > self.max_changed_files:
            raise Exception(f"Policy violation: changed files {len(changed_files)} > {self.max_changed_files}")
        for rel_path in changed_files:
            self._validate_rel_path(rel_path)
        diff_lines = self._count_diff_lines(session.working_dir)
        if diff_lines > self.max_diff_lines:
            raise Exception(f"Policy violation: diff lines {diff_lines} > {self.max_diff_lines}")

    def _infer_quality_commands(self, work_dir: Path) -> List[str]:
        commands: List[str] = []
        if (work_dir / "pyproject.toml").exists() or (work_dir / "setup.cfg").exists():
            commands.append("python -m pytest -q")
            commands.append("python -m ruff check .")
        elif (work_dir / "tests").exists() or (work_dir / "pytest.ini").exists():
            commands.append("python -m pytest -q")
        return commands

    async def _run_quality_gates(self, session: AgentSession) -> None:
        work_dir = Path(session.working_dir)
        commands = self.quality_commands or self._infer_quality_commands(work_dir)
        if not commands and not self.allow_no_quality_gates:
            raise Exception(
                "No quality commands configured/detected. Set AGENT_QUALITY_COMMANDS or AGENT_ALLOW_NO_QUALITY_GATES=true."
            )
        for cmd in commands:
            self._log(session, f"Quality gate: {cmd}")
            result = self._run(cmd, cwd=str(work_dir), shell=True, timeout=self.quality_timeout_seconds)
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                stdout = (result.stdout or "").strip()
                output_preview = (stderr or stdout)[-1200:]
                raise Exception(f"Quality gate failed: {cmd}\n{output_preview}")

    async def _create_commit(self, session: AgentSession, plan: str, issue_data: Dict[str, Any]) -> Dict[str, Any]:
        work_dir = session.working_dir
        self._run(["git", "config", "user.email", "agent@github.local"], cwd=work_dir, check=True)
        self._run(["git", "config", "user.name", "AI Agent"], cwd=work_dir, check=True)
        branch_name = f"agent/{issue_data['issue_number']}"

        checkout = self._run(["git", "checkout", "-b", branch_name], cwd=work_dir)
        if checkout.returncode != 0:
            raise Exception(f"Git checkout failed: {checkout.stderr}")

        add = self._run(["git", "add", "."], cwd=work_dir)
        if add.returncode != 0:
            raise Exception(f"Git add failed: {add.stderr}")

        commit_msg = f"Agent fix: Issue #{issue_data['issue_number']}\n\n{plan[:500]}"
        result = self._run(["git", "commit", "-m", commit_msg], cwd=work_dir)
        if result.returncode != 0:
            if "nothing to commit" in (result.stderr + result.stdout):
                return {"success": True, "created_commit": False, "message": "No changes needed"}
            raise Exception(f"Git commit failed: {result.stderr}")

        push = self._run(["git", "push", "-u", "origin", branch_name], cwd=work_dir)
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
Automated quality gates were executed before commit.

---
Generated by AI Developer Agent
"""
        if not self.github_token:
            raise Exception("GITHUB_TOKEN is required to create pull requests.")
        owner, repo = self._owner_repo()
        gh_url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
        headers = {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        pr_data = {
            "title": pr_title,
            "body": pr_body,
            "head": branch_name,
            "base": self.base_branch,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(gh_url, headers=headers, json=pr_data)
            if response.status_code != 201:
                raise Exception(f"Failed to create PR: {response.text}")
            pr_info = response.json()
            return {"number": pr_info.get("number"), "url": pr_info.get("html_url")}
