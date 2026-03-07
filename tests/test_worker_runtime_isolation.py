import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import webhook_handler as wh


def make_task(task_id: str = "delivery-worker-1") -> dict:
    return {
        "task_id": task_id,
        "queue_key": "pastoriomarco:agentic-dev-system:51",
        "subject_kind": "issue",
        "trigger_source": "issues",
        "event_type": "issues",
        "event_action": "opened",
        "delivery_id": task_id,
        "issue_number": 51,
        "title": "Worker isolation test",
        "body": "Run safely",
        "is_pr": False,
        "trigger_type": "manual",
        "status": "approved",
        "sender": "tester",
        "repo_full_name": "pastoriomarco/agentic-dev-system",
        "repo_clone_url": "https://github.com/pastoriomarco/agentic-dev-system.git",
    }


class FakeVolume:
    def __init__(self, name: str):
        self.name = name
        self.removed = False

    def remove(self, force: bool = False):
        self.removed = True


class FakeVolumes:
    def __init__(self):
        self.created: list[FakeVolume] = []

    def create(self, name: str):
        volume = FakeVolume(name)
        self.created.append(volume)
        return volume


class FakeContainer:
    def __init__(self, status_code: int = 0, logs: bytes = b""):
        self.status_code = status_code
        self._logs = logs
        self.removed = False

    def wait(self, timeout: int | None = None):
        return {"StatusCode": self.status_code}

    def logs(self, stdout: bool = True, stderr: bool = True):
        return self._logs

    def remove(self, force: bool = False):
        self.removed = True


class FakeContainers:
    def __init__(self):
        self.calls: list[dict] = []

    def run(self, image: str, **kwargs):
        self.calls.append({"image": image, **kwargs})
        return FakeContainer()


class FakeDockerClient:
    def __init__(self):
        self.volumes = FakeVolumes()
        self.containers = FakeContainers()


class WorkerRuntimeIsolationTest(unittest.TestCase):
    def setUp(self):
        self.orig_artifacts_dir = wh.WORKER_ARTIFACTS_DIR
        self.orig_worker_enable_host_gateway = wh.WORKER_ENABLE_HOST_GATEWAY
        self.orig_worker_run_as_uid = wh.WORKER_RUN_AS_UID
        self.orig_worker_run_as_gid = wh.WORKER_RUN_AS_GID
        self.tempdir = tempfile.TemporaryDirectory()
        wh.WORKER_ARTIFACTS_DIR = Path(self.tempdir.name)
        wh.WORKER_ENABLE_HOST_GATEWAY = False
        wh.WORKER_RUN_AS_UID = 1000
        wh.WORKER_RUN_AS_GID = 1000

    def tearDown(self):
        wh.WORKER_ARTIFACTS_DIR = self.orig_artifacts_dir
        wh.WORKER_ENABLE_HOST_GATEWAY = self.orig_worker_enable_host_gateway
        wh.WORKER_RUN_AS_UID = self.orig_worker_run_as_uid
        wh.WORKER_RUN_AS_GID = self.orig_worker_run_as_gid
        self.tempdir.cleanup()

    def test_run_worker_container_uses_non_root_user_without_host_gateway_by_default(self):
        fake_client = FakeDockerClient()

        with patch("webhook_handler.get_docker_client", return_value=fake_client):
            session_payload, _logs = wh._run_worker_container(make_task())

        self.assertEqual(session_payload["status"], "failed")
        self.assertEqual(len(fake_client.containers.calls), 2)
        init_call, worker_call = fake_client.containers.calls
        self.assertEqual(init_call["user"], "0:0")
        self.assertIn("chown 1000:1000", " ".join(init_call["command"]))
        self.assertEqual(worker_call["user"], "1000:1000")
        self.assertNotIn("extra_hosts", worker_call)

    def test_run_worker_container_adds_host_gateway_only_when_enabled(self):
        fake_client = FakeDockerClient()
        wh.WORKER_ENABLE_HOST_GATEWAY = True

        with patch("webhook_handler.get_docker_client", return_value=fake_client):
            wh._run_worker_container(make_task("delivery-worker-2"))

        _init_call, worker_call = fake_client.containers.calls
        self.assertEqual(worker_call["extra_hosts"], {"host.docker.internal": "host-gateway"})


if __name__ == "__main__":
    unittest.main()
