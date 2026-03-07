import unittest
from pathlib import Path
import tempfile

from fastapi import BackgroundTasks, HTTPException

import webhook_handler as wh


class FakeRedis:
    def __init__(self):
        self.kv = {}
        self.zsets = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def mget(self, keys):
        return [self.kv.get(key) for key in keys]

    async def zadd(self, key, mapping):
        zset = self.zsets.setdefault(key, {})
        zset.update(mapping)

    async def zrem(self, key, member):
        zset = self.zsets.get(key, {})
        zset.pop(member, None)

    async def zrevrange(self, key, start, stop):
        zset = self.zsets.get(key, {})
        ordered = sorted(zset.items(), key=lambda item: item[1], reverse=True)
        members = [member for member, _ in ordered]
        if stop == -1:
            return members[start:]
        return members[start : stop + 1]

    async def zrangebyscore(self, key, min_score, max_score):
        zset = self.zsets.get(key, {})
        matches = [(member, score) for member, score in zset.items() if min_score <= score <= max_score]
        matches.sort(key=lambda item: item[1])
        return [member for member, _ in matches]

    async def ping(self):
        return True

    async def aclose(self):
        return None


def make_task(task_id: str, queue_key: str, status: str, created_at: str, issue_number: int = 11) -> dict:
    return {
        "task_id": task_id,
        "queue_key": queue_key,
        "subject_kind": "issue",
        "trigger_source": "issues",
        "event_type": "issues",
        "event_action": "opened",
        "delivery_id": task_id,
        "issue_number": issue_number,
        "title": f"Task {task_id}",
        "body": "Test body",
        "is_pr": False,
        "trigger_type": "manual",
        "status": status,
        "sender": "tester",
        "repo_full_name": "pastoriomarco/agentic-dev-system",
        "repo_clone_url": "https://github.com/pastoriomarco/agentic-dev-system.git",
        "created_at": created_at,
        "updated_at": created_at,
        "attempt_count": 0,
        "next_retry_at": None,
        "last_error": None,
        "errors": [],
        "assigned_agent": None,
        "output_pr": None,
        "approved_at": created_at if status in {"approved", "processing"} else None,
        "started_at": created_at if status == "processing" else None,
        "completed_at": None,
        "retried_at": None,
        "rejected_at": None,
        "dead_lettered_at": None,
        "needs_human_at": None,
        "needs_human_reason": None,
        "pr": None,
        "comment": None,
    }


class IssueTaskStateMachineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.orig_redis_client = wh.redis_client
        self.orig_admin_token = wh.ADMIN_API_TOKEN
        self.orig_github_token = wh.GITHUB_TOKEN
        self.orig_cleanup_worker_runtime = wh._cleanup_worker_runtime_for_task
        self.orig_worker_artifacts_dir = wh.WORKER_ARTIFACTS_DIR
        self.tempdir = tempfile.TemporaryDirectory()

        wh.redis_client = FakeRedis()
        wh.ADMIN_API_TOKEN = "admin-token"
        wh.GITHUB_TOKEN = ""
        wh._cleanup_worker_runtime_for_task = lambda _task: {
            "container_removed": False,
            "volume_removed": False,
            "logs": "",
        }
        wh.WORKER_ARTIFACTS_DIR = Path(self.tempdir.name)

    async def asyncTearDown(self):
        wh.redis_client = self.orig_redis_client
        wh.ADMIN_API_TOKEN = self.orig_admin_token
        wh.GITHUB_TOKEN = self.orig_github_token
        wh._cleanup_worker_runtime_for_task = self.orig_cleanup_worker_runtime
        wh.WORKER_ARTIFACTS_DIR = self.orig_worker_artifacts_dir
        self.tempdir.cleanup()

    async def test_invalid_transition_is_rejected_by_admin_endpoint(self):
        queue_key = "pastoriomarco:agentic-dev-system:11"
        task = make_task("delivery-11", queue_key, "queued", wh.now_utc().isoformat())
        await wh.store_task(task, create_only=False)
        await wh.sync_issue_projection(queue_key)

        await wh.reject_issue(queue_key, x_admin_token="admin-token")
        with self.assertRaises(HTTPException) as context:
            await wh.approve_issue(queue_key, BackgroundTasks(), x_admin_token="admin-token")

        self.assertEqual(context.exception.status_code, 409)

    async def test_processing_tasks_recover_to_needs_human(self):
        queue_key = "pastoriomarco:agentic-dev-system:12"
        task = make_task("delivery-12", queue_key, "processing", wh.now_utc().isoformat(), issue_number=12)
        await wh.store_task(task, create_only=False)
        await wh.sync_issue_projection(queue_key)

        recovered = await wh.recover_processing_tasks()
        self.assertEqual(recovered, 1)

        issue, current_task = await wh.load_current_task_for_issue(queue_key)
        self.assertEqual(issue["status"], "needs_human")
        self.assertEqual(current_task["status"], "needs_human")
        self.assertIn("Human review is required", current_task["needs_human_reason"])

    async def test_processing_task_recovery_reingests_session_artifact(self):
        queue_key = "pastoriomarco:agentic-dev-system:15"
        task = make_task("delivery-15", queue_key, "processing", wh.now_utc().isoformat(), issue_number=15)
        task["worker_job_id"] = "job-15"
        runtime = wh._worker_runtime_metadata(task)
        task["worker_container_name"] = runtime["container_name"]
        task["worker_workspace_volume"] = runtime["workspace_volume_name"]
        task["worker_artifact_path"] = str(runtime["artifact_host_path"])
        runtime["artifact_host_path"].write_text(
            '{"session_id":"session-15","status":"completed","created_at":"2026-03-07T12:00:00","output_pr_url":"https://github.com/pastoriomarco/agentic-dev-system/pull/15","logs":["done"]}',
            encoding="utf-8",
        )
        wh._cleanup_worker_runtime_for_task = lambda _task: {
            "container_removed": True,
            "volume_removed": True,
            "logs": "recovered logs",
        }
        await wh.store_task(task, create_only=False)
        await wh.sync_issue_projection(queue_key)

        stats = await wh.reconcile_processing_tasks()

        issue, current_task = await wh.load_current_task_for_issue(queue_key)
        sessions = await wh.list_sessions()
        self.assertEqual(stats["task_count"], 1)
        self.assertEqual(stats["reingested_sessions"], 1)
        self.assertEqual(stats["containers_removed"], 1)
        self.assertEqual(stats["volumes_removed"], 1)
        self.assertEqual(issue["status"], "completed")
        self.assertEqual(current_task["status"], "completed")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], "session-15")
        self.assertIn("recovered logs", sessions[0]["logs"][-1])

    async def test_task_endpoint_can_approve_specific_pull_request_task(self):
        queue_key = "pastoriomarco:agentic-dev-system:14"
        first_task = make_task("delivery-14-a", queue_key, "queued", "2026-03-07T10:00:00", issue_number=14)
        second_task = make_task("delivery-14-b", queue_key, "queued", "2026-03-07T10:01:00", issue_number=14)
        first_task["subject_kind"] = "pull_request"
        first_task["trigger_source"] = "pr_issue_comment"
        first_task["is_pr"] = True
        first_task["pr"] = {
            "number": 14,
            "title": "PR one",
            "body": "",
            "html_url": "https://github.com/pastoriomarco/agentic-dev-system/pull/14",
            "same_repo": True,
            "maintainer_can_modify": True,
            "head_repo_full_name": "pastoriomarco/agentic-dev-system",
            "head_repo_clone_url": "https://github.com/pastoriomarco/agentic-dev-system.git",
            "head_ref": "feature/a",
            "head_sha": "aaaa",
            "base_repo_full_name": "pastoriomarco/agentic-dev-system",
            "base_ref": "main",
            "base_sha": "bbbb",
        }
        first_task["output_pr"] = {"number": 14, "url": "https://github.com/pastoriomarco/agentic-dev-system/pull/14"}
        second_task.update(first_task)
        second_task["task_id"] = "delivery-14-b"
        second_task["delivery_id"] = "delivery-14-b"
        second_task["created_at"] = "2026-03-07T10:01:00"
        second_task["updated_at"] = "2026-03-07T10:01:00"
        second_task["pr"] = {**first_task["pr"], "head_ref": "feature/b", "head_sha": "cccc"}
        await wh.store_task(first_task, create_only=False)
        await wh.store_task(second_task, create_only=False)
        await wh.sync_issue_projection(queue_key)

        response = await wh.approve_task("delivery-14-b", BackgroundTasks(), x_admin_token="admin-token")
        self.assertEqual(response["status"], "approved")

        issue, current_task = await wh.load_current_task_for_issue(queue_key)
        self.assertEqual(current_task["task_id"], "delivery-14-b")
        self.assertEqual(issue["current_task_id"], "delivery-14-b")

    async def test_next_open_task_is_promoted_after_current_task_becomes_terminal(self):
        queue_key = "pastoriomarco:agentic-dev-system:13"
        first_task = make_task("delivery-13-a", queue_key, "approved", "2026-03-07T10:00:00", issue_number=13)
        second_task = make_task("delivery-13-b", queue_key, "queued", "2026-03-07T10:01:00", issue_number=13)
        await wh.store_task(first_task, create_only=False)
        await wh.store_task(second_task, create_only=False)
        await wh.sync_issue_projection(queue_key)

        issue, current_task = await wh.load_current_task_for_issue(queue_key)
        self.assertEqual(current_task["task_id"], "delivery-13-a")
        self.assertEqual(issue["pending_task_count"], 1)

        await wh.transition_task(
            queue_key,
            current_task,
            "rejected",
            rejected_at=wh.now_utc().isoformat(),
        )

        issue, current_task = await wh.load_current_task_for_issue(queue_key)
        self.assertEqual(current_task["task_id"], "delivery-13-b")
        self.assertEqual(issue["status"], "queued")
        self.assertEqual(issue["pending_task_count"], 0)


if __name__ == "__main__":
    unittest.main()
