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
from docker.errors import NotFound
from docker.types import Mount
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from network_policy import (
    NetworkPolicyError,
    host_allowed_by_squid,
    load_squid_allowed_domains,
    parse_host_patterns,
    parse_proxy_url,
    validate_llm_endpoint,
    validate_public_http_url,
)
from redis.asyncio import Redis

# Configuration
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
RUNTIME_ENV = os.environ.get("DEPLOYMENT_ENV", "development").strip().lower()
WEBHOOK_DELIVERY_TTL_SECONDS = int(os.environ.get("WEBHOOK_DELIVERY_TTL_SECONDS", "86400"))
WEBHOOK_MAX_BODY_BYTES = int(os.environ.get("WEBHOOK_MAX_BODY_BYTES", "262144"))
WEBHOOK_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("WEBHOOK_RATE_LIMIT_WINDOW_SECONDS", "60"))
WEBHOOK_RATE_LIMIT_GLOBAL_MAX = int(os.environ.get("WEBHOOK_RATE_LIMIT_GLOBAL_MAX", "120"))
WEBHOOK_RATE_LIMIT_REPO_MAX = int(os.environ.get("WEBHOOK_RATE_LIMIT_REPO_MAX", "60"))
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
WORKER_RUN_AS_UID = int(os.environ.get("WORKER_RUN_AS_UID", "1000"))
WORKER_RUN_AS_GID = int(os.environ.get("WORKER_RUN_AS_GID", "1000"))
WORKER_ENABLE_HOST_GATEWAY = os.environ.get("WORKER_ENABLE_HOST_GATEWAY", "false").lower() == "true"
WORKER_ARTIFACTS_DIR = Path(os.environ.get("WORKER_ARTIFACTS_DIR", "/worker-artifacts"))
WORKER_ARTIFACTS_VOLUME = os.environ.get("WORKER_ARTIFACTS_VOLUME", "worker-artifacts")
WORKER_VOLUME_PREFIX = os.environ.get("WORKER_VOLUME_PREFIX", "agent-task")
WORKER_HTTP_PROXY = os.environ.get("WORKER_HTTP_PROXY", "http://egress-proxy:3128")
WORKER_HTTPS_PROXY = os.environ.get("WORKER_HTTPS_PROXY", "http://egress-proxy:3128")
WORKER_NO_PROXY = os.environ.get("WORKER_NO_PROXY", "localhost,127.0.0.1,egress-proxy,redis")
LLM_API_URL = os.environ.get("LLM_API_URL", "").strip()
LLM_MODEL = os.environ.get("LLM_MODEL", "").strip()
LLM_HOST_ALLOWLIST_RAW = os.environ.get("LLM_HOST_ALLOWLIST", "localhost").strip()
DEEP_HEALTH_TIMEOUT_SECONDS = float(os.environ.get("DEEP_HEALTH_TIMEOUT_SECONDS", "8"))
DEEP_HEALTH_GITHUB_URL = os.environ.get("DEEP_HEALTH_GITHUB_URL", "https://api.github.com/meta")
DEEP_HEALTH_CHECK_LLM = os.environ.get("DEEP_HEALTH_CHECK_LLM", "false").lower() == "true"
GITHUB_STATUS_LABELS_ENABLED = os.environ.get("GITHUB_STATUS_LABELS_ENABLED", "true").lower() == "true"
GITHUB_STATUS_COMMENTS_ENABLED = os.environ.get("GITHUB_STATUS_COMMENTS_ENABLED", "true").lower() == "true"
GITHUB_ASSIGN_ON_PROCESSING = os.environ.get("GITHUB_ASSIGN_ON_PROCESSING", "false").lower() == "true"
GITHUB_ASSIGNEE_LOGIN = os.environ.get("GITHUB_ASSIGNEE_LOGIN", "").strip()
LABEL_QUEUED = os.environ.get("GH_LABEL_QUEUED", "agent:queued")
LABEL_IN_PROGRESS = os.environ.get("GH_LABEL_IN_PROGRESS", "agent:in-progress")
LABEL_NEEDS_HUMAN = os.environ.get("GH_LABEL_NEEDS_HUMAN", "agent:needs-human")
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
TASK_KEY_PREFIX = "task:"
SESSION_KEY_PREFIX = "session:"
ISSUE_INDEX_KEY = "issues:index"
TASK_INDEX_KEY = "tasks:index"
SESSION_INDEX_KEY = "sessions:index"
RETRY_INDEX_KEY = "issues:retry:index"
DEAD_LETTER_INDEX_KEY = "issues:dead_letter:index"
ISSUE_TASKS_INDEX_PREFIX = "issue:tasks:"
TASK_RETRY_INDEX_KEY = "tasks:retry:index"
WEBHOOK_RATE_LIMIT_GLOBAL_KEY_PREFIX = "webhook:rate:global:"
WEBHOOK_RATE_LIMIT_REPO_KEY_PREFIX = "webhook:rate:repo:"
WORKER_LABEL_KEY = "agentic.dev-system.worker"
WORKER_JOB_LABEL_KEY = "agentic.dev-system.job-id"
WORKER_TASK_LABEL_KEY = "agentic.dev-system.task-id"
WORKER_QUEUE_LABEL_KEY = "agentic.dev-system.queue-key"

SUPPORTED_WEBHOOK_ACTIONS: Dict[str, set[str]] = {
    "issues": {"opened", "edited", "reopened"},
    "issue_comment": {"created"},
    "pull_request": {"synchronize"},
}

ALL_AGENT_LABELS = [
    LABEL_QUEUED,
    LABEL_IN_PROGRESS,
    LABEL_NEEDS_HUMAN,
    LABEL_PR_OPENED,
    LABEL_FAILED,
    LABEL_DEAD_LETTER,
    LABEL_REJECTED,
]
LABEL_COLORS = {
    LABEL_QUEUED: "D4C5F9",
    LABEL_IN_PROGRESS: "0E8A16",
    LABEL_NEEDS_HUMAN: "FBCA04",
    LABEL_PR_OPENED: "1D76DB",
    LABEL_FAILED: "B60205",
    LABEL_DEAD_LETTER: "5319E7",
    LABEL_REJECTED: "6A737D",
}

TERMINAL_TASK_STATUSES = {"completed", "rejected", "dead_letter"}
OPEN_TASK_STATUSES = {"queued", "approved", "processing", "queued_retry", "needs_human"}
ALLOWED_TASK_TRANSITIONS: Dict[str, set[str]] = {
    "queued": {"approved", "rejected", "needs_human"},
    "approved": {"processing", "rejected", "needs_human"},
    "processing": {"completed", "queued_retry", "needs_human", "dead_letter"},
    "queued_retry": {"approved", "needs_human", "dead_letter"},
    "needs_human": {"approved", "rejected", "dead_letter"},
    "dead_letter": {"approved"},
    "rejected": set(),
    "completed": set(),
}
PROJECTION_STATUS_PRIORITY = {
    "processing": 0,
    "approved": 1,
    "queued_retry": 2,
    "needs_human": 3,
    "queued": 4,
}
PR_AGENT_TRIGGER_KEYWORDS = ["@agent", "@ai"]
SQUID_CONFIG_PATH = Path(__file__).resolve().parent / "proxy" / "squid.conf"
WORKER_HOST_GATEWAY_NAME = "host.docker.internal"


class PayloadTooLargeError(Exception):
    """Raised when a webhook body exceeds the configured byte limit."""


class RateLimitExceededError(Exception):
    """Raised when a webhook request exceeds configured fixed-window limits."""

    def __init__(self, retry_after_seconds: int):
        super().__init__("Webhook rate limit exceeded.")
        self.retry_after_seconds = retry_after_seconds


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


def is_production_runtime() -> bool:
    return RUNTIME_ENV in {"prod", "production"}


def validate_runtime_configuration() -> None:
    if is_production_runtime() and not WEBHOOK_SECRET:
        raise RuntimeError("GITHUB_WEBHOOK_SECRET is required when DEPLOYMENT_ENV is set to production.")
    if is_production_runtime() and not ADMIN_API_TOKEN:
        raise RuntimeError("ADMIN_API_TOKEN is required when DEPLOYMENT_ENV is set to production.")
    positive_limits = {
        "WEBHOOK_MAX_BODY_BYTES": WEBHOOK_MAX_BODY_BYTES,
        "WEBHOOK_RATE_LIMIT_WINDOW_SECONDS": WEBHOOK_RATE_LIMIT_WINDOW_SECONDS,
        "WEBHOOK_RATE_LIMIT_GLOBAL_MAX": WEBHOOK_RATE_LIMIT_GLOBAL_MAX,
        "WEBHOOK_RATE_LIMIT_REPO_MAX": WEBHOOK_RATE_LIMIT_REPO_MAX,
        "WORKER_RUN_AS_UID": WORKER_RUN_AS_UID,
        "WORKER_RUN_AS_GID": WORKER_RUN_AS_GID,
    }
    for name, value in positive_limits.items():
        if value <= 0:
            raise RuntimeError(f"{name} must be greater than zero.")
    try:
        parse_proxy_url(WORKER_HTTP_PROXY, context="WORKER_HTTP_PROXY")
        parse_proxy_url(WORKER_HTTPS_PROXY, context="WORKER_HTTPS_PROXY")
        validate_public_http_url(DEEP_HEALTH_GITHUB_URL, context="DEEP_HEALTH_GITHUB_URL")
        if LLM_API_URL:
            llm_decision = validate_llm_endpoint(
                LLM_API_URL,
                allowlisted_hosts=parse_host_patterns(LLM_HOST_ALLOWLIST_RAW),
                no_proxy_hosts=parse_host_patterns(WORKER_NO_PROXY),
            )
            if llm_decision.host == WORKER_HOST_GATEWAY_NAME and not WORKER_ENABLE_HOST_GATEWAY:
                raise RuntimeError(
                    "WORKER_ENABLE_HOST_GATEWAY must be true when LLM_API_URL targets host.docker.internal."
                )
            if llm_decision.route == "proxy":
                squid_domains = load_squid_allowed_domains(SQUID_CONFIG_PATH)
                if not host_allowed_by_squid(llm_decision.host, squid_domains):
                    raise RuntimeError(
                        f"LLM_API_URL host '{llm_decision.host}' is not allowed by proxy/squid.conf allowed_domains."
                    )
    except NetworkPolicyError as exc:
        raise RuntimeError(str(exc)) from exc


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


def _task_storage_key(task_id: str) -> str:
    return f"{TASK_KEY_PREFIX}{task_id}"


def _issue_tasks_index_key(queue_key: str) -> str:
    return f"{ISSUE_TASKS_INDEX_PREFIX}{queue_key}"


def is_supported_webhook_action(event_type: str, action: str) -> bool:
    supported_actions = SUPPORTED_WEBHOOK_ACTIONS.get(event_type)
    if not supported_actions:
        return False
    return action in supported_actions


def is_task_terminal(status: str) -> bool:
    return status in TERMINAL_TASK_STATUSES


def is_task_open(status: str) -> bool:
    return status in OPEN_TASK_STATUSES


def validate_task_transition(current_status: str, next_status: str) -> None:
    allowed = ALLOWED_TASK_TRANSITIONS.get(current_status, set())
    if next_status not in allowed:
        raise HTTPException(status_code=409, detail=f"Invalid task transition: {current_status} -> {next_status}")


async def store_task(task: Dict[str, Any], *, create_only: bool = False) -> bool:
    redis = await get_redis()
    payload = json.dumps(task)
    key = _task_storage_key(task["task_id"])
    if create_only:
        try:
            created = await redis.set(key, payload, nx=True)
        except TypeError:
            existing = await redis.get(key)
            if existing:
                created = False
            else:
                await redis.set(key, payload)
                created = True
        if not created:
            return False
    else:
        await redis.set(key, payload)

    created_at = task.get("created_at") or now_utc().isoformat()
    timestamp = datetime.fromisoformat(created_at).timestamp()
    await redis.zadd(TASK_INDEX_KEY, {task["task_id"]: timestamp})
    await redis.zadd(_issue_tasks_index_key(task["queue_key"]), {task["task_id"]: timestamp})
    if task.get("status") == "queued_retry" and task.get("next_retry_at"):
        retry_ts = datetime.fromisoformat(task["next_retry_at"]).timestamp()
        await redis.zadd(TASK_RETRY_INDEX_KEY, {task["task_id"]: retry_ts})
    else:
        await redis.zrem(TASK_RETRY_INDEX_KEY, task["task_id"])
    return True


async def load_task(task_id: str) -> Dict[str, Any] | None:
    redis = await get_redis()
    data = await redis.get(_task_storage_key(task_id))
    return json.loads(data) if data else None


async def list_task_ids_for_issue(queue_key: str) -> List[str]:
    redis = await get_redis()
    task_ids = await redis.zrevrange(_issue_tasks_index_key(queue_key), 0, -1)
    task_ids.reverse()
    return task_ids


async def list_tasks_for_issue(queue_key: str) -> List[Dict[str, Any]]:
    task_ids = await list_task_ids_for_issue(queue_key)
    if not task_ids:
        return []
    redis = await get_redis()
    values = await redis.mget([_task_storage_key(task_id) for task_id in task_ids])
    return [json.loads(value) for value in values if value]


async def list_all_tasks() -> List[Dict[str, Any]]:
    redis = await get_redis()
    task_ids = await redis.zrevrange(TASK_INDEX_KEY, 0, -1)
    if not task_ids:
        return []
    values = await redis.mget([_task_storage_key(task_id) for task_id in task_ids])
    return [json.loads(value) for value in values if value]


async def store_issue(issue: Dict[str, Any]) -> None:
    redis = await get_redis()
    queue_key = issue["queue_key"]
    await redis.set(_issue_storage_key(queue_key), json.dumps(issue))
    await redis.zadd(ISSUE_INDEX_KEY, {queue_key: now_utc().timestamp()})
    if issue.get("status") == "dead_letter":
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


def _task_to_issue_projection(
    current_task: Dict[str, Any],
    latest_task: Dict[str, Any],
    existing_issue: Dict[str, Any] | None,
    pending_task_count: int,
    task_count: int,
) -> Dict[str, Any]:
    created_at = (existing_issue or {}).get("created_at") or current_task.get("created_at") or now_utc().isoformat()
    return {
        "queue_key": current_task["queue_key"],
        "subject_kind": current_task.get("subject_kind", "issue"),
        "repo_full_name": current_task["repo_full_name"],
        "repo_clone_url": current_task.get("repo_clone_url", ""),
        "issue_number": current_task["issue_number"],
        "title": current_task.get("title", ""),
        "body": current_task.get("body", ""),
        "sender": current_task.get("sender", "unknown"),
        "is_pr": current_task.get("is_pr", False),
        "trigger_source": current_task.get("trigger_source", current_task.get("event_type", "")),
        "event_type": current_task.get("event_type", ""),
        "event_action": current_task.get("event_action", ""),
        "trigger_type": current_task.get("trigger_type", "manual"),
        "status": current_task["status"],
        "task_id": current_task["task_id"],
        "current_task_id": current_task["task_id"],
        "latest_task_id": latest_task["task_id"],
        "latest_event_type": latest_task.get("event_type", ""),
        "latest_event_action": latest_task.get("event_action", ""),
        "delivery_id": current_task["delivery_id"],
        "created_at": created_at,
        "updated_at": now_utc().isoformat(),
        "approved_at": current_task.get("approved_at"),
        "started_at": current_task.get("started_at"),
        "completed_at": current_task.get("completed_at"),
        "retried_at": current_task.get("retried_at"),
        "rejected_at": current_task.get("rejected_at"),
        "dead_lettered_at": current_task.get("dead_lettered_at"),
        "needs_human_at": current_task.get("needs_human_at"),
        "needs_human_reason": current_task.get("needs_human_reason"),
        "attempt_count": int(current_task.get("attempt_count", 0)),
        "next_retry_at": current_task.get("next_retry_at"),
        "last_error": current_task.get("last_error"),
        "errors": current_task.get("errors", []),
        "assigned_agent": current_task.get("assigned_agent"),
        "output_pr": current_task.get("output_pr"),
        "pr": current_task.get("pr"),
        "comment": current_task.get("comment"),
        "task_count": task_count,
        "pending_task_count": pending_task_count,
    }


def _pick_current_task(tasks: List[Dict[str, Any]], preferred_task_id: str | None = None) -> Dict[str, Any]:
    if preferred_task_id:
        preferred = next((task for task in tasks if task["task_id"] == preferred_task_id and is_task_open(task.get("status", ""))), None)
        if preferred is not None:
            return preferred

    open_tasks = [task for task in tasks if is_task_open(task.get("status", ""))]
    if not open_tasks:
        return tasks[-1]

    def sort_key(task: Dict[str, Any]) -> tuple[int, float]:
        priority = PROJECTION_STATUS_PRIORITY.get(task.get("status", ""), 99)
        created_at = datetime.fromisoformat(task.get("created_at") or now_utc().isoformat()).timestamp()
        return priority, -created_at

    return min(open_tasks, key=sort_key)


async def sync_issue_projection(queue_key: str, preferred_task_id: str | None = None) -> Dict[str, Any] | None:
    tasks = await list_tasks_for_issue(queue_key)
    if not tasks:
        return None

    existing_issue = await load_issue(queue_key)
    latest_task = tasks[-1]
    current_task = _pick_current_task(tasks, preferred_task_id or (existing_issue or {}).get("current_task_id"))

    pending_task_count = sum(
        1
        for task in tasks
        if is_task_open(task.get("status", "")) and task["task_id"] != current_task["task_id"]
    )
    projection = _task_to_issue_projection(current_task, latest_task, existing_issue, pending_task_count, len(tasks))
    await store_issue(projection)
    return projection


async def load_current_task_for_issue(queue_key: str) -> tuple[Dict[str, Any] | None, Dict[str, Any] | None]:
    issue = await load_issue(queue_key)
    if not issue:
        return None, None
    task_id = issue.get("current_task_id") or issue.get("task_id")
    if not task_id:
        return issue, None
    task = await load_task(task_id)
    if task is None:
        issue = await sync_issue_projection(queue_key)
        if issue is None:
            return None, None
        task_id = issue.get("current_task_id")
        task = await load_task(task_id) if task_id else None
    return issue, task


async def update_task(queue_key: str, task: Dict[str, Any], preferred_task_id: str | None = None) -> Dict[str, Any]:
    task["updated_at"] = now_utc().isoformat()
    await store_task(task, create_only=False)
    synced = await sync_issue_projection(queue_key, preferred_task_id=preferred_task_id)
    return synced or task


async def transition_task(
    queue_key: str,
    task: Dict[str, Any],
    next_status: str,
    *,
    preferred_task_id: str | None = None,
    **updates: Any,
) -> tuple[Dict[str, Any], Dict[str, Any] | None]:
    validate_task_transition(task["status"], next_status)
    task["status"] = next_status
    task.update(updates)
    issue = await update_task(queue_key, task, preferred_task_id=preferred_task_id or task["task_id"])
    return task, issue


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


def _load_worker_artifact_payload(task: Dict[str, Any]) -> tuple[Dict[str, Any] | None, str | None]:
    runtime = _worker_runtime_metadata(task)
    artifact_path = runtime["artifact_host_path"]
    if not artifact_path.exists():
        return None, None
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"Failed reading worker artifact {artifact_path.name}: {exc}"
    if not isinstance(payload, dict):
        return None, f"Worker artifact {artifact_path.name} did not contain a JSON object."
    return payload, None


async def _finalize_task_session(
    task: Dict[str, Any],
    session_data: Dict[str, Any],
    worker_logs: str = "",
    *,
    recovered: bool = False,
) -> Dict[str, Any] | None:
    queue_key = task["queue_key"]
    session_data.setdefault("session_id", str(uuid.uuid4()))
    session_data.setdefault("created_at", now_utc().isoformat())
    session_data.setdefault("status", "failed")
    if worker_logs:
        logs = session_data.get("logs") or []
        logs.append(worker_logs[-4000:])
        session_data["logs"] = logs

    await store_session(session_data, session_data["created_at"])
    completed_at = now_utc().isoformat()
    output_pr = {
        "number": session_data.get("output_pr_number"),
        "url": session_data.get("output_pr_url"),
    }
    status_prefix = "Recovered agent task" if recovered else "Agent task"
    issue_prefix = "Recovered agent" if recovered else "Agent"

    if session_data.get("status") == "completed":
        task, issue = await transition_task(
            queue_key,
            task,
            "completed",
            preferred_task_id=task["task_id"],
            assigned_agent=session_data["session_id"],
            completed_at=completed_at,
            output_pr=output_pr,
            next_retry_at=None,
            last_error=None,
            errors=session_data.get("errors", []),
        )
        pr_url = output_pr.get("url")
        if issue:
            if pr_url:
                if task.get("subject_kind") == "pull_request":
                    message = f"{issue_prefix} updated pull request task `{task['task_id']}` successfully: {pr_url}"
                else:
                    message = f"{issue_prefix} completed successfully. PR created: {pr_url}"
                await _post_issue_comment(issue["repo_full_name"], issue["issue_number"], message)
            else:
                await _post_issue_comment(
                    issue["repo_full_name"],
                    issue["issue_number"],
                    f"{issue_prefix} completed successfully.",
                )
            await _set_issue_status_label(issue["repo_full_name"], issue["issue_number"], issue["status"])
        return issue

    if session_data.get("status") == "needs_human":
        errors = session_data.get("errors") or ["Agent run requires human review."]
        task, issue = await transition_task(
            queue_key,
            task,
            "needs_human",
            preferred_task_id=task["task_id"],
            assigned_agent=session_data["session_id"],
            completed_at=completed_at,
            errors=errors,
            last_error=errors[-1],
            next_retry_at=None,
            needs_human_at=now_utc().isoformat(),
            needs_human_reason=errors[-1],
            output_pr=output_pr,
        )
        if issue:
            await _post_issue_comment(
                issue["repo_full_name"],
                issue["issue_number"],
                f"{issue_prefix} moved task `{task['task_id']}` to needs-human review. Reason: {task['needs_human_reason']}",
            )
            await _set_issue_status_label(issue["repo_full_name"], issue["issue_number"], issue["status"])
        return issue

    errors = session_data.get("errors") or ["Agent run failed without explicit error message."]
    attempts_used = int(task.get("attempt_count", 1))
    retries_used = max(attempts_used - 1, 0)
    if retries_used < MAX_RETRIES:
        delay_seconds = compute_retry_delay_seconds(attempts_used)
        retry_at = now_utc() + timedelta(seconds=delay_seconds)
        task, issue = await transition_task(
            queue_key,
            task,
            "queued_retry",
            preferred_task_id=task["task_id"],
            assigned_agent=session_data["session_id"],
            completed_at=completed_at,
            errors=errors,
            last_error=errors[-1],
            next_retry_at=retry_at.isoformat(),
            output_pr=output_pr,
        )
        if issue:
            await _post_issue_comment(
                issue["repo_full_name"],
                issue["issue_number"],
                f"{status_prefix} `{task['task_id']}` attempt {attempts_used} failed. "
                f"Scheduled retry at {task['next_retry_at']}. Last error: {task['last_error']}",
            )
            await _set_issue_status_label(issue["repo_full_name"], issue["issue_number"], issue["status"])
        return issue

    task, issue = await transition_task(
        queue_key,
        task,
        "dead_letter",
        preferred_task_id=task["task_id"],
        assigned_agent=session_data["session_id"],
        completed_at=completed_at,
        errors=errors,
        last_error=errors[-1],
        next_retry_at=None,
        dead_lettered_at=now_utc().isoformat(),
        output_pr=output_pr,
    )
    if issue:
        await _post_issue_comment(
            issue["repo_full_name"],
            issue["issue_number"],
            f"{status_prefix} `{task['task_id']}` to dead-letter after {attempts_used} attempts. "
            f"Last error: {task['last_error']}",
        )
        await _set_issue_status_label(issue["repo_full_name"], issue["issue_number"], issue["status"])
    return issue


def _parse_content_length(raw_value: str | None) -> int | None:
    if raw_value is None:
        return None
    try:
        parsed = int(raw_value.strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


async def read_webhook_body(request: Request) -> bytes:
    content_length = _parse_content_length(request.headers.get("Content-Length"))
    if content_length is not None and content_length > WEBHOOK_MAX_BODY_BYTES:
        raise PayloadTooLargeError()

    if hasattr(request, "stream"):
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > WEBHOOK_MAX_BODY_BYTES:
                raise PayloadTooLargeError()
            chunks.append(chunk)
        return b"".join(chunks)

    payload = await request.body()
    if len(payload) > WEBHOOK_MAX_BODY_BYTES:
        raise PayloadTooLargeError()
    return payload


def verify_github_signature(payload: bytes, signature: str) -> bool:
    """Verify GitHub webhook signature using HMAC SHA-256."""
    if not WEBHOOK_SECRET:
        return not is_production_runtime()
    if not signature:
        return False
    digest = hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(signature, expected)


def _rate_limit_bucket(now: datetime | None = None) -> tuple[str, int]:
    current = now or now_utc()
    current_ts = int(current.timestamp())
    window_start = current_ts - (current_ts % WEBHOOK_RATE_LIMIT_WINDOW_SECONDS)
    retry_after = max(1, WEBHOOK_RATE_LIMIT_WINDOW_SECONDS - (current_ts - window_start))
    return str(window_start), retry_after


def _normalize_repo_token(repo_full_name: str) -> str:
    return repo_full_name.strip().lower().replace("/", ":")


async def _increment_rate_limit_counter(key: str, limit: int, retry_after_seconds: int) -> None:
    if limit <= 0:
        return
    redis = await get_redis()
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, WEBHOOK_RATE_LIMIT_WINDOW_SECONDS + 1)
    if count > limit:
        raise RateLimitExceededError(retry_after_seconds)


async def enforce_webhook_rate_limit(repo_full_name: str) -> int:
    bucket, retry_after_seconds = _rate_limit_bucket()
    await _increment_rate_limit_counter(
        f"{WEBHOOK_RATE_LIMIT_GLOBAL_KEY_PREFIX}{bucket}",
        WEBHOOK_RATE_LIMIT_GLOBAL_MAX,
        retry_after_seconds,
    )
    normalized_repo = _normalize_repo_token(repo_full_name)
    if normalized_repo:
        await _increment_rate_limit_counter(
            f"{WEBHOOK_RATE_LIMIT_REPO_KEY_PREFIX}{normalized_repo}:{bucket}",
            WEBHOOK_RATE_LIMIT_REPO_MAX,
            retry_after_seconds,
        )
    return retry_after_seconds


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
        "needs_human": LABEL_NEEDS_HUMAN,
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


def _worker_container_user() -> str:
    return f"{WORKER_RUN_AS_UID}:{WORKER_RUN_AS_GID}"


def _worker_container_extra_hosts() -> Dict[str, str] | None:
    if not WORKER_ENABLE_HOST_GATEWAY:
        return None
    return {WORKER_HOST_GATEWAY_NAME: "host-gateway"}


def _worker_runtime_metadata(task: Dict[str, Any]) -> Dict[str, Any]:
    job_id = (task.get("worker_job_id") or uuid.uuid4().hex).strip()
    container_name = task.get("worker_container_name") or f"agent-worker-{job_id}"
    workspace_volume_name = task.get("worker_workspace_volume") or f"{WORKER_VOLUME_PREFIX}-ws-{job_id}"
    artifact_host_path = Path(task.get("worker_artifact_path") or (WORKER_ARTIFACTS_DIR / f"{job_id}.json"))
    artifact_container_path = f"/artifacts/{artifact_host_path.name}"
    labels = {
        WORKER_LABEL_KEY: "true",
        WORKER_JOB_LABEL_KEY: job_id,
        WORKER_TASK_LABEL_KEY: task["task_id"],
        WORKER_QUEUE_LABEL_KEY: task["queue_key"],
    }
    return {
        "job_id": job_id,
        "container_name": container_name,
        "workspace_volume_name": workspace_volume_name,
        "artifact_host_path": artifact_host_path,
        "artifact_container_path": artifact_container_path,
        "labels": labels,
    }


def _prepare_worker_mount_permissions(client: DockerClient, mounts: List[Mount]) -> None:
    init_container = None
    try:
        init_container = client.containers.run(
            WORKER_IMAGE,
            command=[
                "sh",
                "-lc",
                (
                    "mkdir -p /workspace /artifacts && "
                    f"chown {WORKER_RUN_AS_UID}:{WORKER_RUN_AS_GID} /workspace /artifacts && "
                    "chmod 700 /workspace && chmod 755 /artifacts"
                ),
            ],
            mounts=mounts,
            detach=True,
            user="0:0",
        )
        result = init_container.wait(timeout=60)
        status_code = int(result.get("StatusCode", 1)) if isinstance(result, dict) else 1
        if status_code != 0:
            logs = init_container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
            raise Exception(f"Worker mount preparation failed: {logs[-800:]}")
    finally:
        if init_container is not None:
            try:
                init_container.remove(force=True)
            except Exception:
                pass


def _cleanup_worker_runtime_for_task(task: Dict[str, Any]) -> Dict[str, Any]:
    client = get_docker_client()
    runtime = _worker_runtime_metadata(task)
    logs_text = ""
    container_removed = False
    volume_removed = False

    try:
        container = client.containers.get(runtime["container_name"])
    except NotFound:
        container = None
    if container is not None:
        try:
            logs_text = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
        except Exception:
            logs_text = ""
        try:
            container.remove(force=True)
            container_removed = True
        except Exception as exc:
            logs_text = (logs_text + f"\nContainer cleanup failed: {exc}").strip()

    try:
        volume = client.volumes.get(runtime["workspace_volume_name"])
    except NotFound:
        volume = None
    if volume is not None:
        try:
            volume.remove(force=True)
            volume_removed = True
        except Exception as exc:
            logs_text = (logs_text + f"\nWorkspace volume cleanup failed: {exc}").strip()

    return {
        "container_removed": container_removed,
        "volume_removed": volume_removed,
        "logs": logs_text[-4000:],
    }


def _run_worker_container(task: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
    """
    Run a one-off worker container and return (session_payload, logs).
    This function runs in a thread via asyncio.to_thread.
    """
    client = get_docker_client()
    runtime = _worker_runtime_metadata(task)
    job_id = runtime["job_id"]
    workspace_volume_name = runtime["workspace_volume_name"]
    artifact_host_path = runtime["artifact_host_path"]
    artifact_container_path = runtime["artifact_container_path"]
    repo_url = build_repo_url(task["repo_full_name"], task.get("repo_clone_url", ""))

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
        workspace_volume = client.volumes.create(name=workspace_volume_name, labels=runtime["labels"])
        mounts = [
            Mount(target="/workspace", source=workspace_volume_name, type="volume", read_only=False),
            Mount(target="/artifacts", source=WORKER_ARTIFACTS_VOLUME, type="volume", read_only=False),
        ]
        _prepare_worker_mount_permissions(client, mounts)
        env = {
            "ISSUE_JSON": json.dumps(
                {
                    "task_id": task["task_id"],
                    "queue_key": task["queue_key"],
                    "subject_kind": task.get("subject_kind", "issue"),
                    "trigger_source": task.get("trigger_source", task.get("event_type", "")),
                    "issue_number": task["issue_number"],
                    "title": task["title"],
                    "body": task["body"],
                    "is_pr": task["is_pr"],
                    "repo_name": task["repo_full_name"],
                    "repo_full_name": task["repo_full_name"],
                    "comment": task.get("comment"),
                    "pr": task.get("pr"),
                }
            ),
            "OUTPUT_PATH": artifact_container_path,
            "GITHUB_WRITE_TOKEN": GITHUB_TOKEN,
            "TARGET_REPO_URL": repo_url,
            "GITHUB_BASE_BRANCH": os.environ.get("GITHUB_BASE_BRANCH", "main"),
            "LLM_API_URL": LLM_API_URL,
            "LLM_MODEL": LLM_MODEL,
            "LLM_HOST_ALLOWLIST": LLM_HOST_ALLOWLIST_RAW,
            "WORKER_HTTP_PROXY": WORKER_HTTP_PROXY,
            "WORKER_HTTPS_PROXY": WORKER_HTTPS_PROXY,
            "WORKER_NO_PROXY": WORKER_NO_PROXY,
            "WORKER_ENABLE_HOST_GATEWAY": "true" if WORKER_ENABLE_HOST_GATEWAY else "false",
            "HTTP_PROXY": WORKER_HTTP_PROXY,
            "HTTPS_PROXY": WORKER_HTTPS_PROXY,
            "ALL_PROXY": WORKER_HTTPS_PROXY,
            "NO_PROXY": WORKER_NO_PROXY,
            "http_proxy": WORKER_HTTP_PROXY,
            "https_proxy": WORKER_HTTPS_PROXY,
            "all_proxy": WORKER_HTTPS_PROXY,
            "no_proxy": WORKER_NO_PROXY,
        }
        run_kwargs: Dict[str, Any] = {
            "name": runtime["container_name"],
            "command": ["python", "worker_entrypoint.py"],
            "environment": env,
            "mounts": mounts,
            "network": WORKER_NETWORK,
            "detach": True,
            "labels": runtime["labels"],
            "user": _worker_container_user(),
            "read_only": True,
            "tmpfs": {"/tmp": "rw,noexec,nosuid,size=256m"},
            "mem_limit": WORKER_MEMORY_LIMIT,
            "nano_cpus": int(WORKER_CPU_LIMIT * 1_000_000_000),
            "pids_limit": WORKER_PIDS_LIMIT,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges"],
        }
        extra_hosts = _worker_container_extra_hosts()
        if extra_hosts:
            run_kwargs["extra_hosts"] = extra_hosts

        container = client.containers.run(
            WORKER_IMAGE,
            **run_kwargs,
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


async def run_agent_for_task(task_id: str) -> None:
    """Run orchestrator for a specific approved task inside an isolated worker container."""
    async with AGENT_RUN_LOCK:
        task = await load_task(task_id)
        if not task:
            return
        queue_key = task["queue_key"]
        issue = await sync_issue_projection(queue_key, preferred_task_id=task_id)
        if not issue:
            return
        if task.get("status") != "approved":
            return

        started_at = now_utc().isoformat()
        attempt_count = int(task.get("attempt_count", 0)) + 1
        worker_job_id = uuid.uuid4().hex
        runtime = _worker_runtime_metadata({"task_id": task["task_id"], "queue_key": queue_key, "worker_job_id": worker_job_id})
        task, issue = await transition_task(
            queue_key,
            task,
            "processing",
            preferred_task_id=task_id,
            started_at=started_at,
            attempt_count=attempt_count,
            next_retry_at=None,
            needs_human_at=None,
            needs_human_reason=None,
            worker_job_id=worker_job_id,
            worker_container_name=runtime["container_name"],
            worker_workspace_volume=runtime["workspace_volume_name"],
            worker_artifact_path=str(runtime["artifact_host_path"]),
        )
        await _set_issue_status_label(issue["repo_full_name"], issue["issue_number"], issue["status"])
        await _assign_issue(issue["repo_full_name"], issue["issue_number"])
        await _post_issue_comment(
            issue["repo_full_name"],
            issue["issue_number"],
            f"Agent started processing task `{task['task_id']}` (attempt {task['attempt_count']}). Session pending...",
        )

        session_data, worker_logs = await asyncio.to_thread(_run_worker_container, task)
        await _finalize_task_session(task, session_data, worker_logs)


async def run_agent_for_issue(queue_key: str) -> None:
    """Backward-compatible wrapper that runs the current approved task for a queue key."""
    issue, task = await load_current_task_for_issue(queue_key)
    if not issue or not task:
        return
    await run_agent_for_task(task["task_id"])


async def retry_worker_loop() -> None:
    while True:
        try:
            redis = await get_redis()
            now_ts = now_utc().timestamp()
            due_task_ids = await redis.zrangebyscore(TASK_RETRY_INDEX_KEY, 0, now_ts)
            for task_id in due_task_ids:
                task = await load_task(task_id)
                if not task:
                    await redis.zrem(TASK_RETRY_INDEX_KEY, task_id)
                    continue
                if task.get("status") != "queued_retry":
                    await redis.zrem(TASK_RETRY_INDEX_KEY, task_id)
                    continue
                queue_key = task["queue_key"]
                task, issue = await transition_task(
                    queue_key,
                    task,
                    "approved",
                    preferred_task_id=task_id,
                    retried_at=now_utc().isoformat(),
                    next_retry_at=None,
                )
                await _set_issue_status_label(issue["repo_full_name"], issue["issue_number"], issue["status"])
                await _post_issue_comment(
                    issue["repo_full_name"],
                    issue["issue_number"],
                    f"Automatic retry triggered for task `{task['task_id']}` at {task['retried_at']}.",
                )
                asyncio.create_task(run_agent_for_task(task_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Retry worker error: {exc}")
        await asyncio.sleep(RETRY_POLL_INTERVAL_SECONDS)


def _text_has_pr_agent_trigger(text: str) -> bool:
    text_l = text.lower()
    return any(keyword in text_l for keyword in PR_AGENT_TRIGGER_KEYWORDS)


def _detect_trigger_type(title: str, body: str) -> str:
    trigger_keywords = ["@agent", "@ai", "fix this", "implement", "create"]
    trigger_text = f"{title}\n{body}".lower()
    return "auto" if any(keyword in trigger_text for keyword in trigger_keywords) else "manual"


async def _fetch_pull_request_context(repo_full_name: str, pr_number: int) -> Dict[str, Any]:
    response = await _github_api_request("GET", repo_full_name, f"/pulls/{pr_number}")
    if response.status_code >= 300:
        raise Exception(f"Failed to fetch pull request context: {response.status_code} {response.text[:300]}")
    pr_data = response.json()
    head_repo = pr_data.get("head", {}).get("repo") or {}
    base_repo = pr_data.get("base", {}).get("repo") or {}
    head_full_name = (head_repo.get("full_name") or "").strip()
    base_full_name = (base_repo.get("full_name") or repo_full_name).strip()
    html_url = pr_data.get("html_url") or f"https://github.com/{repo_full_name}/pull/{pr_number}"
    return {
        "number": pr_data.get("number", pr_number),
        "title": pr_data.get("title", ""),
        "body": pr_data.get("body", "") or "",
        "html_url": html_url,
        "same_repo": head_full_name.lower() == repo_full_name.lower(),
        "maintainer_can_modify": bool(pr_data.get("maintainer_can_modify", False)),
        "head_repo_full_name": head_full_name or repo_full_name,
        "head_repo_clone_url": head_repo.get("clone_url") or build_repo_url(repo_full_name),
        "head_ref": pr_data.get("head", {}).get("ref", ""),
        "head_sha": pr_data.get("head", {}).get("sha", ""),
        "base_repo_full_name": base_full_name,
        "base_ref": pr_data.get("base", {}).get("ref", ""),
        "base_sha": pr_data.get("base", {}).get("sha", ""),
    }


async def _mark_pull_request_tasks_stale(
    repo_full_name: str,
    pr_number: int,
    new_head_sha: str,
    delivery_id: str,
) -> int:
    queue_key = build_queue_key(repo_full_name, pr_number)
    tasks = await list_tasks_for_issue(queue_key)
    stale_count = 0
    for task in tasks:
        if task.get("subject_kind") != "pull_request":
            continue
        if task.get("status") not in {"queued", "approved", "queued_retry", "needs_human"}:
            continue
        old_head_sha = ((task.get("pr") or {}).get("head_sha") or "").strip()
        if not old_head_sha or old_head_sha == new_head_sha:
            continue
        reason = (
            f"Pull request head changed from {old_head_sha[:12]} to {new_head_sha[:12]} "
            f"on synchronize delivery `{delivery_id}`. Re-approval is required."
        )
        task, issue = await transition_task(
            queue_key,
            task,
            "needs_human",
            preferred_task_id=task["task_id"],
            needs_human_at=now_utc().isoformat(),
            needs_human_reason=reason,
            last_error=reason,
            errors=((task.get("errors") or []) + [reason])[-10:],
            next_retry_at=None,
        )
        stale_count += 1
        if issue:
            await _set_issue_status_label(issue["repo_full_name"], issue["issue_number"], issue["status"])
            await _post_issue_comment(issue["repo_full_name"], issue["issue_number"], reason)
    return stale_count


async def build_task_from_event(
    event_type: str,
    payload: Dict[str, Any],
    action: str,
    delivery_id: str,
) -> tuple[Dict[str, Any] | None, str | None]:
    repo = payload.get("repository", {})
    repo_full_name = repo.get("full_name", "").strip()
    repo_clone_url = repo.get("clone_url", "")
    sender = payload.get("sender", {}).get("login", "unknown")

    issue_number = None
    title = ""
    body = ""

    if event_type == "issues":
        issue = payload.get("issue", {})
        issue_number = issue.get("number")
        title = issue.get("title", "")
        body = issue.get("body", "") or ""
        subject_kind = "issue"
    elif event_type == "issue_comment":
        issue = payload.get("issue", {})
        comment = payload.get("comment", {})
        issue_number = issue.get("number")
        title = issue.get("title", "")
        body = comment.get("body", "") or ""
        if issue.get("pull_request"):
            if not _text_has_pr_agent_trigger(body):
                return None, "pull_request_comment_without_agent_trigger"
            pr_context = await _fetch_pull_request_context(repo_full_name, issue_number)
            if not pr_context.get("same_repo"):
                return None, "fork_pull_request_not_supported"
            if not pr_context.get("head_ref") or not pr_context.get("head_sha") or not pr_context.get("base_ref"):
                return None, "pull_request_context_incomplete"
            created_at = now_utc().isoformat()
            queue_key = build_queue_key(repo_full_name, issue_number)
            return (
                {
                    "task_id": delivery_id,
                    "queue_key": queue_key,
                    "subject_kind": "pull_request",
                    "trigger_source": "pr_issue_comment",
                    "event_type": event_type,
                    "event_action": action,
                    "delivery_id": delivery_id,
                    "issue_number": issue_number,
                    "title": pr_context.get("title", title),
                    "body": body,
                    "is_pr": True,
                    "trigger_type": "manual",
                    "status": "queued",
                    "sender": sender,
                    "repo_full_name": repo_full_name,
                    "repo_clone_url": pr_context.get("head_repo_clone_url") or repo_clone_url,
                    "created_at": created_at,
                    "updated_at": created_at,
                    "attempt_count": 0,
                    "next_retry_at": None,
                    "last_error": None,
                    "errors": [],
                    "assigned_agent": None,
                    "output_pr": {
                        "number": pr_context["number"],
                        "url": pr_context["html_url"],
                    },
                    "approved_at": None,
                    "started_at": None,
                    "completed_at": None,
                    "retried_at": None,
                    "rejected_at": None,
                    "dead_lettered_at": None,
                    "needs_human_at": None,
                    "needs_human_reason": None,
                    "pr": pr_context,
                    "comment": {
                        "comment_id": comment.get("id"),
                        "body": body,
                        "html_url": comment.get("html_url", ""),
                    },
                },
                None,
            )
        subject_kind = "issue"
    else:
        return None, "unsupported_event"

    if issue_number is None:
        return None, "invalid_payload_missing_issue_number"

    trigger_type = _detect_trigger_type(title, body)
    queue_key = build_queue_key(repo_full_name, issue_number)
    created_at = now_utc().isoformat()

    return (
        {
            "task_id": delivery_id,
            "queue_key": queue_key,
            "subject_kind": subject_kind,
            "trigger_source": event_type,
            "event_type": event_type,
            "event_action": action,
            "delivery_id": delivery_id,
            "issue_number": issue_number,
            "title": title,
            "body": body,
            "is_pr": False,
            "trigger_type": trigger_type,
            "status": "queued",
            "sender": sender,
            "repo_full_name": repo_full_name,
            "repo_clone_url": repo_clone_url,
            "created_at": created_at,
            "updated_at": created_at,
            "attempt_count": 0,
            "next_retry_at": None,
            "last_error": None,
            "errors": [],
            "assigned_agent": None,
            "output_pr": None,
            "approved_at": None,
            "started_at": None,
            "completed_at": None,
            "retried_at": None,
            "rejected_at": None,
            "dead_lettered_at": None,
            "needs_human_at": None,
            "needs_human_reason": None,
            "pr": None,
            "comment": None,
        },
        None,
    )


async def register_task_from_webhook(task: Dict[str, Any]) -> tuple[bool, Dict[str, Any] | None]:
    created = await store_task(task, create_only=True)
    if not created:
        existing_task = await load_task(task["task_id"])
        if existing_task is not None:
            await store_task(existing_task, create_only=False)
    issue = await sync_issue_projection(task["queue_key"])
    return created, issue


async def reconcile_processing_tasks() -> Dict[str, int]:
    stats = {
        "task_count": 0,
        "reingested_sessions": 0,
        "needs_human": 0,
        "containers_removed": 0,
        "volumes_removed": 0,
    }
    recovery_note = "Service restarted while this task was processing. Human review is required before resuming."

    for task in await list_all_tasks():
        if task.get("status") != "processing":
            continue
        stats["task_count"] += 1
        cleanup = await asyncio.to_thread(_cleanup_worker_runtime_for_task, task)
        stats["containers_removed"] += int(bool(cleanup.get("container_removed")))
        stats["volumes_removed"] += int(bool(cleanup.get("volume_removed")))

        artifact_payload, artifact_error = _load_worker_artifact_payload(task)
        if artifact_payload is not None:
            await _finalize_task_session(task, artifact_payload, cleanup.get("logs", ""), recovered=True)
            stats["reingested_sessions"] += 1
            continue

        task["status"] = "needs_human"
        task["completed_at"] = now_utc().isoformat()
        task["needs_human_at"] = task["completed_at"]
        task["needs_human_reason"] = recovery_note if not artifact_error else f"{recovery_note} {artifact_error}"
        errors = task.get("errors") or []
        errors.append(task["needs_human_reason"])
        task["errors"] = errors[-10:]
        task["last_error"] = task["needs_human_reason"]
        await store_task(task, create_only=False)
        issue = await sync_issue_projection(task["queue_key"])
        if issue:
            await _set_issue_status_label(issue["repo_full_name"], issue["issue_number"], issue["status"])
            await _post_issue_comment(issue["repo_full_name"], issue["issue_number"], task["needs_human_reason"])
        stats["needs_human"] += 1

    return stats


async def recover_processing_tasks() -> int:
    stats = await reconcile_processing_tasks()
    return stats["task_count"]


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
    if LLM_API_URL and DEEP_HEALTH_CHECK_LLM:
        try:
            llm_route = validate_llm_endpoint(
                LLM_API_URL,
                allowlisted_hosts=parse_host_patterns(LLM_HOST_ALLOWLIST_RAW),
                no_proxy_hosts=parse_host_patterns(WORKER_NO_PROXY),
            )
            checks.append(await _check_http_url("llm_endpoint", LLM_API_URL, use_proxy=llm_route.route == "proxy", required=False))
        except NetworkPolicyError as exc:
            checks.append({"name": "llm_endpoint", "ok": False, "error": str(exc), "url": LLM_API_URL, "required": False})

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
    validate_runtime_configuration()
    redis = await get_redis()
    await redis.ping()
    await asyncio.to_thread(get_docker_client().ping)
    WORKER_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    recovery_stats = await reconcile_processing_tasks()
    if recovery_stats["task_count"]:
        print(
            "Recovered processing task state on startup: "
            f"tasks={recovery_stats['task_count']}, "
            f"reingested_sessions={recovery_stats['reingested_sessions']}, "
            f"needs_human={recovery_stats['needs_human']}, "
            f"containers_removed={recovery_stats['containers_removed']}, "
            f"volumes_removed={recovery_stats['volumes_removed']}."
        )
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
    delivery_id = request.headers.get("X-GitHub-Delivery", "").strip()

    try:
        raw_payload = await read_webhook_body(request)
    except PayloadTooLargeError:
        return JSONResponse({"status": "ignored", "reason": "payload_too_large"}, status_code=413)

    if not verify_github_signature(raw_payload, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload: top-level object required")
    action = payload.get("action", "")
    repo_full_name = payload.get("repository", {}).get("full_name", "").strip()
    try:
        await enforce_webhook_rate_limit(repo_full_name)
    except RateLimitExceededError as exc:
        return JSONResponse(
            {"status": "ignored", "reason": "rate_limited"},
            status_code=429,
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    if repo_full_name and not is_repo_allowed(repo_full_name):
        print(f"Ignoring webhook from non-allowlisted repo: {repo_full_name}")
        return JSONResponse(
            {"status": "ignored", "reason": "repo_not_allowlisted", "repo": repo_full_name},
            status_code=202,
        )

    if github_event not in SUPPORTED_WEBHOOK_ACTIONS:
        return JSONResponse({"status": "ignored", "reason": "unsupported_event", "event_type": github_event}, status_code=202)
    if not is_supported_webhook_action(github_event, action):
        return JSONResponse(
            {"status": "ignored", "reason": "unsupported_action", "event_type": github_event, "action": action},
            status_code=202,
        )
    if not delivery_id:
        raise HTTPException(status_code=400, detail="Missing X-GitHub-Delivery header")
    if github_event == "pull_request":
        pr = payload.get("pull_request", {})
        pr_number = pr.get("number")
        head_sha = (pr.get("head", {}) or {}).get("sha", "")
        if pr_number is None or not head_sha:
            return JSONResponse(
                {"status": "ignored", "reason": "invalid_pull_request_payload", "delivery_id": delivery_id},
                status_code=202,
            )
        stale_count = await _mark_pull_request_tasks_stale(repo_full_name, pr_number, head_sha, delivery_id)
        return JSONResponse(
            {
                "status": "received",
                "event_type": github_event,
                "action": action,
                "delivery_id": delivery_id,
                "stale_task_count": stale_count,
            }
        )

    task, ignore_reason = await build_task_from_event(github_event, payload, action, delivery_id)
    if task is None:
        return JSONResponse(
            {"status": "ignored", "reason": ignore_reason or "unsupported_payload", "delivery_id": delivery_id},
            status_code=202,
        )

    created, issue = await register_task_from_webhook(task)
    if issue:
        await _set_issue_status_label(issue["repo_full_name"], issue["issue_number"], issue["status"])
    if not created:
        return JSONResponse({"status": "ignored", "reason": "duplicate_delivery", "delivery_id": delivery_id}, status_code=202)

    print(f"Queued task {task['task_id']} for {task['queue_key']} ({github_event}, trigger={task['trigger_type']})")
    if task["trigger_type"] == "auto":
        background_tasks.add_task(notify_slack, github_event, task["issue_number"], task["title"])
    return JSONResponse({"status": "received", "task_id": task["task_id"], "queue_key": task["queue_key"]})


@app.get("/api/issues")
async def get_queue():
    return {"issues": await list_issues()}


@app.get("/api/issues/{queue_key}")
async def get_issue(queue_key: str):
    issue = await load_issue(queue_key)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    return issue


@app.get("/api/tasks")
async def get_tasks():
    return {"tasks": await list_all_tasks()}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    task = await load_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


async def _approve_task(
    task_id: str,
    background_tasks: BackgroundTasks,
    x_admin_token: str | None,
) -> Dict[str, Any]:
    verify_admin_token(x_admin_token)
    task = await load_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] not in {"queued", "needs_human"}:
        raise HTTPException(status_code=409, detail=f"Cannot approve task from state {task['status']}")
    queue_key = task["queue_key"]
    task, issue = await transition_task(
        queue_key,
        task,
        "approved",
        preferred_task_id=task_id,
        approved_at=now_utc().isoformat(),
        next_retry_at=None,
        last_error=None,
        needs_human_at=None,
        needs_human_reason=None,
    )
    if issue:
        await _set_issue_status_label(issue["repo_full_name"], issue["issue_number"], issue["status"])
        await _post_issue_comment(
            issue["repo_full_name"],
            issue["issue_number"],
            f"Issue task `{task['task_id']}` approved for agent execution. Waiting for worker start.",
        )
    background_tasks.add_task(run_agent_for_task, task_id)
    return {"status": "approved", "task": task, "issue": issue}


async def _reject_task(task_id: str, x_admin_token: str | None) -> Dict[str, Any]:
    verify_admin_token(x_admin_token)
    task = await load_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] not in {"queued", "approved", "needs_human"}:
        raise HTTPException(status_code=409, detail=f"Cannot reject task from state {task['status']}")
    queue_key = task["queue_key"]
    task, issue = await transition_task(
        queue_key,
        task,
        "rejected",
        preferred_task_id=task_id,
        rejected_at=now_utc().isoformat(),
        next_retry_at=None,
    )
    if issue:
        await _set_issue_status_label(issue["repo_full_name"], issue["issue_number"], issue["status"])
        await _post_issue_comment(
            issue["repo_full_name"],
            issue["issue_number"],
            f"Issue task `{task['task_id']}` rejected for agent execution.",
        )
    return {"status": "rejected", "task": task, "issue": issue}


async def _requeue_task(task_id: str, x_admin_token: str | None) -> Dict[str, Any]:
    verify_admin_token(x_admin_token)
    task = await load_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] not in {"dead_letter", "needs_human"}:
        raise HTTPException(status_code=409, detail=f"Cannot requeue task from state {task['status']}")
    queue_key = task["queue_key"]
    task, issue = await transition_task(
        queue_key,
        task,
        "approved",
        preferred_task_id=task_id,
        approved_at=now_utc().isoformat(),
        attempt_count=0,
        next_retry_at=None,
        last_error=None,
        errors=[],
        dead_lettered_at=None,
        needs_human_at=None,
        needs_human_reason=None,
    )
    if issue:
        await _set_issue_status_label(issue["repo_full_name"], issue["issue_number"], issue["status"])
        await _post_issue_comment(
            issue["repo_full_name"],
            issue["issue_number"],
            f"Issue task `{task['task_id']}` manually requeued for agent execution.",
        )
    asyncio.create_task(run_agent_for_task(task_id))
    return {"status": "requeued", "task": task, "issue": issue}


@app.post("/api/tasks/{task_id}/approve")
async def approve_task(
    task_id: str,
    background_tasks: BackgroundTasks,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    return await _approve_task(task_id, background_tasks, x_admin_token)


@app.post("/api/tasks/{task_id}/reject")
async def reject_task(
    task_id: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    return await _reject_task(task_id, x_admin_token)


@app.post("/api/tasks/{task_id}/requeue")
async def requeue_task(
    task_id: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    return await _requeue_task(task_id, x_admin_token)


@app.post("/api/issues/{queue_key}/approve")
async def approve_issue(
    queue_key: str,
    background_tasks: BackgroundTasks,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    issue, task = await load_current_task_for_issue(queue_key)
    if not issue or not task:
        raise HTTPException(status_code=404, detail="Issue not found")
    if issue.get("subject_kind") == "pull_request":
        raise HTTPException(status_code=409, detail="Use /api/tasks/{task_id}/approve for pull request tasks.")
    return await _approve_task(task["task_id"], background_tasks, x_admin_token)


@app.post("/api/issues/{queue_key}/reject")
async def reject_issue(
    queue_key: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    issue, task = await load_current_task_for_issue(queue_key)
    if not issue or not task:
        raise HTTPException(status_code=404, detail="Issue not found")
    if issue.get("subject_kind") == "pull_request":
        raise HTTPException(status_code=409, detail="Use /api/tasks/{task_id}/reject for pull request tasks.")
    return await _reject_task(task["task_id"], x_admin_token)


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
    issue, task = await load_current_task_for_issue(queue_key)
    if not issue or not task:
        raise HTTPException(status_code=404, detail="Issue not found")
    if issue.get("subject_kind") == "pull_request":
        raise HTTPException(status_code=409, detail="Use /api/tasks/{task_id}/requeue for pull request tasks.")
    return await _requeue_task(task["task_id"], x_admin_token)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
