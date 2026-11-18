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
from a100sim.scenes.contracts import OPERATIONS, PARAMETER_SCHEMAS
from a100sim.service_queue import persist

OBSERVATION_TIMEOUT_SECONDS = 1800
POLL_TIMEOUT_SECONDS = 5
TRANSIENT_OBSERVATION_CODES = frozenset(
    {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.RESOURCE_EXHAUSTED}
)


class SceneBackend(SimulatorBackend):
    """Use the existing gateway's durable jobs and artifact download contract."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        settings.backend_urls["scenes"] = require_worker_url(os.environ["A100SIM_SCENE_URL"])
        self.assets = AssetStore(settings.asset_root, settings.max_upload_bytes)
        for name, scope in OPERATIONS.items():

            async def handler(arguments: dict[str, Any], directory: Path, operation: str = name) -> dict[str, Any]:
                return await self.scene_task(operation, arguments, directory)

            self.register(name, RegisteredOperation(handler, scope, PARAMETER_SCHEMAS[name]))

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
        receipt = {"id": job_id, "operation": operation}
        persist(directory / "scene-worker.json", receipt)
        try:
            async with asyncio.timeout(OBSERVATION_TIMEOUT_SECONDS):
                return await self._observe_scene(job_id)
        except TimeoutError as error:
            persist(directory / "scene-worker.json", {**receipt, "observation": "deadline_exceeded", "retained": True})
            raise ServiceError(
                grpc.StatusCode.DEADLINE_EXCEEDED,
                "Scene observation exceeded its deadline; accepted work is still retained on the simulator. "
                "Inspect the worker receipt; do not resubmit the task.",
            ) from error
        except asyncio.CancelledError:
            # Gateway shutdown cancels observation, not GPU execution. Preserve
            # the accepted identity and let cancellation reach the job manager.
            persist(directory / "scene-worker.json", {**receipt, "observation": "cancelled", "retained": True})
            raise

    async def _observe_scene(self, job_id: str) -> dict[str, Any]:
        """Retry transient status reads only, within the caller's observation budget."""
        deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
        retry_delay = 0.25
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                value = await self._request(
                    "scenes", "/jobs/" + job_id + "?wait_ms=1000", timeout=min(POLL_TIMEOUT_SECONDS, remaining)
                )
            except ServiceError as error:
                if error.code not in TRANSIENT_OBSERVATION_CODES:
                    raise
                # A busy scene thread can miss the HTTP deadline while the GPU
                # succeeds. Retry this read, never repeat the accepted mutation.
                await asyncio.sleep(min(retry_delay, max(0, deadline - time.monotonic())))
                retry_delay = min(2.0, retry_delay * 2)
                continue
            retry_delay = 0.25
            if value["state"] == "succeeded":
                return value["result"]
            if value["state"] in {"failed", "interrupted"}:
                raise ServiceError(grpc.StatusCode.FAILED_PRECONDITION, value.get("error", "Scene worker failed"))
            await asyncio.sleep(min(0.01, max(0, deadline - time.monotonic())))
        raise TimeoutError("Scene observation budget expired")
