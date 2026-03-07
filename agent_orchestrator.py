"""
Agent Orchestrator - processes one issue or pull-request task and publishes changes.
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
from network_policy import NetworkPolicyError, parse_host_patterns, validate_llm_endpoint


class NeedsHumanError(Exception):
    """Raised when the task should halt and return to human review."""


@dataclass
class AgentSession:
    """Represents an agent session for processing an issue."""

    session_id: str
    issue_number: int
    repo_name: str
    issue_title: str
    issue_body: str
    is_pr: bool
    subject_kind: str = "issue"
    task_id: str = ""
    status: str = "pending"
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    output_pr_number: Optional[int] = None
    output_pr_url: Optional[str] = None
    changed_files: List[str] = None
    logs: List[str] = None
    errors: List[str] = None
    working_dir: str = ""

    def __post_init__(self):
        if self.logs is None:
            self.logs = []
        if self.errors is None:
            self.errors = []
        if self.changed_files is None:
            self.changed_files = []
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
        self.llm_host_allowlist = parse_host_patterns(os.environ.get("LLM_HOST_ALLOWLIST", "localhost"))
        self.no_proxy_hosts = parse_host_patterns(
            os.environ.get("WORKER_NO_PROXY")
            or os.environ.get("NO_PROXY")
            or os.environ.get("no_proxy")
            or "localhost,127.0.0.1"
        )
        self.base_branch = os.environ.get("GITHUB_BASE_BRANCH", "main")

        self.max_changed_files = int(os.environ.get("AGENT_MAX_CHANGED_FILES", "20"))
        self.max_diff_lines = int(os.environ.get("AGENT_MAX_DIFF_LINES", "1500"))
        self.max_edit_actions = int(os.environ.get("AGENT_MAX_EDIT_ACTIONS", "50"))
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

    def _subprocess_env(self, include_write_token: bool = False) -> Dict[str, str]:
        env = os.environ.copy()
        env.pop("GITHUB_TOKEN", None)
        env.pop("GITHUB_WRITE_TOKEN", None)
        if include_write_token and self.github_token:
            env["GITHUB_WRITE_TOKEN"] = self.github_token
        return env

    def _run(
        self,
        cmd: List[str] | str,
        cwd: str,
        timeout: int = 120,
        shell: bool = False,
        check: bool = False,
        env: Optional[Dict[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=shell,
            env=env,
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
            subject_kind=issue_data.get("subject_kind", "issue"),
            task_id=issue_data.get("task_id", ""),
        )
        try:
            session.status = "in_progress"
            session.started_at = datetime.utcnow().isoformat()

            self._log(session, f"Cloning repository: {self.repo_url}")
            await self._clone_repository(session, issue_data)

            if issue_data.get("subject_kind") == "pull_request":
                pr_changed_files = self._get_pull_request_changed_files(Path(session.working_dir), issue_data)
                issue_data.setdefault("pr", {})["changed_files"] = pr_changed_files
                self._log(session, f"Pull request changed files in scope: {len(pr_changed_files)}")

            self._log(session, f"Planning changes for issue #{issue_data['issue_number']}")
            plan = await self._plan_changes(session, issue_data)
            self._log(session, f"Plan summary: {plan[:200]}")

            self._log(session, "Applying repo-aware edits from LLM plan")
            changed_files = await self._implement_changes(session, plan, issue_data)
            session.changed_files = changed_files
            self._log(session, f"Changed files: {len(changed_files)}")

            if not changed_files:
                session.status = "completed"
                session.completed_at = datetime.utcnow().isoformat()
                self._log(session, "No changes created; stopping before commit.")
                return session

            self._log(session, "Running policy checks")
            self._enforce_change_policies(session, changed_files, issue_data)

            self._log(session, "Running quality gates (lint/test)")
            await self._run_quality_gates(session)

            self._log(session, "Creating commit")
            commit_result = await self._create_commit(session, plan, issue_data)
            if not commit_result.get("created_commit"):
                session.status = "completed"
                session.completed_at = datetime.utcnow().isoformat()
                self._log(session, "Nothing to commit after checks.")
                return session

            publish_action = "Updating pull request" if issue_data.get("subject_kind") == "pull_request" else "Creating pull request"
            self._log(session, publish_action)
            pr_result = await self._publish_changes(session, plan, issue_data)
            session.status = "completed"
            session.completed_at = datetime.utcnow().isoformat()
            session.output_pr_number = pr_result.get("number")
            session.output_pr_url = pr_result.get("url")
        except NeedsHumanError as exc:
            session.status = "needs_human"
            session.errors.append(str(exc))
            self._log(session, f"Needs human review: {exc}")
        except Exception as exc:
            session.status = "failed"
            session.errors.append(str(exc))
            self._log(session, f"Error: {exc}")
        return session

    def _sanitize_origin_url(self, work_dir: str) -> None:
        self._run(["git", "remote", "set-url", "origin", self.repo_url], cwd=work_dir, check=True)

    def _current_head_sha(self, work_dir: str) -> str:
        result = self._run(["git", "rev-parse", "HEAD"], cwd=work_dir, check=True)
        return result.stdout.strip()

    async def _clone_repository(self, session: AgentSession, issue_data: Dict[str, Any]) -> str:
        work_dir = self.working_base / session.session_id
        work_dir.mkdir(parents=True, exist_ok=True)
        session.working_dir = str(work_dir)
        clone_url = self._authenticated_repo_url()
        if issue_data.get("subject_kind") == "pull_request":
            pr = issue_data.get("pr") or {}
            if not pr.get("same_repo"):
                raise NeedsHumanError("Pull request comes from a forked repository; same-repo PRs only are supported.")
            head_ref = pr.get("head_ref", "")
            head_sha = pr.get("head_sha", "")
            base_ref = pr.get("base_ref", "")
            if not head_ref or not head_sha or not base_ref:
                raise NeedsHumanError("Pull request context is incomplete; missing head/base refs or SHA.")

            result = self._run(["git", "clone", "--no-checkout", clone_url, "."], cwd=str(work_dir), timeout=300)
            if result.returncode != 0:
                raise Exception(f"Git clone failed: {result.stderr}")
            self._run(["git", "fetch", "--depth", "1", "origin", head_ref], cwd=str(work_dir), timeout=120, check=True)
            self._run(["git", "checkout", "-B", head_ref, "FETCH_HEAD"], cwd=str(work_dir), timeout=120, check=True)
            self._run(["git", "fetch", "--depth", "1", "origin", base_ref], cwd=str(work_dir), timeout=120, check=True)
            current_sha = self._current_head_sha(str(work_dir))
            if current_sha != head_sha:
                raise NeedsHumanError(
                    f"Pull request head moved before execution start ({head_sha[:12]} -> {current_sha[:12]}). Re-approve on latest SHA."
                )
            self._sanitize_origin_url(str(work_dir))
        else:
            result = self._run(["git", "clone", "--depth", "1", "--branch", self.base_branch, clone_url, "."], cwd=str(work_dir), timeout=300)
            if result.returncode != 0:
                raise Exception(f"Git clone failed: {result.stderr}")
            self._sanitize_origin_url(str(work_dir))
        return str(work_dir)

    def _get_pull_request_changed_files(self, work_dir: Path, issue_data: Dict[str, Any]) -> List[str]:
        pr = issue_data.get("pr") or {}
        base_ref = pr.get("base_ref", "")
        if not base_ref:
            return []
        result = self._run(
            ["git", "diff", "--name-only", f"origin/{base_ref}...HEAD"],
            cwd=str(work_dir),
            timeout=60,
            check=True,
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    async def _list_repo_files(self, work_dir: Path) -> List[str]:
        result = self._run(["git", "ls-files"], cwd=str(work_dir), timeout=60)
        if result.returncode != 0:
            raise Exception(f"Failed to list files: {result.stderr}")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _extract_keywords(self, issue_data: Dict[str, Any]) -> List[str]:
        pr = issue_data.get("pr") or {}
        comment = issue_data.get("comment") or {}
        text = " ".join(
            [
                issue_data.get("title", ""),
                issue_data.get("body", ""),
                pr.get("title", ""),
                pr.get("body", ""),
                comment.get("body", ""),
                " ".join(pr.get("changed_files", []) or []),
            ]
        ).lower()
        keywords = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{3,}", text)
        stop = {"this", "that", "with", "from", "have", "should", "issue", "please", "need", "into"}
        return [k for k in keywords if k not in stop][:25]

    def _score_file(self, file_path: str, keywords: List[str]) -> int:
        path_l = file_path.lower()
        return sum(3 if kw in path_l else 0 for kw in keywords)

    def _select_candidate_files(self, files: List[str], issue_data: Dict[str, Any]) -> List[str]:
        pr_changed_files = (issue_data.get("pr") or {}).get("changed_files") or []
        if issue_data.get("subject_kind") == "pull_request" and pr_changed_files:
            in_scope = [path for path in pr_changed_files if path in set(files)]
            if in_scope:
                files = in_scope
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
        try:
            validate_llm_endpoint(
                self.llm_api_url,
                allowlisted_hosts=self.llm_host_allowlist,
                no_proxy_hosts=self.no_proxy_hosts,
            )
        except NetworkPolicyError as exc:
            raise NeedsHumanError(f"LLM endpoint blocked by network policy: {exc}") from exc
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

    def _extract_json_object(self, text: str, response_name: str) -> Dict[str, Any]:
        text = text.strip()
        # Support fenced JSON responses.
        if "```" in text:
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
            if match:
                text = match.group(1)
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise NeedsHumanError(f"{response_name} did not contain a JSON object.")
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise NeedsHumanError(f"{response_name} was not valid JSON: {exc.msg}.") from exc
        if not isinstance(parsed, dict):
            raise NeedsHumanError(f"{response_name} must be a JSON object.")
        return parsed

    def _validate_plan_response(self, payload: Dict[str, Any]) -> Dict[str, str]:
        summary = payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise NeedsHumanError("LLM plan response must include a non-empty summary.")

        validated: Dict[str, str] = {"summary": summary.strip()}
        for field_name in ("rationale", "risk_level"):
            value = payload.get(field_name)
            if value is None:
                continue
            if not isinstance(value, str):
                raise NeedsHumanError(f"LLM plan response field '{field_name}' must be a string.")
            cleaned = value.strip()
            if cleaned:
                validated[field_name] = cleaned
        return validated

    def _validate_edit_spec(self, edit: Dict[str, Any], index: int) -> Dict[str, str]:
        if not isinstance(edit, dict):
            raise NeedsHumanError(f"LLM edit #{index} must be an object.")

        action = edit.get("action")
        if not isinstance(action, str) or not action.strip():
            raise NeedsHumanError(f"LLM edit #{index} must include a non-empty string action.")

        normalized_action = action.strip().lower()
        if normalized_action not in {"overwrite", "append", "replace"}:
            raise NeedsHumanError(f"LLM edit #{index} uses unsupported action '{action}'.")

        allowed_fields = {
            "overwrite": {"path", "action", "content"},
            "append": {"path", "action", "content"},
            "replace": {"path", "action", "find", "replace"},
        }[normalized_action]
        unknown_fields = sorted(set(edit.keys()) - allowed_fields)
        if unknown_fields:
            raise NeedsHumanError(
                f"LLM edit #{index} includes unsupported fields: {', '.join(unknown_fields)}."
            )

        path = edit.get("path")
        if not isinstance(path, str) or not path.strip():
            raise NeedsHumanError(f"LLM edit #{index} must include a non-empty string path.")
        try:
            normalized_path = self._validate_rel_path(path)
        except Exception as exc:
            raise NeedsHumanError(f"LLM edit #{index} has an invalid path: {exc}") from exc

        validated = {"path": normalized_path, "action": normalized_action}
        if normalized_action in {"overwrite", "append"}:
            content = edit.get("content")
            if not isinstance(content, str):
                raise NeedsHumanError(
                    f"LLM edit #{index} action '{normalized_action}' requires a string content field."
                )
            validated["content"] = content
            return validated

        find = edit.get("find")
        replace = edit.get("replace")
        if not isinstance(find, str) or not find:
            raise NeedsHumanError("LLM replace edit must include a non-empty string find field.")
        if not isinstance(replace, str):
            raise NeedsHumanError("LLM replace edit must include a string replace field.")
        validated["find"] = find
        validated["replace"] = replace
        return validated

    def _validate_edit_response(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        unknown_top_level = sorted(set(payload.keys()) - {"summary", "edits"})
        if unknown_top_level:
            raise NeedsHumanError(
                "LLM edit response includes unsupported top-level keys: " + ", ".join(unknown_top_level) + "."
            )

        summary = payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise NeedsHumanError("LLM edit response must include a non-empty summary.")

        edits = payload.get("edits")
        if not isinstance(edits, list) or not edits:
            raise NeedsHumanError("LLM edit response must include a non-empty edits list.")
        if len(edits) > self.max_edit_actions:
            raise NeedsHumanError(
                f"LLM edit response exceeds AGENT_MAX_EDIT_ACTIONS ({len(edits)} > {self.max_edit_actions})."
            )

        validated_edits = [self._validate_edit_spec(edit, index) for index, edit in enumerate(edits, start=1)]
        return {"summary": summary.strip(), "edits": validated_edits}

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
        subject_header = f"Issue #{issue_data['issue_number']}"
        context_block = ""
        if issue_data.get("subject_kind") == "pull_request":
            pr = issue_data.get("pr") or {}
            comment = issue_data.get("comment") or {}
            subject_header = f"Pull request #{pr.get('number', issue_data['issue_number'])}"
            context_block = (
                f"\nPull request title: {pr.get('title', issue_data.get('title', ''))}\n"
                f"Pull request body:\n{pr.get('body', '')}\n\n"
                f"Triggering comment:\n{comment.get('body', issue_data.get('body', ''))}\n\n"
                f"PR head/base: {pr.get('head_ref', '')}@{pr.get('head_sha', '')[:12]} -> {pr.get('base_ref', '')}\n"
                f"PR changed files:\n" + "\n".join((pr.get("changed_files") or [])[:40]) + "\n\n"
            )
        user_prompt = (
            f"{subject_header}\n"
            f"Title: {issue_data.get('title','')}\n"
            f"Body:\n{issue_data.get('body','')}\n\n"
            f"{context_block}"
            f"Candidate files:\n" + "\n".join(candidates[:30]) + "\n\n"
            f"Snippets:\n{chr(10).join(snippets[:8])}"
        )
        response_text = await self._call_llm(system_prompt, user_prompt)
        parsed = self._validate_plan_response(self._extract_json_object(response_text, "LLM plan response"))
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
            "summary, edits. "
            "Each edit item: {path, action, content/find/replace}. "
            "Allowed actions: overwrite, append, replace."
        )
        snippets_blob = "\n\n".join(
            [f"## {path}\n{snippet}" for path, snippet in file_snippets.items() if snippet.strip()]
        )
        pr_context = ""
        if issue_data.get("subject_kind") == "pull_request":
            pr = issue_data.get("pr") or {}
            comment = issue_data.get("comment") or {}
            pr_context = (
                f"Pull request title: {pr.get('title', issue_data.get('title', ''))}\n"
                f"Pull request body:\n{pr.get('body', '')}\n\n"
                f"Triggering comment:\n{comment.get('body', issue_data.get('body', ''))}\n\n"
                f"Only edit files already changed in this PR unless you cannot proceed safely.\n"
                f"Changed files:\n" + "\n".join((pr.get("changed_files") or [])[:50]) + "\n\n"
            )
        user_prompt = (
            f"{'Pull request' if issue_data.get('subject_kind') == 'pull_request' else 'Issue'} #{issue_data['issue_number']}\n"
            f"Title: {issue_data.get('title','')}\n"
            f"Body:\n{issue_data.get('body','')}\n\n"
            f"{pr_context}"
            f"Plan summary:\n{plan}\n\n"
            f"Candidate files:\n" + "\n".join(candidate_files[:40]) + "\n\n"
            f"File snippets:\n{snippets_blob}\n\n"
            "Return minimal edits to satisfy the issue."
        )
        response_text = await self._call_llm(system_prompt, user_prompt)
        payload = self._extract_json_object(response_text, "LLM edit response")
        return self._validate_edit_response(payload)

    async def _implement_changes(self, session: AgentSession, plan: str, issue_data: Dict[str, Any]) -> List[str]:
        work_dir = Path(session.working_dir)
        files = await self._list_repo_files(work_dir)
        candidates = self._select_candidate_files(files, issue_data)
        file_snippets = {path: self._read_file_snippet(work_dir, path, max_lines=120) for path in candidates[:12]}

        llm_edits = await self._request_edits_from_llm(issue_data, plan, candidates, file_snippets)
        edits = llm_edits["edits"]

        touched = []
        for edit in edits:
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

    def _enforce_change_policies(self, session: AgentSession, changed_files: List[str], issue_data: Dict[str, Any]) -> None:
        if len(changed_files) > self.max_changed_files:
            raise Exception(f"Policy violation: changed files {len(changed_files)} > {self.max_changed_files}")
        for rel_path in changed_files:
            self._validate_rel_path(rel_path)
        if issue_data.get("subject_kind") == "pull_request":
            allowed_pr_files = set((issue_data.get("pr") or {}).get("changed_files") or [])
            if not allowed_pr_files:
                raise NeedsHumanError("Pull request changed-file context is unavailable; cannot enforce safe edit scope.")
            out_of_scope = sorted(path for path in changed_files if path not in allowed_pr_files)
            if out_of_scope:
                raise NeedsHumanError(
                    "Pull request task would modify files outside the reviewed diff: " + ", ".join(out_of_scope[:10])
                )
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
            result = self._run(
                cmd,
                cwd=str(work_dir),
                shell=True,
                timeout=self.quality_timeout_seconds,
                env=self._subprocess_env(include_write_token=False),
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                stdout = (result.stdout or "").strip()
                output_preview = (stderr or stdout)[-1200:]
                raise Exception(f"Quality gate failed: {cmd}\n{output_preview}")

    def _push_url(self) -> str:
        return self._authenticated_repo_url()

    def _ensure_pull_request_head_unchanged(self, work_dir: str, issue_data: Dict[str, Any]) -> None:
        pr = issue_data.get("pr") or {}
        head_ref = pr.get("head_ref", "")
        expected_head_sha = pr.get("head_sha", "")
        if not head_ref or not expected_head_sha:
            raise NeedsHumanError("Pull request head context is incomplete; cannot validate branch freshness.")
        fetch_result = self._run(
            ["git", "fetch", "--depth", "1", self._push_url(), head_ref],
            cwd=work_dir,
            timeout=120,
        )
        if fetch_result.returncode != 0:
            raise Exception(f"Failed to refresh pull request head: {fetch_result.stderr}")
        remote_head = self._run(["git", "rev-parse", "FETCH_HEAD"], cwd=work_dir, check=True).stdout.strip()
        if remote_head != expected_head_sha:
            raise NeedsHumanError(
                f"Pull request head changed from approved SHA {expected_head_sha[:12]} to {remote_head[:12]}."
            )

    async def _create_commit(self, session: AgentSession, plan: str, issue_data: Dict[str, Any]) -> Dict[str, Any]:
        work_dir = session.working_dir
        self._run(["git", "config", "user.email", "agent@github.local"], cwd=work_dir, check=True)
        self._run(["git", "config", "user.name", "AI Agent"], cwd=work_dir, check=True)
        if issue_data.get("subject_kind") == "pull_request":
            pr = issue_data.get("pr") or {}
            branch_name = pr.get("head_ref", "")
            if not branch_name:
                raise NeedsHumanError("Pull request head branch is missing.")
            checkout = self._run(["git", "checkout", branch_name], cwd=work_dir)
            if checkout.returncode != 0:
                raise Exception(f"Git checkout failed: {checkout.stderr}")
        else:
            branch_name = f"agent/{issue_data['issue_number']}"
            checkout = self._run(["git", "checkout", "-b", branch_name], cwd=work_dir)
            if checkout.returncode != 0:
                raise Exception(f"Git checkout failed: {checkout.stderr}")

        add = self._run(["git", "add", "."], cwd=work_dir)
        if add.returncode != 0:
            raise Exception(f"Git add failed: {add.stderr}")

        commit_prefix = "Agent update" if issue_data.get("subject_kind") == "pull_request" else "Agent fix"
        commit_msg = f"{commit_prefix}: Issue #{issue_data['issue_number']}\n\n{plan[:500]}"
        result = self._run(["git", "commit", "-m", commit_msg], cwd=work_dir)
        if result.returncode != 0:
            if "nothing to commit" in (result.stderr + result.stdout):
                return {"success": True, "created_commit": False, "message": "No changes needed"}
            raise Exception(f"Git commit failed: {result.stderr}")

        if issue_data.get("subject_kind") == "pull_request":
            self._ensure_pull_request_head_unchanged(work_dir, issue_data)
            push = self._run(["git", "push", self._push_url(), f"HEAD:{branch_name}"], cwd=work_dir, env=self._subprocess_env(include_write_token=True))
        else:
            push = self._run(
                ["git", "push", self._push_url(), f"HEAD:refs/heads/{branch_name}"],
                cwd=work_dir,
                env=self._subprocess_env(include_write_token=True),
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

    async def _comment_on_pull_request(self, issue_data: Dict[str, Any], plan: str, changed_files: List[str]) -> Dict[str, Any]:
        pr = issue_data.get("pr") or {}
        pr_number = pr.get("number")
        if not pr_number:
            raise NeedsHumanError("Pull request number missing; cannot publish update.")
        if not self.github_token:
            raise Exception("GITHUB_TOKEN is required to publish pull request updates.")
        owner, repo = self._owner_repo()
        url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
        headers = {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        changed_blob = "\n".join(f"- `{path}`" for path in changed_files[:15]) or "- no tracked file changes"
        body = (
            f"AI agent updated this pull request branch.\n\n"
            f"Task: `{issue_data.get('task_id', '')}`\n\n"
            f"Summary:\n{plan}\n\n"
            f"Changed files:\n{changed_blob}"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=headers, json={"body": body})
            if response.status_code not in (200, 201):
                print(f"Warning: failed to comment on pull request #{pr_number}: {response.status_code} {response.text[:200]}")
        return {"number": pr_number, "url": pr.get("html_url")}

    async def _publish_changes(self, session: AgentSession, plan: str, issue_data: Dict[str, Any]) -> Dict[str, Any]:
        if issue_data.get("subject_kind") == "pull_request":
            return await self._comment_on_pull_request(issue_data, plan, session.changed_files)
        return await self._create_pull_request(session, plan, issue_data)
