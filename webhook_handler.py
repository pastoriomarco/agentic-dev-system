"""
GitHub Webhook Handler for Agentic Developer System.
Receives issue/PR events, queues them, and runs an agent after approval.
"""

import hashlib
import hmac
import os
from datetime import datetime
from typing import Any, Dict

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from agent_orchestrator import AgentOrchestrator

# Configuration
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

app = FastAPI(title="Agent Webhook Handler")

# In-memory MVP storage (replace with Redis/DB for production)
issue_queue: Dict[str, Dict[str, Any]] = {}
agent_sessions: Dict[str, Dict[str, Any]] = {}


def build_queue_key(repo_full_name: str, issue_number: int) -> str:
    repo_token = repo_full_name.replace("/", ":")
    return f"{repo_token}:{issue_number}"


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
    issue = issue_queue.get(queue_key)
    if not issue:
        return
    if issue.get("status") in {"processing", "completed"}:
        return

    issue["status"] = "processing"
    issue["started_at"] = datetime.utcnow().isoformat()
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
    agent_sessions[session.session_id] = session.to_dict()
    issue["assigned_agent"] = session.session_id
    issue["completed_at"] = datetime.utcnow().isoformat()
    issue["status"] = "completed" if session.status == "completed" else "failed"
    issue["output_pr"] = {
        "number": session.output_pr_number,
        "url": session.output_pr_url,
    }
    if session.errors:
        issue["errors"] = session.errors


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


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
        issue_queue[queue_key] = {
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
        print(f"Queued {queue_key} ({event_type}, trigger={trigger_type})")

        if trigger_type == "auto":
            await notify_slack(event_type, issue_number, title)
    except Exception as exc:
        print(f"Error processing event: {exc}")


@app.get("/api/issues")
async def get_queue():
    """Get all queued issues."""
    return {"issues": list(issue_queue.values())}


@app.get("/api/issues/{queue_key}")
async def get_issue(queue_key: str):
    """Get specific queued issue details."""
    if queue_key not in issue_queue:
        raise HTTPException(status_code=404, detail="Issue not found")
    return issue_queue[queue_key]


@app.post("/api/issues/{queue_key}/approve")
async def approve_issue(queue_key: str, background_tasks: BackgroundTasks):
    """Approve an issue and start agent processing."""
    if queue_key not in issue_queue:
        raise HTTPException(status_code=404, detail="Issue not found")

    issue = issue_queue[queue_key]
    issue["status"] = "approved"
    issue["approved_at"] = datetime.utcnow().isoformat()
    background_tasks.add_task(run_agent_for_issue, queue_key)
    return {"status": "approved", "issue": issue}


@app.post("/api/issues/{queue_key}/reject")
async def reject_issue(queue_key: str):
    """Reject an issue (no agent processing)."""
    if queue_key not in issue_queue:
        raise HTTPException(status_code=404, detail="Issue not found")

    issue = issue_queue[queue_key]
    issue["status"] = "rejected"
    issue["rejected_at"] = datetime.utcnow().isoformat()
    return {"status": "rejected", "issue": issue}


@app.get("/api/sessions")
async def get_sessions():
    """Get completed/active agent sessions."""
    return {"sessions": list(agent_sessions.values())}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
