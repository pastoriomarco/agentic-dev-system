"""
Per-task worker entrypoint.
Runs a single issue in an isolated container and writes session output to /artifacts.
"""

import asyncio
import json
import os
import traceback
from datetime import datetime

from agent_orchestrator import AgentOrchestrator


def _fail_payload(message: str) -> dict:
    return {
        "session_id": "",
        "status": "failed",
        "created_at": datetime.utcnow().isoformat(),
        "started_at": datetime.utcnow().isoformat(),
        "completed_at": datetime.utcnow().isoformat(),
        "errors": [message],
        "logs": [],
    }


async def _run() -> dict:
    issue_raw = os.environ.get("ISSUE_JSON", "")
    output_path = os.environ.get("OUTPUT_PATH", "")
    github_token = os.environ.get("GITHUB_WRITE_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
    repo_url = os.environ.get("TARGET_REPO_URL", "")

    if not issue_raw:
        return _fail_payload("ISSUE_JSON is required")
    if not output_path:
        return _fail_payload("OUTPUT_PATH is required")
    if not repo_url:
        return _fail_payload("TARGET_REPO_URL is required")

    try:
        issue_data = json.loads(issue_raw)
    except Exception as exc:
        return _fail_payload(f"Invalid ISSUE_JSON: {exc}")

    orchestrator = AgentOrchestrator(
        github_token=github_token,
        repo_url=repo_url,
        working_base="/workspace",
    )
    try:
        session = await orchestrator.process_issue(issue_data)
        payload = session.to_dict()
    except Exception as exc:
        payload = _fail_payload(f"Unhandled worker exception: {exc}")
        payload["traceback"] = traceback.format_exc()

    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as file:
            json.dump(payload, file)
    except Exception as exc:
        return _fail_payload(f"Failed to write OUTPUT_PATH: {exc}")

    return payload


if __name__ == "__main__":
    result = asyncio.run(_run())
    print(json.dumps({"status": result.get("status", "unknown"), "session_id": result.get("session_id", "")}))
