"""
GitHub Webhook Handler for Agentic Developer System.
Receives issue/PR events, queues them, and runs an agent after approval.
"""

import hashlib
import hmac
import os
import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from agent_orchestrator import AgentOrchestrator

# Configuration
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
ADMIN_API_TOKEN = os.environ.get("ADMIN_API_TOKEN", "")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()
ALLOWED_REPOS_RAW = os.environ.get("ALLOWED_REPOS", "").strip()

app = FastAPI(title="Agent Webhook Handler")

AGENT_RUN_LOCK = asyncio.Lock()
redis_client: Redis | None = None

ISSUE_KEY_PREFIX = "issue:"
SESSION_KEY_PREFIX = "session:"
ISSUE_INDEX_KEY = "issues:index"
SESSION_INDEX_KEY = "sessions:index"


def parse_allowlist(raw_value: str) -> set[str]:
    entries = [item.strip().lower() for item in raw_value.split(",")]
    return {entry for entry in entries if entry}


ALLOWLIST = parse_allowlist(ALLOWED_REPOS_RAW)
if not ALLOWLIST and GITHUB_OWNER and GITHUB_REPO:
    ALLOWLIST = {f"{GITHUB_OWNER.lower()}/{GITHUB_REPO.lower()}"}


def build_queue_key(repo_full_name: str, issue_number: int) -> str:
    repo_token = repo_full_name.replace("/", ":")
    return f"{repo_token}:{issue_number}"


def is_repo_allowed(repo_full_name: str) -> bool:
    if not ALLOWLIST:
        return True
    return repo_full_name.lower() in ALLOWLIST


async def get_redis() -> Redis:
    global redis_client
    if redis_client is None:
        redis_client = Redis.from_url(REDIS_URL, decode_responses=True)
    return redis_client


def _issue_storage_key(queue_key: str) -> str:
    return f"{ISSUE_KEY_PREFIX}{queue_key}"


def _session_storage_key(session_id: str) -> str:
    return f"{SESSION_KEY_PREFIX}{session_id}"


async def store_issue(issue: Dict[str, Any]) -> None:
    redis = await get_redis()
    queue_key = issue["queue_key"]
    await redis.set(_issue_storage_key(queue_key), json.dumps(issue))
    await redis.zadd(ISSUE_INDEX_KEY, {queue_key: datetime.utcnow().timestamp()})


async def load_issue(queue_key: str) -> Dict[str, Any] | None:
    redis = await get_redis()
    data = await redis.get(_issue_storage_key(queue_key))
    return json.loads(data) if data else None


async def list_issues() -> List[Dict[str, Any]]:
    redis = await get_redis()
    queue_keys = await redis.zrevrange(ISSUE_INDEX_KEY, 0, -1)
    if not queue_keys:
        return []
    values = await redis.mget([_issue_storage_key(k) for k in queue_keys])
    issues: List[Dict[str, Any]] = []
    for value in values:
        if value:
            issues.append(json.loads(value))
    return issues


async def store_session(session_data: Dict[str, Any], created_at: str) -> None:
    redis = await get_redis()
    session_id = session_data["session_id"]
    await redis.set(_session_storage_key(session_id), json.dumps(session_data))
    timestamp = datetime.fromisoformat(created_at).timestamp()
    await redis.zadd(SESSION_INDEX_KEY, {session_id: timestamp})


async def list_sessions() -> List[Dict[str, Any]]:
    redis = await get_redis()
    session_ids = await redis.zrevrange(SESSION_INDEX_KEY, 0, -1)
    if not session_ids:
        return []
    values = await redis.mget([_session_storage_key(s) for s in session_ids])
    sessions: List[Dict[str, Any]] = []
    for value in values:
        if value:
            sessions.append(json.loads(value))
    return sessions


async def verify_github_signature(request: Request, signature: str) -> bool:
    """Verify GitHub webhook signature using HMAC SHA-256."""
    if not WEBHOOK_SECRET:
        return True
    if not signature:
        return False
    payload = await request.body()
    digest = hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(signature, expected)


async def notify_slack(event_type: str, issue_number: int, title: str) -> None:
    """Optional Slack notification for review queue updates."""
    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not slack_webhook:
        return

    message = {
        "text": f"New GitHub event: #{issue_number} - {title}",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":robot_face: Event `{event_type}` queued for issue/PR #{issue_number}\n"
                        f"*Title:* {title}\n"
                        "Use the approval API before agent execution."
                    ),
                },
            }
        ],
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            await client.post(slack_webhook, json=message)
        except Exception as exc:
            print(f"Slack notification failed: {exc}")


def build_repo_url(repo_full_name: str, clone_url: str = "") -> str:
    if clone_url:
        return clone_url
    return f"https://github.com/{repo_full_name}.git"


async def run_agent_for_issue(queue_key: str) -> None:
    """Run orchestrator for a queued issue."""
    async with AGENT_RUN_LOCK:
        issue = await load_issue(queue_key)
        if not issue:
            return
        if issue.get("status") in {"processing", "completed"}:
            return

        issue["status"] = "processing"
        issue["started_at"] = datetime.utcnow().isoformat()
        await store_issue(issue)
        repo_url = build_repo_url(issue["repo_full_name"], issue.get("repo_clone_url", ""))
        orchestrator = AgentOrchestrator(
            github_token=GITHUB_TOKEN,
            repo_url=repo_url,
            working_base="/app/workspace",
        )
        session = await orchestrator.process_issue(
            {
                "issue_number": issue["issue_number"],
                "title": issue["title"],
                "body": issue["body"],
                "is_pr": issue["is_pr"],
                "repo_name": issue["repo_full_name"],
            }
        )
        await store_session(session.to_dict(), session.created_at)
        issue["assigned_agent"] = session.session_id
        issue["completed_at"] = datetime.utcnow().isoformat()
        issue["status"] = "completed" if session.status == "completed" else "failed"
        issue["output_pr"] = {
            "number": session.output_pr_number,
            "url": session.output_pr_url,
        }
        if session.errors:
            issue["errors"] = session.errors
        await store_issue(issue)


def verify_admin_token(header_value: str | None) -> None:
    if not ADMIN_API_TOKEN:
        return
    if not header_value or not hmac.compare_digest(header_value, ADMIN_API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid admin token")


@app.get("/health")
async def health() -> Dict[str, str]:
    redis = await get_redis()
    await redis.ping()
    return {"status": "ok"}


@app.on_event("startup")
async def startup_event() -> None:
    redis = await get_redis()
    await redis.ping()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    global redis_client
    if redis_client is not None:
        await redis_client.aclose()
        redis_client = None


@app.post("/webhook/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    GitHub webhook endpoint.
    Triggers: issues, pull_request, issue_comment, pull_request_review, pull_request_review_comment
    """
    github_event = request.headers.get("X-GitHub-Event", "")
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not await verify_github_signature(request, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()
    repo_full_name = payload.get("repository", {}).get("full_name", "").strip()
    if repo_full_name and not is_repo_allowed(repo_full_name):
        print(f"Ignoring webhook from non-allowlisted repo: {repo_full_name}")
        return JSONResponse(
            {"status": "ignored", "reason": "repo_not_allowlisted", "repo": repo_full_name},
            status_code=202,
        )

    supported = {
        "issues",
        "pull_request",
        "issue_comment",
        "pull_request_review",
        "pull_request_review_comment",
    }
    if github_event in supported:
        background_tasks.add_task(process_issue_event, github_event, payload)

    return JSONResponse({"status": "received"})


async def process_issue_event(event_type: str, payload: Dict[str, Any]) -> None:
    """Process incoming GitHub events and store queue item."""
    try:
        repo = payload.get("repository", {})
        repo_full_name = repo.get("full_name", "unknown/unknown")
        if not is_repo_allowed(repo_full_name):
            print(f"Skipping event for non-allowlisted repo: {repo_full_name}")
            return
        repo_clone_url = repo.get("clone_url", "")
        sender = payload.get("sender", {}).get("login", "unknown")

        issue_number = None
        title = ""
        body = ""
        is_pr = False

        if event_type == "issues":
            issue = payload.get("issue", {})
            issue_number = issue.get("number")
            title = issue.get("title", "")
            body = issue.get("body", "") or ""
            is_pr = False
        elif event_type == "pull_request":
            pr = payload.get("pull_request", {})
            issue_number = pr.get("number")
            title = pr.get("title", "")
            body = pr.get("body", "") or ""
            is_pr = True
        elif event_type == "issue_comment":
            issue = payload.get("issue", {})
            comment = payload.get("comment", {})
            issue_number = issue.get("number")
            title = issue.get("title", "")
            body = comment.get("body", "") or ""
            is_pr = bool(issue.get("pull_request"))
        elif event_type in {"pull_request_review", "pull_request_review_comment"}:
            pr = payload.get("pull_request", {})
            review = payload.get("review", {}) if event_type == "pull_request_review" else payload.get("comment", {})
            issue_number = pr.get("number")
            title = pr.get("title", "")
            body = review.get("body", "") or ""
            is_pr = True

        if issue_number is None:
            return

        trigger_keywords = ["@agent", "@ai", "fix this", "implement", "create"]
        trigger_text = f"{title}\n{body}".lower()
        trigger_type = "auto" if any(k in trigger_text for k in trigger_keywords) else "manual"

        queue_key = build_queue_key(repo_full_name, issue_number)
        issue = {
            "queue_key": queue_key,
            "event_type": event_type,
            "issue_number": issue_number,
            "title": title,
            "body": body,
            "is_pr": is_pr,
            "trigger_type": trigger_type,
            "status": "queued",
            "sender": sender,
            "repo_full_name": repo_full_name,
            "repo_clone_url": repo_clone_url,
            "created_at": datetime.utcnow().isoformat(),
            "assigned_agent": None,
            "output_pr": None,
        }
        await store_issue(issue)
        print(f"Queued {queue_key} ({event_type}, trigger={trigger_type})")

        if trigger_type == "auto":
            await notify_slack(event_type, issue_number, title)
    except Exception as exc:
        print(f"Error processing event: {exc}")


@app.get("/api/issues")
async def get_queue():
    """Get all queued issues."""
    return {"issues": await list_issues()}


@app.get("/api/issues/{queue_key}")
async def get_issue(queue_key: str):
    """Get specific queued issue details."""
    issue = await load_issue(queue_key)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    return issue


@app.post("/api/issues/{queue_key}/approve")
async def approve_issue(
    queue_key: str,
    background_tasks: BackgroundTasks,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    """Approve an issue and start agent processing."""
    verify_admin_token(x_admin_token)
    issue = await load_issue(queue_key)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    issue["status"] = "approved"
    issue["approved_at"] = datetime.utcnow().isoformat()
    await store_issue(issue)
    background_tasks.add_task(run_agent_for_issue, queue_key)
    return {"status": "approved", "issue": issue}


@app.post("/api/issues/{queue_key}/reject")
async def reject_issue(
    queue_key: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    """Reject an issue (no agent processing)."""
    verify_admin_token(x_admin_token)
    issue = await load_issue(queue_key)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    issue["status"] = "rejected"
    issue["rejected_at"] = datetime.utcnow().isoformat()
    await store_issue(issue)
    return {"status": "rejected", "issue": issue}


@app.get("/api/sessions")
async def get_sessions():
    """Get completed/active agent sessions."""
    return {"sessions": await list_sessions()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
