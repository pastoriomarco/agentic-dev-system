"""
GitHub Webhook Handler for Agentic Developer System.
Receives issue/PR events, queues them in Redis, and runs each approved task in
an isolated short-lived worker container.
"""

import asyncio
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote

import httpx
from docker import from_env as docker_from_env
from docker.client import DockerClient
from docker.types import Mount
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

# Configuration
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
ADMIN_API_TOKEN = os.environ.get("ADMIN_API_TOKEN", "")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()
ALLOWED_REPOS_RAW = os.environ.get("ALLOWED_REPOS", "").strip()
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
RETRY_BASE_DELAY_SECONDS = int(os.environ.get("RETRY_BASE_DELAY_SECONDS", "60"))
RETRY_MAX_DELAY_SECONDS = int(os.environ.get("RETRY_MAX_DELAY_SECONDS", "1800"))
RETRY_POLL_INTERVAL_SECONDS = int(os.environ.get("RETRY_POLL_INTERVAL_SECONDS", "15"))

WORKER_IMAGE = os.environ.get("WORKER_IMAGE", "agentic-dev-system:latest")
WORKER_NETWORK = os.environ.get("WORKER_NETWORK", "agent_worker_net")
WORKER_TIMEOUT_SECONDS = int(os.environ.get("WORKER_TIMEOUT_SECONDS", "1800"))
WORKER_CPU_LIMIT = float(os.environ.get("WORKER_CPU_LIMIT", "1.0"))
WORKER_MEMORY_LIMIT = os.environ.get("WORKER_MEMORY_LIMIT", "2g")
WORKER_PIDS_LIMIT = int(os.environ.get("WORKER_PIDS_LIMIT", "256"))
WORKER_ARTIFACTS_DIR = Path(os.environ.get("WORKER_ARTIFACTS_DIR", "/worker-artifacts"))
WORKER_ARTIFACTS_VOLUME = os.environ.get("WORKER_ARTIFACTS_VOLUME", "worker-artifacts")
WORKER_VOLUME_PREFIX = os.environ.get("WORKER_VOLUME_PREFIX", "agent-task")
WORKER_HTTP_PROXY = os.environ.get("WORKER_HTTP_PROXY", "http://egress-proxy:3128")
WORKER_HTTPS_PROXY = os.environ.get("WORKER_HTTPS_PROXY", "http://egress-proxy:3128")
WORKER_NO_PROXY = os.environ.get("WORKER_NO_PROXY", "localhost,127.0.0.1,egress-proxy,redis")
DEEP_HEALTH_TIMEOUT_SECONDS = float(os.environ.get("DEEP_HEALTH_TIMEOUT_SECONDS", "8"))
DEEP_HEALTH_GITHUB_URL = os.environ.get("DEEP_HEALTH_GITHUB_URL", "https://api.github.com/meta")
DEEP_HEALTH_CHECK_LLM = os.environ.get("DEEP_HEALTH_CHECK_LLM", "false").lower() == "true"
GITHUB_STATUS_LABELS_ENABLED = os.environ.get("GITHUB_STATUS_LABELS_ENABLED", "true").lower() == "true"
GITHUB_STATUS_COMMENTS_ENABLED = os.environ.get("GITHUB_STATUS_COMMENTS_ENABLED", "true").lower() == "true"
GITHUB_ASSIGN_ON_PROCESSING = os.environ.get("GITHUB_ASSIGN_ON_PROCESSING", "false").lower() == "true"
GITHUB_ASSIGNEE_LOGIN = os.environ.get("GITHUB_ASSIGNEE_LOGIN", "").strip()
LABEL_QUEUED = os.environ.get("GH_LABEL_QUEUED", "agent:queued")
LABEL_IN_PROGRESS = os.environ.get("GH_LABEL_IN_PROGRESS", "agent:in-progress")
LABEL_PR_OPENED = os.environ.get("GH_LABEL_PR_OPENED", "agent:pr-opened")
LABEL_FAILED = os.environ.get("GH_LABEL_FAILED", "agent:failed")
LABEL_DEAD_LETTER = os.environ.get("GH_LABEL_DEAD_LETTER", "agent:dead-letter")
LABEL_REJECTED = os.environ.get("GH_LABEL_REJECTED", "agent:rejected")

app = FastAPI(title="Agent Webhook Handler")

AGENT_RUN_LOCK = asyncio.Lock()
redis_client: Redis | None = None
docker_client: DockerClient | None = None
retry_worker_task: asyncio.Task | None = None
ensured_label_repos: set[str] = set()

ISSUE_KEY_PREFIX = "issue:"
SESSION_KEY_PREFIX = "session:"
ISSUE_INDEX_KEY = "issues:index"
SESSION_INDEX_KEY = "sessions:index"
RETRY_INDEX_KEY = "issues:retry:index"
DEAD_LETTER_INDEX_KEY = "issues:dead_letter:index"

ALL_AGENT_LABELS = [
    LABEL_QUEUED,
    LABEL_IN_PROGRESS,
    LABEL_PR_OPENED,
    LABEL_FAILED,
    LABEL_DEAD_LETTER,
    LABEL_REJECTED,
]
LABEL_COLORS = {
    LABEL_QUEUED: "D4C5F9",
    LABEL_IN_PROGRESS: "0E8A16",
    LABEL_PR_OPENED: "1D76DB",
    LABEL_FAILED: "B60205",
    LABEL_DEAD_LETTER: "5319E7",
    LABEL_REJECTED: "6A737D",
}


def now_utc() -> datetime:
    return datetime.utcnow()


def parse_allowlist(raw_value: str) -> set[str]:
    entries = [item.strip().lower() for item in raw_value.split(",")]
    return {entry for entry in entries if entry}


ALLOWLIST = parse_allowlist(ALLOWED_REPOS_RAW)
if not ALLOWLIST and GITHUB_OWNER and GITHUB_REPO:
    ALLOWLIST = {f"{GITHUB_OWNER.lower()}/{GITHUB_REPO.lower()}"}


def is_repo_allowed(repo_full_name: str) -> bool:
    if not ALLOWLIST:
        return True
    return repo_full_name.lower() in ALLOWLIST


def build_queue_key(repo_full_name: str, issue_number: int) -> str:
    repo_token = repo_full_name.replace("/", ":")
    return f"{repo_token}:{issue_number}"


def build_repo_url(repo_full_name: str, clone_url: str = "") -> str:
    if clone_url:
        return clone_url
    return f"https://github.com/{repo_full_name}.git"


def verify_admin_token(header_value: str | None) -> None:
    if not ADMIN_API_TOKEN:
        return
    if not header_value or not hmac.compare_digest(header_value, ADMIN_API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid admin token")


def compute_retry_delay_seconds(attempt_count: int) -> int:
    delay = RETRY_BASE_DELAY_SECONDS * (2 ** max(attempt_count - 1, 0))
    return min(delay, RETRY_MAX_DELAY_SECONDS)


async def get_redis() -> Redis:
    global redis_client
    if redis_client is None:
        redis_client = Redis.from_url(REDIS_URL, decode_responses=True)
    return redis_client


def get_docker_client() -> DockerClient:
    global docker_client
    if docker_client is None:
        docker_client = docker_from_env()
    return docker_client


def _issue_storage_key(queue_key: str) -> str:
    return f"{ISSUE_KEY_PREFIX}{queue_key}"


def _session_storage_key(session_id: str) -> str:
    return f"{SESSION_KEY_PREFIX}{session_id}"


async def store_issue(issue: Dict[str, Any]) -> None:
    redis = await get_redis()
    queue_key = issue["queue_key"]
    await redis.set(_issue_storage_key(queue_key), json.dumps(issue))
    await redis.zadd(ISSUE_INDEX_KEY, {queue_key: now_utc().timestamp()})
    status = issue.get("status")
    if status == "queued_retry" and issue.get("next_retry_at"):
        retry_ts = datetime.fromisoformat(issue["next_retry_at"]).timestamp()
        await redis.zadd(RETRY_INDEX_KEY, {queue_key: retry_ts})
    else:
        await redis.zrem(RETRY_INDEX_KEY, queue_key)
    if status == "dead_letter":
        await redis.zadd(DEAD_LETTER_INDEX_KEY, {queue_key: now_utc().timestamp()})
    else:
        await redis.zrem(DEAD_LETTER_INDEX_KEY, queue_key)


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
    return [json.loads(v) for v in values if v]


async def list_dead_letters() -> List[Dict[str, Any]]:
    redis = await get_redis()
    queue_keys = await redis.zrevrange(DEAD_LETTER_INDEX_KEY, 0, -1)
    if not queue_keys:
        return []
    values = await redis.mget([_issue_storage_key(k) for k in queue_keys])
    return [json.loads(v) for v in values if v]


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
    return [json.loads(v) for v in values if v]


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


def _split_owner_repo(repo_full_name: str) -> tuple[str, str]:
    parts = repo_full_name.split("/")
    if len(parts) != 2:
        raise Exception(f"Invalid repo full name: {repo_full_name}")
    return parts[0], parts[1]


async def _github_api_request(method: str, repo_full_name: str, path: str, json_body: Dict[str, Any] | None = None) -> httpx.Response:
    owner, repo = _split_owner_repo(repo_full_name)
    url = f"https://api.github.com/repos/{owner}/{repo}{path}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.request(method, url, headers=headers, json=json_body)
    return response


def _status_label_for_issue_status(status: str) -> str:
    mapping = {
        "queued": LABEL_QUEUED,
        "approved": LABEL_QUEUED,
        "processing": LABEL_IN_PROGRESS,
        "completed": LABEL_PR_OPENED,
        "queued_retry": LABEL_FAILED,
        "failed": LABEL_FAILED,
        "dead_letter": LABEL_DEAD_LETTER,
        "rejected": LABEL_REJECTED,
    }
    return mapping.get(status, LABEL_QUEUED)


async def _ensure_repo_labels(repo_full_name: str) -> None:
    if not GITHUB_TOKEN or not GITHUB_STATUS_LABELS_ENABLED:
        return
    if repo_full_name in ensured_label_repos:
        return
    for label_name in ALL_AGENT_LABELS:
        response = await _github_api_request(
            "POST",
            repo_full_name,
            "/labels",
            {"name": label_name, "color": LABEL_COLORS.get(label_name, "6A737D"), "description": "Agent workflow status"},
        )
        # 201 created, 422 already exists; both acceptable.
        if response.status_code not in (201, 422):
            print(f"Warning: failed to ensure label '{label_name}' on {repo_full_name}: {response.status_code} {response.text[:200]}")
    ensured_label_repos.add(repo_full_name)


async def _set_issue_status_label(repo_full_name: str, issue_number: int, status: str) -> None:
    if not GITHUB_TOKEN or not GITHUB_STATUS_LABELS_ENABLED:
        return
    await _ensure_repo_labels(repo_full_name)
    for label in ALL_AGENT_LABELS:
        encoded = quote(label, safe="")
        response = await _github_api_request("DELETE", repo_full_name, f"/issues/{issue_number}/labels/{encoded}")
        if response.status_code not in (200, 404):
            print(f"Warning: failed deleting label {label} on {repo_full_name}#{issue_number}: {response.status_code}")
    target = _status_label_for_issue_status(status)
    response = await _github_api_request("POST", repo_full_name, f"/issues/{issue_number}/labels", {"labels": [target]})
    if response.status_code not in (200, 201):
        print(f"Warning: failed setting label {target} on {repo_full_name}#{issue_number}: {response.status_code} {response.text[:200]}")


async def _post_issue_comment(repo_full_name: str, issue_number: int, body: str) -> None:
    if not GITHUB_TOKEN or not GITHUB_STATUS_COMMENTS_ENABLED:
        return
    response = await _github_api_request("POST", repo_full_name, f"/issues/{issue_number}/comments", {"body": body})
    if response.status_code not in (200, 201):
        print(f"Warning: failed posting comment on {repo_full_name}#{issue_number}: {response.status_code} {response.text[:200]}")


async def _assign_issue(repo_full_name: str, issue_number: int) -> None:
    if not GITHUB_TOKEN or not GITHUB_ASSIGN_ON_PROCESSING or not GITHUB_ASSIGNEE_LOGIN:
        return
    response = await _github_api_request(
        "POST",
        repo_full_name,
        f"/issues/{issue_number}/assignees",
        {"assignees": [GITHUB_ASSIGNEE_LOGIN]},
    )
    if response.status_code not in (200, 201):
        print(f"Warning: failed assigning {repo_full_name}#{issue_number} to {GITHUB_ASSIGNEE_LOGIN}: {response.status_code} {response.text[:200]}")


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


def _run_worker_container(issue: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
    """
    Run a one-off worker container and return (session_payload, logs).
    This function runs in a thread via asyncio.to_thread.
    """
    client = get_docker_client()
    job_id = uuid.uuid4().hex
    workspace_volume_name = f"{WORKER_VOLUME_PREFIX}-ws-{job_id}"
    artifact_host_path = WORKER_ARTIFACTS_DIR / f"{job_id}.json"
    artifact_container_path = f"/artifacts/{job_id}.json"
    repo_url = build_repo_url(issue["repo_full_name"], issue.get("repo_clone_url", ""))

    session_payload: Dict[str, Any] = {
        "session_id": "",
        "status": "failed",
        "created_at": now_utc().isoformat(),
        "started_at": now_utc().isoformat(),
        "completed_at": now_utc().isoformat(),
        "errors": [],
        "logs": [],
    }
    logs_text = ""
    container = None
    workspace_volume = None

    try:
        if artifact_host_path.exists():
            artifact_host_path.unlink()
        workspace_volume = client.volumes.create(name=workspace_volume_name)
        mounts = [
            Mount(target="/workspace", source=workspace_volume_name, type="volume", read_only=False),
            Mount(target="/artifacts", source=WORKER_ARTIFACTS_VOLUME, type="volume", read_only=False),
        ]
        env = {
            "ISSUE_JSON": json.dumps(
                {
                    "issue_number": issue["issue_number"],
                    "title": issue["title"],
                    "body": issue["body"],
                    "is_pr": issue["is_pr"],
                    "repo_name": issue["repo_full_name"],
                }
            ),
            "OUTPUT_PATH": artifact_container_path,
            "GITHUB_TOKEN": GITHUB_TOKEN,
            "TARGET_REPO_URL": repo_url,
            "GITHUB_BASE_BRANCH": os.environ.get("GITHUB_BASE_BRANCH", "main"),
            "LLM_API_URL": os.environ.get("LLM_API_URL", ""),
            "LLM_MODEL": os.environ.get("LLM_MODEL", ""),
            "HTTP_PROXY": WORKER_HTTP_PROXY,
            "HTTPS_PROXY": WORKER_HTTPS_PROXY,
            "ALL_PROXY": WORKER_HTTPS_PROXY,
            "NO_PROXY": WORKER_NO_PROXY,
            "http_proxy": WORKER_HTTP_PROXY,
            "https_proxy": WORKER_HTTPS_PROXY,
            "all_proxy": WORKER_HTTPS_PROXY,
            "no_proxy": WORKER_NO_PROXY,
        }

        container = client.containers.run(
            WORKER_IMAGE,
            command=["python", "worker_entrypoint.py"],
            environment=env,
            mounts=mounts,
            network=WORKER_NETWORK,
            detach=True,
            user="0:0",
            read_only=True,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=256m"},
            mem_limit=WORKER_MEMORY_LIMIT,
            nano_cpus=int(WORKER_CPU_LIMIT * 1_000_000_000),
            pids_limit=WORKER_PIDS_LIMIT,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
        )
        container.wait(timeout=WORKER_TIMEOUT_SECONDS)
        logs_text = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")

        if artifact_host_path.exists():
            session_payload = json.loads(artifact_host_path.read_text(encoding="utf-8"))
        else:
            session_payload["errors"] = ["Worker completed without producing session artifact."]

    except Exception as exc:
        session_payload["errors"] = [f"Worker container execution failed: {exc}"]
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass
        if workspace_volume is not None:
            try:
                workspace_volume.remove(force=True)
            except Exception:
                pass

    return session_payload, logs_text


async def run_agent_for_issue(queue_key: str) -> None:
    """Run orchestrator for a queued issue inside isolated worker container."""
    async with AGENT_RUN_LOCK:
        issue = await load_issue(queue_key)
        if not issue:
            return
        if issue.get("status") in {"processing", "completed"}:
            return

        issue["status"] = "processing"
        issue["started_at"] = now_utc().isoformat()
        issue["attempt_count"] = int(issue.get("attempt_count", 0)) + 1
        await store_issue(issue)
        await _set_issue_status_label(issue["repo_full_name"], issue["issue_number"], issue["status"])
        await _assign_issue(issue["repo_full_name"], issue["issue_number"])
        await _post_issue_comment(
            issue["repo_full_name"],
            issue["issue_number"],
            f"Agent started processing (attempt {issue['attempt_count']}). Session pending...",
        )

        session_data, worker_logs = await asyncio.to_thread(_run_worker_container, issue)
        session_data.setdefault("session_id", str(uuid.uuid4()))
        session_data.setdefault("created_at", now_utc().isoformat())
        session_data.setdefault("status", "failed")
        if worker_logs:
            logs = session_data.get("logs") or []
            logs.append(worker_logs[-4000:])
            session_data["logs"] = logs

        await store_session(session_data, session_data["created_at"])
        issue["assigned_agent"] = session_data["session_id"]
        issue["completed_at"] = now_utc().isoformat()
        issue["output_pr"] = {
            "number": session_data.get("output_pr_number"),
            "url": session_data.get("output_pr_url"),
        }
        if session_data.get("status") == "completed":
            issue["status"] = "completed"
            issue.pop("next_retry_at", None)
            pr_url = issue.get("output_pr", {}).get("url")
            if pr_url:
                await _post_issue_comment(
                    issue["repo_full_name"],
                    issue["issue_number"],
                    f"Agent completed successfully. PR created: {pr_url}",
                )
            else:
                await _post_issue_comment(
                    issue["repo_full_name"],
                    issue["issue_number"],
                    "Agent completed successfully.",
                )
        else:
            errors = session_data.get("errors") or ["Agent run failed without explicit error message."]
            issue["errors"] = errors
            issue["last_error"] = errors[-1]
            attempts_used = int(issue.get("attempt_count", 1))
            retries_used = max(attempts_used - 1, 0)
            if retries_used < MAX_RETRIES:
                delay_seconds = compute_retry_delay_seconds(attempts_used)
                retry_at = now_utc() + timedelta(seconds=delay_seconds)
                issue["status"] = "queued_retry"
                issue["next_retry_at"] = retry_at.isoformat()
                await _post_issue_comment(
                    issue["repo_full_name"],
                    issue["issue_number"],
                    f"Agent attempt {attempts_used} failed. Scheduled retry at {issue['next_retry_at']}. "
                    f"Last error: {issue['last_error']}",
                )
            else:
                issue["status"] = "dead_letter"
                issue["dead_lettered_at"] = now_utc().isoformat()
                issue.pop("next_retry_at", None)
                await _post_issue_comment(
                    issue["repo_full_name"],
                    issue["issue_number"],
                    f"Agent moved this item to dead-letter after {attempts_used} attempts. "
                    f"Last error: {issue['last_error']}",
                )
        await _set_issue_status_label(issue["repo_full_name"], issue["issue_number"], issue["status"])
        await store_issue(issue)


async def retry_worker_loop() -> None:
    while True:
        try:
            redis = await get_redis()
            now_ts = now_utc().timestamp()
            due_keys = await redis.zrangebyscore(RETRY_INDEX_KEY, 0, now_ts)
            for queue_key in due_keys:
                issue = await load_issue(queue_key)
                if not issue:
                    await redis.zrem(RETRY_INDEX_KEY, queue_key)
                    continue
                if issue.get("status") != "queued_retry":
                    await redis.zrem(RETRY_INDEX_KEY, queue_key)
                    continue
                issue["status"] = "approved"
                issue["retried_at"] = now_utc().isoformat()
                issue.pop("next_retry_at", None)
                await store_issue(issue)
                await _set_issue_status_label(issue["repo_full_name"], issue["issue_number"], issue["status"])
                await _post_issue_comment(
                    issue["repo_full_name"],
                    issue["issue_number"],
                    f"Automatic retry triggered at {issue['retried_at']}.",
                )
                asyncio.create_task(run_agent_for_issue(queue_key))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Retry worker error: {exc}")
        await asyncio.sleep(RETRY_POLL_INTERVAL_SECONDS)


@app.get("/health")
async def health() -> Dict[str, str]:
    redis = await get_redis()
    await redis.ping()
    await asyncio.to_thread(get_docker_client().ping)
    return {"status": "ok"}


async def _check_http_url(
    name: str,
    url: str,
    use_proxy: bool = False,
    required: bool = True,
) -> Dict[str, Any]:
    proxies = None
    if use_proxy:
        proxies = {"http://": WORKER_HTTP_PROXY, "https://": WORKER_HTTPS_PROXY}
    try:
        async with httpx.AsyncClient(timeout=DEEP_HEALTH_TIMEOUT_SECONDS, proxies=proxies) as client:
            response = await client.get(url)
        return {"name": name, "ok": response.status_code < 500, "status_code": response.status_code, "url": url}
    except Exception as exc:
        return {"name": name, "ok": not required, "error": str(exc), "url": url, "required": required}


@app.get("/health/deep")
async def health_deep() -> JSONResponse:
    checks: List[Dict[str, Any]] = []

    # Core dependencies.
    try:
        redis = await get_redis()
        await redis.ping()
        checks.append({"name": "redis", "ok": True})
    except Exception as exc:
        checks.append({"name": "redis", "ok": False, "error": str(exc)})

    try:
        await asyncio.to_thread(get_docker_client().ping)
        checks.append({"name": "docker_daemon", "ok": True})
    except Exception as exc:
        checks.append({"name": "docker_daemon", "ok": False, "error": str(exc)})

    # Egress path through proxy to GitHub.
    checks.append(await _check_http_url("proxy_to_github", DEEP_HEALTH_GITHUB_URL, use_proxy=True, required=True))

    # Optional LLM endpoint check.
    llm_url = os.environ.get("LLM_API_URL", "").strip()
    if llm_url and DEEP_HEALTH_CHECK_LLM:
        checks.append(await _check_http_url("llm_endpoint", llm_url, use_proxy=False, required=False))

    ok = all(item.get("ok", False) for item in checks if item.get("required", True))
    status_code = 200 if ok else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if ok else "degraded",
            "checks": checks,
            "timestamp": now_utc().isoformat(),
        },
    )


@app.on_event("startup")
async def startup_event() -> None:
    global retry_worker_task
    redis = await get_redis()
    await redis.ping()
    await asyncio.to_thread(get_docker_client().ping)
    WORKER_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    retry_worker_task = asyncio.create_task(retry_worker_loop())


@app.on_event("shutdown")
async def shutdown_event() -> None:
    global redis_client, retry_worker_task, docker_client
    if retry_worker_task is not None:
        retry_worker_task.cancel()
        try:
            await retry_worker_task
        except asyncio.CancelledError:
            pass
        retry_worker_task = None
    if redis_client is not None:
        await redis_client.aclose()
        redis_client = None
    if docker_client is not None:
        docker_client.close()
        docker_client = None


@app.post("/webhook/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
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
            "created_at": now_utc().isoformat(),
            "attempt_count": 0,
            "next_retry_at": None,
            "last_error": None,
            "assigned_agent": None,
            "output_pr": None,
        }
        await store_issue(issue)
        await _set_issue_status_label(repo_full_name, issue_number, issue["status"])
        print(f"Queued {queue_key} ({event_type}, trigger={trigger_type})")

        if trigger_type == "auto":
            await notify_slack(event_type, issue_number, title)
    except Exception as exc:
        print(f"Error processing event: {exc}")


@app.get("/api/issues")
async def get_queue():
    return {"issues": await list_issues()}


@app.get("/api/issues/{queue_key}")
async def get_issue(queue_key: str):
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
    verify_admin_token(x_admin_token)
    issue = await load_issue(queue_key)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    issue["status"] = "approved"
    issue["approved_at"] = now_utc().isoformat()
    issue["attempt_count"] = 0
    issue["next_retry_at"] = None
    issue["last_error"] = None
    issue.pop("dead_lettered_at", None)
    await store_issue(issue)
    await _set_issue_status_label(issue["repo_full_name"], issue["issue_number"], issue["status"])
    await _post_issue_comment(
        issue["repo_full_name"],
        issue["issue_number"],
        "Issue approved for agent execution. Waiting for worker start.",
    )
    background_tasks.add_task(run_agent_for_issue, queue_key)
    return {"status": "approved", "issue": issue}


@app.post("/api/issues/{queue_key}/reject")
async def reject_issue(
    queue_key: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    verify_admin_token(x_admin_token)
    issue = await load_issue(queue_key)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    issue["status"] = "rejected"
    issue["rejected_at"] = now_utc().isoformat()
    issue["next_retry_at"] = None
    await store_issue(issue)
    await _set_issue_status_label(issue["repo_full_name"], issue["issue_number"], issue["status"])
    await _post_issue_comment(issue["repo_full_name"], issue["issue_number"], "Issue rejected for agent execution.")
    return {"status": "rejected", "issue": issue}


@app.get("/api/sessions")
async def get_sessions():
    return {"sessions": await list_sessions()}


@app.get("/api/dead-letter/issues")
async def get_dead_letter_issues():
    return {"issues": await list_dead_letters()}


@app.post("/api/issues/{queue_key}/requeue")
async def requeue_issue(
    queue_key: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    verify_admin_token(x_admin_token)
    issue = await load_issue(queue_key)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    issue["status"] = "approved"
    issue["attempt_count"] = 0
    issue["next_retry_at"] = None
    issue["last_error"] = None
    issue.pop("dead_lettered_at", None)
    await store_issue(issue)
    await _set_issue_status_label(issue["repo_full_name"], issue["issue_number"], issue["status"])
    await _post_issue_comment(
        issue["repo_full_name"],
        issue["issue_number"],
        "Issue manually requeued for agent execution.",
    )
    asyncio.create_task(run_agent_for_issue(queue_key))
    return {"status": "requeued", "issue": issue}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
