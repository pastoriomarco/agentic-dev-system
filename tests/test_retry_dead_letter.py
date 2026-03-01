import unittest

import webhook_handler as wh


class FakeRedis:
    def __init__(self):
        self.kv = {}
        self.zsets = {}

    async def set(self, key, value):
        self.kv[key] = value

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


class RetryDeadLetterFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.orig_redis_client = wh.redis_client
        self.orig_run_worker = wh._run_worker_container
        self.orig_max_retries = wh.MAX_RETRIES
        self.orig_retry_base = wh.RETRY_BASE_DELAY_SECONDS
        self.orig_retry_max = wh.RETRY_MAX_DELAY_SECONDS

        wh.redis_client = FakeRedis()
        wh.MAX_RETRIES = 2
        wh.RETRY_BASE_DELAY_SECONDS = 1
        wh.RETRY_MAX_DELAY_SECONDS = 2

        self.run_count = 0

        def always_fail_worker(_issue):
            self.run_count += 1
            return (
                {
                    "session_id": f"session-{self.run_count}",
                    "status": "failed",
                    "created_at": wh.now_utc().isoformat(),
                    "errors": [f"forced failure #{self.run_count}"],
                    "logs": [],
                },
                "forced worker failure",
            )

        wh._run_worker_container = always_fail_worker

    async def asyncTearDown(self):
        wh.redis_client = self.orig_redis_client
        wh._run_worker_container = self.orig_run_worker
        wh.MAX_RETRIES = self.orig_max_retries
        wh.RETRY_BASE_DELAY_SECONDS = self.orig_retry_base
        wh.RETRY_MAX_DELAY_SECONDS = self.orig_retry_max

    async def test_failed_issue_moves_to_dead_letter_after_max_retries(self):
        queue_key = "pastoriomarco:agentic-dev-system:42"
        issue = {
            "queue_key": queue_key,
            "event_type": "issues",
            "issue_number": 42,
            "title": "Test dead-letter transition",
            "body": "Force failures to test retry flow",
            "is_pr": False,
            "trigger_type": "manual",
            "status": "approved",
            "sender": "tester",
            "repo_full_name": "pastoriomarco/agentic-dev-system",
            "repo_clone_url": "https://github.com/pastoriomarco/agentic-dev-system.git",
            "created_at": wh.now_utc().isoformat(),
            "attempt_count": 0,
            "next_retry_at": None,
            "last_error": None,
            "assigned_agent": None,
            "output_pr": None,
        }
        await wh.store_issue(issue)

        # 1st run -> queued_retry, 2nd run -> queued_retry, 3rd run -> dead_letter
        for _ in range(wh.MAX_RETRIES + 1):
            await wh.run_agent_for_issue(queue_key)
            current = await wh.load_issue(queue_key)
            if current["status"] == "queued_retry":
                current["status"] = "approved"
                current.pop("next_retry_at", None)
                await wh.store_issue(current)

        final_issue = await wh.load_issue(queue_key)
        self.assertEqual(final_issue["status"], "dead_letter")
        self.assertEqual(final_issue["attempt_count"], wh.MAX_RETRIES + 1)
        self.assertIn("forced failure", final_issue.get("last_error", ""))

        sessions = await wh.list_sessions()
        self.assertEqual(len(sessions), wh.MAX_RETRIES + 1)

        dead_letters_response = await wh.get_dead_letter_issues()
        dead_letter_keys = {item["queue_key"] for item in dead_letters_response["issues"]}
        self.assertIn(queue_key, dead_letter_keys)


if __name__ == "__main__":
    unittest.main()
