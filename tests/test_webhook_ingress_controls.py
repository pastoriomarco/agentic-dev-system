import hashlib
import hmac
import json
import unittest

import httpx

import webhook_handler as wh


class FakeRequest:
    def __init__(self, payload: bytes):
        self._payload = payload

    async def body(self) -> bytes:
        return self._payload


class FakeRedis:
    def __init__(self):
        self.kv = {}
        self.zsets = {}
        self.expiry = {}

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

    async def incr(self, key):
        value = int(self.kv.get(key, "0")) + 1
        self.kv[key] = str(value)
        return value

    async def expire(self, key, seconds):
        self.expiry[key] = seconds
        return True

    async def ping(self):
        return True

    async def aclose(self):
        return None


class WebhookIngressControlsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.orig_runtime_env = wh.RUNTIME_ENV
        self.orig_webhook_secret = wh.WEBHOOK_SECRET
        self.orig_admin_token = wh.ADMIN_API_TOKEN
        self.orig_redis_client = wh.redis_client
        self.orig_github_token = wh.GITHUB_TOKEN
        self.orig_fetch_pr_context = wh._fetch_pull_request_context
        self.orig_webhook_max_body_bytes = wh.WEBHOOK_MAX_BODY_BYTES
        self.orig_rate_limit_window_seconds = wh.WEBHOOK_RATE_LIMIT_WINDOW_SECONDS
        self.orig_rate_limit_global_max = wh.WEBHOOK_RATE_LIMIT_GLOBAL_MAX
        self.orig_rate_limit_repo_max = wh.WEBHOOK_RATE_LIMIT_REPO_MAX

        wh.redis_client = FakeRedis()
        wh.WEBHOOK_SECRET = "test-secret"
        wh.RUNTIME_ENV = "production"
        wh.ADMIN_API_TOKEN = "admin-token"
        wh.GITHUB_TOKEN = ""
        wh.WEBHOOK_MAX_BODY_BYTES = 262144
        wh.WEBHOOK_RATE_LIMIT_WINDOW_SECONDS = 60
        wh.WEBHOOK_RATE_LIMIT_GLOBAL_MAX = 120
        wh.WEBHOOK_RATE_LIMIT_REPO_MAX = 60

    async def asyncTearDown(self):
        wh.RUNTIME_ENV = self.orig_runtime_env
        wh.WEBHOOK_SECRET = self.orig_webhook_secret
        wh.ADMIN_API_TOKEN = self.orig_admin_token
        wh.redis_client = self.orig_redis_client
        wh.GITHUB_TOKEN = self.orig_github_token
        wh._fetch_pull_request_context = self.orig_fetch_pr_context
        wh.WEBHOOK_MAX_BODY_BYTES = self.orig_webhook_max_body_bytes
        wh.WEBHOOK_RATE_LIMIT_WINDOW_SECONDS = self.orig_rate_limit_window_seconds
        wh.WEBHOOK_RATE_LIMIT_GLOBAL_MAX = self.orig_rate_limit_global_max
        wh.WEBHOOK_RATE_LIMIT_REPO_MAX = self.orig_rate_limit_repo_max

    def _signed_headers(self, payload: bytes, delivery_id: str, event: str = "issues") -> dict:
        digest = hmac.new(wh.WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
        return {
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": f"sha256={digest}",
            "Content-Type": "application/json",
        }

    async def test_production_requires_non_empty_webhook_and_admin_secrets(self):
        wh.RUNTIME_ENV = "production"
        wh.WEBHOOK_SECRET = ""
        with self.assertRaises(RuntimeError):
            wh.validate_runtime_configuration()

        wh.WEBHOOK_SECRET = "set"
        wh.ADMIN_API_TOKEN = ""
        with self.assertRaises(RuntimeError):
            wh.validate_runtime_configuration()

        wh.ADMIN_API_TOKEN = "set"
        wh.validate_runtime_configuration()

    async def test_signature_verification_fails_closed_without_secret_in_production(self):
        wh.RUNTIME_ENV = "production"
        wh.WEBHOOK_SECRET = ""
        is_valid = wh.verify_github_signature(b"{}", signature="")
        self.assertFalse(is_valid)

    async def test_signature_verification_validates_hmac(self):
        payload = b'{"action":"opened"}'
        wh.RUNTIME_ENV = "production"
        wh.WEBHOOK_SECRET = "test-secret"
        digest = hmac.new(wh.WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
        signature = f"sha256={digest}"

        self.assertTrue(wh.verify_github_signature(payload, signature))
        self.assertFalse(wh.verify_github_signature(payload, "sha256=bad"))

    async def test_payload_too_large_is_rejected_from_content_length(self):
        wh.WEBHOOK_MAX_BODY_BYTES = 32
        payload = b'{"action":"opened"}'
        headers = self._signed_headers(payload, "delivery-large-header")

        async with httpx.AsyncClient(app=wh.app, base_url="http://test") as client:
            request = client.build_request("POST", "/webhook/github", content=payload, headers=headers)
            request.headers["Content-Length"] = str(wh.WEBHOOK_MAX_BODY_BYTES + 1)
            response = await client.send(request)

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["reason"], "payload_too_large")
        self.assertIsNone(await wh.load_task("delivery-large-header"))
        self.assertIsNone(await wh.load_issue("pastoriomarco:agentic-dev-system:1"))

    async def test_payload_too_large_is_rejected_from_actual_body_size(self):
        wh.WEBHOOK_MAX_BODY_BYTES = 96
        payload_obj = {
            "action": "opened",
            "repository": {"full_name": "pastoriomarco/agentic-dev-system", "clone_url": "https://github.com/pastoriomarco/agentic-dev-system.git"},
            "sender": {"login": "tester"},
            "issue": {"number": 1, "title": "Too big", "body": "x" * 512},
        }
        payload = json.dumps(payload_obj).encode("utf-8")
        headers = self._signed_headers(payload, "delivery-large-body")

        async with httpx.AsyncClient(app=wh.app, base_url="http://test") as client:
            request = client.build_request("POST", "/webhook/github", content=payload, headers=headers)
            request.headers["Content-Length"] = "32"
            response = await client.send(request)

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["reason"], "payload_too_large")
        self.assertIsNone(await wh.load_task("delivery-large-body"))
        self.assertIsNone(await wh.load_issue("pastoriomarco:agentic-dev-system:1"))

    async def test_webhook_persists_task_before_response_and_deduplicates_delivery(self):
        payload_obj = {
            "action": "opened",
            "repository": {"full_name": "pastoriomarco/agentic-dev-system", "clone_url": "https://github.com/pastoriomarco/agentic-dev-system.git"},
            "sender": {"login": "tester"},
            "issue": {"number": 7, "title": "Add safety check", "body": "Please implement this"},
        }
        payload = json.dumps(payload_obj).encode("utf-8")
        headers = self._signed_headers(payload, "delivery-7")

        async with httpx.AsyncClient(app=wh.app, base_url="http://test") as client:
            response = await client.post("/webhook/github", content=payload, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["task_id"], "delivery-7")

            stored_task = await wh.load_task("delivery-7")
            self.assertIsNotNone(stored_task)
            stored_issue = await wh.load_issue("pastoriomarco:agentic-dev-system:7")
            self.assertIsNotNone(stored_issue)
            self.assertEqual(stored_issue["current_task_id"], "delivery-7")
            self.assertEqual(stored_issue["task_count"], 1)

            duplicate = await client.post("/webhook/github", content=payload, headers=headers)
            self.assertEqual(duplicate.status_code, 202)
            self.assertEqual(duplicate.json()["reason"], "duplicate_delivery")

            stored_issue = await wh.load_issue("pastoriomarco:agentic-dev-system:7")
            self.assertEqual(stored_issue["task_count"], 1)

    async def test_global_rate_limit_rejects_before_task_creation(self):
        wh.WEBHOOK_RATE_LIMIT_GLOBAL_MAX = 1
        wh.WEBHOOK_RATE_LIMIT_REPO_MAX = 10

        first_payload = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "pastoriomarco/agentic-dev-system", "clone_url": "https://github.com/pastoriomarco/agentic-dev-system.git"},
                "sender": {"login": "tester"},
                "issue": {"number": 21, "title": "First", "body": "one"},
            }
        ).encode("utf-8")
        second_payload = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "pastoriomarco/agentic-dev-system", "clone_url": "https://github.com/pastoriomarco/agentic-dev-system.git"},
                "sender": {"login": "tester"},
                "issue": {"number": 22, "title": "Second", "body": "two"},
            }
        ).encode("utf-8")
        first_headers = self._signed_headers(first_payload, "delivery-global-1")
        second_headers = self._signed_headers(second_payload, "delivery-global-2")

        async with httpx.AsyncClient(app=wh.app, base_url="http://test") as client:
            first = await client.post("/webhook/github", content=first_payload, headers=first_headers)
            second = await client.post("/webhook/github", content=second_payload, headers=second_headers)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["reason"], "rate_limited")
        retry_after = int(second.headers["Retry-After"])
        self.assertGreaterEqual(retry_after, 1)
        self.assertLessEqual(retry_after, wh.WEBHOOK_RATE_LIMIT_WINDOW_SECONDS)
        self.assertIsNotNone(await wh.load_task("delivery-global-1"))
        self.assertIsNone(await wh.load_task("delivery-global-2"))
        self.assertIsNone(await wh.load_issue("pastoriomarco:agentic-dev-system:22"))

    async def test_repo_rate_limit_rejects_before_task_creation(self):
        wh.WEBHOOK_RATE_LIMIT_GLOBAL_MAX = 10
        wh.WEBHOOK_RATE_LIMIT_REPO_MAX = 1

        first_payload = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "pastoriomarco/agentic-dev-system", "clone_url": "https://github.com/pastoriomarco/agentic-dev-system.git"},
                "sender": {"login": "tester"},
                "issue": {"number": 31, "title": "First", "body": "one"},
            }
        ).encode("utf-8")
        second_payload = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "pastoriomarco/agentic-dev-system", "clone_url": "https://github.com/pastoriomarco/agentic-dev-system.git"},
                "sender": {"login": "tester"},
                "issue": {"number": 32, "title": "Second", "body": "two"},
            }
        ).encode("utf-8")
        first_headers = self._signed_headers(first_payload, "delivery-repo-1")
        second_headers = self._signed_headers(second_payload, "delivery-repo-2")

        async with httpx.AsyncClient(app=wh.app, base_url="http://test") as client:
            first = await client.post("/webhook/github", content=first_payload, headers=first_headers)
            second = await client.post("/webhook/github", content=second_payload, headers=second_headers)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["reason"], "rate_limited")
        retry_after = int(second.headers["Retry-After"])
        self.assertGreaterEqual(retry_after, 1)
        self.assertLessEqual(retry_after, wh.WEBHOOK_RATE_LIMIT_WINDOW_SECONDS)
        self.assertIsNotNone(await wh.load_task("delivery-repo-1"))
        self.assertIsNone(await wh.load_task("delivery-repo-2"))
        self.assertIsNone(await wh.load_issue("pastoriomarco:agentic-dev-system:32"))

    async def test_pull_request_issue_comment_without_trigger_is_ignored(self):
        payload_obj = {
            "action": "created",
            "repository": {"full_name": "pastoriomarco/agentic-dev-system", "clone_url": "https://github.com/pastoriomarco/agentic-dev-system.git"},
            "sender": {"login": "tester"},
            "issue": {
                "number": 9,
                "title": "PR context",
                "body": "",
                "pull_request": {"url": "https://api.github.com/repos/pastoriomarco/agentic-dev-system/pulls/9"},
            },
            "comment": {"body": "please take a look"},
        }
        payload = json.dumps(payload_obj).encode("utf-8")
        headers = self._signed_headers(payload, "delivery-pr-comment-ignored", event="issue_comment")

        async with httpx.AsyncClient(app=wh.app, base_url="http://test") as client:
            response = await client.post("/webhook/github", content=payload, headers=headers)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["reason"], "pull_request_comment_without_agent_trigger")
        self.assertIsNone(await wh.load_task("delivery-pr-comment-ignored"))

    async def test_pull_request_issue_comment_creates_task_with_pr_context(self):
        async def fake_fetch_pr_context(_repo_full_name: str, pr_number: int) -> dict:
            return {
                "number": pr_number,
                "title": "Improve null handling",
                "body": "Original PR body",
                "html_url": f"https://github.com/pastoriomarco/agentic-dev-system/pull/{pr_number}",
                "same_repo": True,
                "maintainer_can_modify": True,
                "head_repo_full_name": "pastoriomarco/agentic-dev-system",
                "head_repo_clone_url": "https://github.com/pastoriomarco/agentic-dev-system.git",
                "head_ref": "feature/fix-null",
                "head_sha": "abc123def4567890",
                "base_repo_full_name": "pastoriomarco/agentic-dev-system",
                "base_ref": "main",
                "base_sha": "def456abc1237890",
            }

        wh._fetch_pull_request_context = fake_fetch_pr_context
        payload_obj = {
            "action": "created",
            "repository": {"full_name": "pastoriomarco/agentic-dev-system", "clone_url": "https://github.com/pastoriomarco/agentic-dev-system.git"},
            "sender": {"login": "tester"},
            "issue": {
                "number": 10,
                "title": "Improve null handling",
                "body": "",
                "pull_request": {"url": "https://api.github.com/repos/pastoriomarco/agentic-dev-system/pulls/10"},
            },
            "comment": {"id": 501, "body": "@agent please fix the branch", "html_url": "https://github.com/example/comment/501"},
        }
        payload = json.dumps(payload_obj).encode("utf-8")
        headers = self._signed_headers(payload, "delivery-pr-comment", event="issue_comment")

        async with httpx.AsyncClient(app=wh.app, base_url="http://test") as client:
            response = await client.post("/webhook/github", content=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        task = await wh.load_task("delivery-pr-comment")
        self.assertIsNotNone(task)
        self.assertEqual(task["subject_kind"], "pull_request")
        self.assertEqual(task["pr"]["head_ref"], "feature/fix-null")
        self.assertEqual(task["comment"]["comment_id"], 501)

    async def test_pull_request_synchronize_marks_open_tasks_needs_human(self):
        queue_key = "pastoriomarco:agentic-dev-system:17"
        created_at = wh.now_utc().isoformat()
        task = {
            "task_id": "delivery-pr-old",
            "queue_key": queue_key,
            "subject_kind": "pull_request",
            "trigger_source": "pr_issue_comment",
            "event_type": "issue_comment",
            "event_action": "created",
            "delivery_id": "delivery-pr-old",
            "issue_number": 17,
            "title": "PR task",
            "body": "@agent fix this",
            "is_pr": True,
            "trigger_type": "manual",
            "status": "approved",
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
            "output_pr": {"number": 17, "url": "https://github.com/pastoriomarco/agentic-dev-system/pull/17"},
            "approved_at": created_at,
            "started_at": None,
            "completed_at": None,
            "retried_at": None,
            "rejected_at": None,
            "dead_lettered_at": None,
            "needs_human_at": None,
            "needs_human_reason": None,
            "pr": {
                "number": 17,
                "title": "PR task",
                "body": "",
                "html_url": "https://github.com/pastoriomarco/agentic-dev-system/pull/17",
                "same_repo": True,
                "maintainer_can_modify": True,
                "head_repo_full_name": "pastoriomarco/agentic-dev-system",
                "head_repo_clone_url": "https://github.com/pastoriomarco/agentic-dev-system.git",
                "head_ref": "feature/fix-pr",
                "head_sha": "aaaaaaaaaaaa111111111111",
                "base_repo_full_name": "pastoriomarco/agentic-dev-system",
                "base_ref": "main",
                "base_sha": "bbbbbbbbbbbb222222222222",
            },
            "comment": {"comment_id": 1, "body": "@agent fix this"},
        }
        await wh.store_task(task, create_only=False)
        await wh.sync_issue_projection(queue_key)

        payload_obj = {
            "action": "synchronize",
            "repository": {"full_name": "pastoriomarco/agentic-dev-system"},
            "pull_request": {
                "number": 17,
                "head": {"sha": "cccccccccccc333333333333"},
            },
        }
        payload = json.dumps(payload_obj).encode("utf-8")
        headers = self._signed_headers(payload, "delivery-pr-sync", event="pull_request")

        async with httpx.AsyncClient(app=wh.app, base_url="http://test") as client:
            response = await client.post("/webhook/github", content=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["stale_task_count"], 1)
        task = await wh.load_task("delivery-pr-old")
        self.assertEqual(task["status"], "needs_human")
        self.assertIn("Pull request head changed", task["needs_human_reason"])

    async def test_supported_action_filter(self):
        self.assertTrue(wh.is_supported_webhook_action("issues", "opened"))
        self.assertTrue(wh.is_supported_webhook_action("issue_comment", "created"))
        self.assertTrue(wh.is_supported_webhook_action("pull_request", "synchronize"))
        self.assertFalse(wh.is_supported_webhook_action("pull_request", "opened"))
        self.assertFalse(wh.is_supported_webhook_action("issues", "deleted"))


if __name__ == "__main__":
    unittest.main()
