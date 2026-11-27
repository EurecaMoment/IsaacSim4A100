"""Connect registered scene operations to a simulator-owned loopback worker.

No Kit/CUDA imports belong in this transport module. Asset handles are resolved
by the same AssetStore as UploadAsset; requests never contain executable code.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import struct
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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
PREVIEW_OPERATION = "scene.preview"
PREVIEW_MAX_BYTES = 16 * 1024 * 1024
PREVIEW_PARAMETER_SCHEMA = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
        "camera": {"type": "string", "pattern": "^/", "description": "Authored absolute camera prim path."},
        "size": {"type": "integer", "minimum": 64, "maximum": 1024, "default": 1024},
        "spp": {"type": "integer", "minimum": 1, "maximum": 4, "default": 4},
        "version": {"type": "string", "description": "Optional expected stage version."},
    },
    "required": ["session_id", "camera"],
    "additionalProperties": False,
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

            self.register(name, RegisteredOperation(handler, scope, PARAMETER_SCHEMAS[name]))
        if settings.preview_url:
            self.register(
                PREVIEW_OPERATION,
                RegisteredOperation(self.preview_task, "read_only", PREVIEW_PARAMETER_SCHEMA),
            )

    async def health(self) -> dict[str, dict[str, Any]]:
        """Include the separately hosted preview worker in live capability checks."""
        workers = await super().health()
        if self.settings.preview_url:
            try:
                async with self.session.get(self.settings.preview_url + "/health", timeout=2) as response:
                    payload = await response.json(content_type=None)
                ready = response.status == 200 and payload.get("status") == "ok"
                workers["preview"] = {
                    "status": "ready" if ready else "unavailable",
                    "reason": "" if ready else "The preview worker is not ready",
                }
            except (OSError, asyncio.TimeoutError, ValueError, TypeError):
                workers["preview"] = {"status": "unavailable", "reason": "The preview worker is not ready"}
        return workers

    async def describe(self) -> list[pb.Capability]:
        """Advertise scene operations only when their independent worker is ready."""
        capabilities = await super().describe()
        worker = (await self.health()).get("scenes", {})
        ready = worker.get("status") == "ready"
        for item in capabilities:
            if item.name.removeprefix("extension.") in OPERATIONS:
                item.availability = pb.AVAILABLE if ready else pb.UNAVAILABLE
                item.reason = "" if ready else worker.get("reason", "The scene worker is not ready")
            elif item.name == "extension." + PREVIEW_OPERATION:
                ready = worker.get("preview", {}).get("status") == "ready"
                item.availability = pb.AVAILABLE if ready else pb.UNAVAILABLE
                item.reason = (
                    "" if ready else worker.get("preview", {}).get("reason", "The preview worker is not ready")
                )
        return capabilities

    async def preview_task(self, arguments: dict[str, Any], directory: Path) -> dict[str, Any]:
        """Render and retain one versioned PNG through the private preview worker."""
        if not self.settings.preview_url:
            raise ServiceError(grpc.StatusCode.UNIMPLEMENTED, "The preview worker is not configured")
        session_id = arguments.get("session_id")
        camera = arguments.get("camera")
        if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise ValueError("session_id must be 32 lower-case hexadecimal characters")
        if not isinstance(camera, str) or not camera.startswith("/"):
            raise ValueError("camera must be an authored absolute prim path")
        payload = {"session_id": session_id, "camera": camera}
        for name, default, minimum, maximum in (("size", 1024, 64, 1024), ("spp", 4, 1, 4)):
            value = arguments.get(name, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be an integer")
            if isinstance(value, float) and not value.is_integer():
                raise ValueError(f"{name} must be an integer")
            integer = int(value)
            if not minimum <= integer <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
            payload[name] = integer
        if "version" in arguments:
            version = arguments["version"]
            if not isinstance(version, str) or not version:
                raise ValueError("version must be a nonempty string")
            payload["version"] = version
        request = Request(
            self.settings.preview_url + "/v1/preview",
            json.dumps(payload, allow_nan=False).encode("utf-8"),
            {"Content-Type": "application/json"},
        )
        try:
            response = await asyncio.to_thread(urlopen, request, timeout=120)
        except HTTPError as error:
            code, message = {
                400: (grpc.StatusCode.INVALID_ARGUMENT, "Invalid preview request"),
                404: (grpc.StatusCode.NOT_FOUND, "Preview scene session was not found"),
                409: (grpc.StatusCode.ABORTED, "Preview scene changed while rendering"),
                503: (grpc.StatusCode.RESOURCE_EXHAUSTED, "Preview worker is at capacity"),
            }.get(error.code, (grpc.StatusCode.INTERNAL, "Preview rendering failed"))
            raise ServiceError(code, message) from error
        except (TimeoutError, URLError, OSError) as error:
            raise ServiceError(grpc.StatusCode.UNAVAILABLE, "Preview worker did not respond") from error

        with response:
            if response.headers.get_content_type() != "image/png":
                raise ServiceError(grpc.StatusCode.INTERNAL, "Preview worker returned an unexpected media type")
            version = response.headers.get("X-Scene-Version", "")
            if not version:
                raise ServiceError(grpc.StatusCode.INTERNAL, "Preview worker returned an unversioned image")
            image = response.read(PREVIEW_MAX_BYTES + 1)
        if (
            len(image) > PREVIEW_MAX_BYTES
            or len(image) < 33
            or image[:8] != b"\x89PNG\r\n\x1a\n"
            or struct.unpack(">II", image[16:24]) != (payload["size"],) * 2
            or image[-8:-4] != b"IEND"
        ):
            raise ServiceError(grpc.StatusCode.INTERNAL, "Preview worker returned an incomplete or mismatched PNG")

        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "scene-preview.png"
        temporary = directory / ".scene-preview.png.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(image)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return {"image": str(path), "scene_version": version, "media_type": "image/png"}

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
