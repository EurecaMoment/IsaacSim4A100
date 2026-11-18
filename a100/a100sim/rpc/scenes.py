"""Connect registered scene operations to a simulator-owned loopback worker.

No Kit/CUDA imports belong in this transport module. Asset handles are resolved
by the same AssetStore as UploadAsset; requests never contain executable code.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import grpc
from a100sim.network import require_worker_url
from a100sim.rpc.backend import RegisteredOperation, SimulatorBackend
from a100sim.rpc.config import Settings
from a100sim.rpc.errors import ServiceError
from a100sim.rpc.storage import AssetStore
from a100sim.rpc.v1 import simulator_pb2 as pb
from a100sim.service_queue import persist

OPERATIONS = {
    "asset.import": "Import an uploaded USD into a retained independent scene session",
    "scene.edit": "Apply bounded transforms and supported scalar attributes before simulation",
    "physics.step": "Advance and retain one scene using CUDA PhysX",
    "world.observe": "Read measured CUDA body velocities from the last physics step",
    "scene.inspect": "Read paged USD hierarchy, world transforms and collider attributes",
    "scene.capture": "Render up to 32 authored cameras from the same retained physical state",
    "scene.export": "Export the retained flattened USD and optional dependency package",
    "scene.close": "Close the session while preserving its immutable artifacts",
}


class SceneBackend(SimulatorBackend):
    """Use the existing gateway's durable jobs and artifact download contract."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        settings.backend_urls["scenes"] = require_worker_url(os.environ["A100SIM_SCENE_URL"])
        self.assets = AssetStore(settings.asset_root, settings.max_upload_bytes)
        for name, scope in OPERATIONS.items():

            async def handler(arguments: dict[str, Any], directory: Path, operation: str = name) -> dict[str, Any]:
                return await self.scene_task(operation, arguments, directory)

            self.register(name, RegisteredOperation(handler, scope, {"type": "object"}))

    async def describe(self) -> list[pb.Capability]:
        """Advertise scene operations only when their independent worker is ready."""
        capabilities = await super().describe()
        ready = (await self.health()).get("scenes", {}).get("status") == "ready"
        for item in capabilities:
            if item.name.removeprefix("extension.") in OPERATIONS:
                item.availability = pb.AVAILABLE if ready else pb.UNAVAILABLE
                item.reason = "" if ready else "The scene worker is not ready"
        return capabilities

    async def scene_task(self, operation: str, arguments: dict[str, Any], directory: Path) -> dict[str, Any]:
        """Resolve uploaded assets and await exactly one accepted worker job."""
        if operation not in OPERATIONS:
            raise ValueError("Unknown scene operation")
        if {"operation", "source_path"} & arguments.keys():
            raise ValueError("operation and source_path are reserved worker fields")
        payload = dict(arguments)
        if operation == "asset.import":
            source = payload.pop("source", None)
            if not isinstance(source, dict) or set(source) != {"asset_id", "relative_path"}:
                raise ValueError("source requires asset_id and relative_path")
            payload["source_path"] = str(self.assets.resolve(**source))
        accepted = await self._request("scenes", "/scene", {**payload, "operation": operation})
        job_id = accepted["id"]
        # Once accepted, retain the upstream identity and never submit it again.
        persist(directory / "scene-worker.json", {"id": job_id, "operation": operation})
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            value = await self._request("scenes", "/jobs/" + job_id + "?wait_ms=1000", timeout=5)
            if value["state"] == "succeeded":
                return value["result"]
            if value["state"] in {"failed", "interrupted"}:
                raise ServiceError(grpc.StatusCode.FAILED_PRECONDITION, value.get("error", "Scene worker failed"))
            await asyncio.sleep(0.01)
        raise ServiceError(
            grpc.StatusCode.DEADLINE_EXCEEDED,
            "Scene work is still retained on the simulator; inspect the worker receipt",
        )
