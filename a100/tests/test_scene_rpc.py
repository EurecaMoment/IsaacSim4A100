"""Exercise retained-scene transport without claiming Kit, physics or GPU coverage."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import grpc
from a100sim.rpc.config import Settings
from a100sim.rpc.errors import ServiceError
from a100sim.rpc.jobs import JobManager
from a100sim.rpc.scenes import OPERATIONS, SceneBackend
from a100sim.rpc.v1 import simulator_pb2 as pb
from a100sim.scenes.service import SceneQueue
from a100sim.service_queue import start_http


class SceneTransportTests(unittest.IsolatedAsyncioTestCase):
    """Protect operation routing, uploaded asset boundaries and durable receipts."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = Settings(self.root / "state", self.root / "assets", (self.root / "results",), {}, self.root)
        with patch.dict("os.environ", A100SIM_SCENE_URL="http://127.0.0.1:18324"):
            self.backend = SceneBackend(self.settings)

    async def test_scene_capabilities_follow_worker_readiness(self) -> None:
        self.backend.health = AsyncMock(return_value={"scenes": {"status": "initializing"}})
        for status, expected in (("initializing", pb.UNAVAILABLE), ("ready", pb.AVAILABLE)):
            self.backend.health.return_value = {"scenes": {"status": status}}
            catalog = await self.backend.describe()
            extensions = {item.name: item.availability for item in catalog if item.name.startswith("extension.")}
            self.assertEqual(extensions, {"extension." + name: expected for name in OPERATIONS})

    async def test_import_resolves_uploaded_asset_through_real_worker_queue(self) -> None:
        received = []

        class SessionFixture:
            """Return a small transport artifact without importing the simulator."""

            def execute(self, payload: dict[str, Any], directory: Path) -> dict[str, Any]:
                """Record worker input and create one real output file."""
                received.append(payload)
                artifact = directory / "fixture.json"
                artifact.write_text('{"transport_fixture":true}', encoding="utf-8")
                return {"session_id": "a" * 32, "report": str(artifact)}

        state = SceneQueue(self.root / "results", 2)
        state.sessions = SessionFixture()
        state.ready = True
        http = start_http(state, "127.0.0.1", 0, "/scene")
        self.settings.backend_urls["scenes"] = f"http://127.0.0.1:{http.server_port}"
        worker = threading.Thread(target=lambda: state.run_one(state.jobs.get()), daemon=True)
        worker.start()
        source = self.settings.asset_root / "uploaded" / "scene.usda"
        source.parent.mkdir(parents=True)
        source.write_text("#usda 1.0\n", encoding="utf-8")
        manager = JobManager(self.settings, self.backend)
        await manager.start()
        try:
            request = pb.SubmitTaskRequest(
                request_id="scene-import",
                extension=pb.ExtensionRequest(
                    operation="asset.import",
                    arguments={
                        "source": {"asset_id": "uploaded", "relative_path": "scene.usda"},
                        "path": "/World/Scene",
                    },
                ),
            )
            job = await manager.submit(request)
            async with asyncio.timeout(5):
                async for job in manager.observe(job.job_id):
                    pass
            self.assertEqual(job.state, pb.SUCCEEDED, str(job.failure))
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]["operation"], "asset.import")
            self.assertEqual(Path(received[0]["source_path"]), source.resolve())
            self.assertNotIn("source", received[0])
            self.assertEqual(len(job.artifacts), 1)
            self.assertTrue(job.result["report"].startswith("artifact://"))
            self.assertEqual((await manager.submit(request)).job_id, job.job_id)
            self.assertEqual(len(received), 1)
        finally:
            await manager.close()
            await asyncio.to_thread(http.shutdown)
            http.server_close()
            worker.join(timeout=1)

    async def test_reserved_fields_and_path_escapes_never_reach_worker(self) -> None:
        self.backend._request = AsyncMock()
        for arguments in (
            {"operation": "scene.close"},
            {"source_path": "/etc/passwd"},
            {"source": {"asset_id": "uploaded", "relative_path": "../private.usda"}},
            {"source": {"asset_id": "uploaded", "relative_path": "x", "extra": True}},
            {},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                await self.backend.scene_task("asset.import", arguments, self.root)
        self.backend._request.assert_not_awaited()

    async def test_poll_failure_preserves_receipt_without_duplicate_submission(self) -> None:
        self.backend._request = AsyncMock(
            side_effect=[{"id": "b" * 32}, ServiceError(grpc.StatusCode.UNAVAILABLE, "Worker unavailable")]
        )
        with self.assertRaises(ServiceError):
            await self.backend.scene_task("physics.step", {"session_id": "a" * 32, "steps": 1}, self.root)
        receipt = json.loads((self.root / "scene-worker.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt, {"id": "b" * 32, "operation": "physics.step"})
        calls = self.backend._request.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[1], "/scene")
        self.assertTrue(calls[1].args[1].startswith("/jobs/" + "b" * 32))

    async def test_interrupted_worker_job_fails_explicitly(self) -> None:
        self.backend._request = AsyncMock(side_effect=[{"id": "b" * 32}, {"state": "interrupted"}])
        with self.assertRaises(ServiceError) as failure:
            await self.backend.scene_task("scene.inspect", {"session_id": "a" * 32}, self.root)
        self.assertEqual(failure.exception.code, grpc.StatusCode.FAILED_PRECONDITION)


if __name__ == "__main__":
    unittest.main()
