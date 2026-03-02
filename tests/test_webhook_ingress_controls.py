import hashlib
import hmac
import unittest

import webhook_handler as wh


class FakeRequest:
    def __init__(self, payload: bytes):
        self._payload = payload

    async def body(self) -> bytes:
        return self._payload


class FakeRedis:
    def __init__(self):
        self.kv = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        return True

    async def get(self, key):
        return self.kv.get(key)


class WebhookIngressControlsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.orig_runtime_env = wh.RUNTIME_ENV
        self.orig_webhook_secret = wh.WEBHOOK_SECRET
        self.orig_redis_client = wh.redis_client

    async def asyncTearDown(self):
        wh.RUNTIME_ENV = self.orig_runtime_env
        wh.WEBHOOK_SECRET = self.orig_webhook_secret
        wh.redis_client = self.orig_redis_client

    async def test_production_requires_non_empty_webhook_secret(self):
        wh.RUNTIME_ENV = "production"
        wh.WEBHOOK_SECRET = ""
        with self.assertRaises(RuntimeError):
            wh.validate_runtime_configuration()

        wh.WEBHOOK_SECRET = "set"
        wh.validate_runtime_configuration()

    async def test_signature_verification_fails_closed_without_secret_in_production(self):
        wh.RUNTIME_ENV = "production"
        wh.WEBHOOK_SECRET = ""
        is_valid = await wh.verify_github_signature(FakeRequest(b"{}"), signature="")
        self.assertFalse(is_valid)

    async def test_signature_verification_validates_hmac(self):
        payload = b'{"action":"opened"}'
        wh.RUNTIME_ENV = "production"
        wh.WEBHOOK_SECRET = "test-secret"
        digest = hmac.new(wh.WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
        signature = f"sha256={digest}"

        self.assertTrue(await wh.verify_github_signature(FakeRequest(payload), signature))
        self.assertFalse(await wh.verify_github_signature(FakeRequest(payload), "sha256=bad"))

    async def test_delivery_id_deduplication(self):
        wh.redis_client = FakeRedis()
        self.assertTrue(await wh.claim_delivery_id("delivery-1"))
        self.assertFalse(await wh.claim_delivery_id("delivery-1"))
        self.assertTrue(await wh.claim_delivery_id("delivery-2"))

    async def test_supported_action_filter(self):
        self.assertTrue(wh.is_supported_webhook_action("issues", "opened"))
        self.assertTrue(wh.is_supported_webhook_action("pull_request", "synchronize"))
        self.assertFalse(wh.is_supported_webhook_action("issues", "deleted"))
        self.assertFalse(wh.is_supported_webhook_action("unknown", "opened"))


if __name__ == "__main__":
    unittest.main()
