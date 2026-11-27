"""protobuf 消息与 HTTP JSON 之间的转换。"""

from __future__ import annotations

from typing import Any

from . import rpc

pb = rpc.simulation_pb2


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _vec3(value: Any) -> list[float]:
    return [float(value.x), float(value.y), float(value.z)]


def _quat(value: Any) -> list[float]:
    return [float(value.x), float(value.y), float(value.z), float(value.w)]


# ---------------------------------------------------------------------------
# 请求：protobuf -> HTTP JSON
# ---------------------------------------------------------------------------


def _light_to_dict(light: Any) -> dict[str, Any]:
    item: dict[str, Any] = {}
    if light.HasField("color"):
        item["color"] = _vec3(light.color)
    for field in ("intensity", "exposure", "width", "height"):
        if light.HasField(field):
            item[field] = float(getattr(light, field))
    if light.HasField("origin"):
        item["origin"] = _vec3(light.origin)
    if light.HasField("target"):
        item["target"] = _vec3(light.target)
    return item


def _body_to_dict(body: Any) -> dict[str, Any]:
    item: dict[str, Any] = {}
    if body.id:
        item["id"] = body.id
    if body.shape:
        item["shape"] = body.shape
    if body.HasField("position"):
        item["position"] = _vec3(body.position)
    if body.HasField("orientation_xyzw"):
        item["orientation_xyzw"] = _quat(body.orientation_xyzw)
    if body.HasField("size"):
        item["size"] = _vec3(body.size)
    if body.HasField("radius"):
        item["radius"] = float(body.radius)
    if body.HasField("mass"):
        item["mass"] = float(body.mass)
    if body.HasField("color"):
        item["color"] = _vec3(body.color)
    if body.HasField("asset"):
        item["asset"] = body.asset
    if body.HasField("asset_id"):
        item["asset_id"] = body.asset_id
    return item


def _render_to_dict(render: Any) -> dict[str, Any]:
    item: dict[str, Any] = {}
    if render.HasField("kind"):
        item["kind"] = render.kind
    for field in ("size", "spp", "seed"):
        if render.HasField(field):
            item[field] = int(getattr(render, field))
    for field in ("azimuth", "elevation", "distance", "gaussian_gain"):
        if render.HasField(field):
            item[field] = float(getattr(render, field))
    if render.HasField("target"):
        item["target"] = _vec3(render.target)
    if render.views:
        views = []
        for view in render.views:
            data = {"id": view.id}
            for name in ("eye", "target", "up"):
                if view.HasField(name):
                    data[name] = _vec3(getattr(view, name))
            if view.HasField("fov_y_degrees"):
                data["fov_y_degrees"] = view.fov_y_degrees
            views.append(data)
        item["views"] = views
    if render.HasField("lighting"):
        lighting: dict[str, Any] = {}
        if render.lighting.HasField("dome"):
            lighting["dome"] = _light_to_dict(render.lighting.dome)
        if render.lighting.HasField("key"):
            lighting["key"] = _light_to_dict(render.lighting.key)
        if render.lighting.fills:
            lighting["fills"] = [_light_to_dict(fill) for fill in render.lighting.fills]
        item["lighting"] = lighting
    return item


def simulate_request_to_payload(request: Any) -> dict[str, Any]:
    """把 SimulateRequest 转成上游 /simulate 接受的 JSON。

    未设置的字段不写入 payload，由上游套用默认值。
    """
    payload: dict[str, Any] = {}
    if request.bodies:
        payload["bodies"] = [_body_to_dict(body) for body in request.bodies]
    if request.HasField("steps"):
        payload["steps"] = int(request.steps)
    if request.HasField("dt"):
        payload["dt"] = float(request.dt)
    if request.HasField("gravity"):
        payload["gravity"] = float(request.gravity)
    if request.HasField("ground"):
        payload["ground"] = bool(request.ground)
    if request.HasField("render"):
        payload["render"] = _render_to_dict(request.render)
    if request.HasField("capture"):
        capture: dict[str, Any] = {}
        if request.capture.HasField("every"):
            capture["every"] = int(request.capture.every)
        if request.capture.frames:
            capture["frames"] = [int(frame) for frame in request.capture.frames]
        payload["capture"] = capture
    return payload


# ---------------------------------------------------------------------------
# 响应：HTTP JSON -> protobuf
# ---------------------------------------------------------------------------


def health_to_proto(record: dict[str, Any]) -> Any:
    message = pb.HealthResponse(
        status=str(record.get("status", "")),
        engine=str(record.get("engine", "")),
        physical_gpu=_to_int(record.get("physical_gpu")),
        cuda_required=bool(record.get("cuda_required", False)),
        cpu_fallback=bool(record.get("cpu_fallback", False)),
        native_rtx=bool(record.get("native_rtx", False)),
        render_backend=str(record.get("render_backend", "")),
        gpu_model=str(record.get("gpu_model") or ""),
        gpu_uuid=str(record.get("gpu_uuid") or ""),
        isaac_sim=str(record.get("isaac_sim", "")),
        renderer=str(record.get("renderer", "")),
        uptime_seconds=_to_float(record.get("uptime_seconds")),
        queue_depth=_to_int(record.get("queue_depth")),
        queue_capacity=_to_int(record.get("queue_capacity")),
        active_job=str(record.get("active_job") or ""),
        completed=_to_int(record.get("completed")),
        failed=_to_int(record.get("failed")),
        error=str(record.get("error") or ""),
    )
    message.supported_shapes.extend(record.get("supported_shapes") or [])
    message.annotators.extend(record.get("annotators") or [])
    message.api_version = str(record.get("api_version") or "")
    message.artifacts_root = str(record.get("artifacts_root") or "")
    message.artifacts_free_bytes = _to_int(record.get("artifacts_free_bytes"))
    message.retention_seconds = _to_int(record.get("retention_seconds"))
    message.max_views = _to_int(record.get("max_views"))
    message.capabilities.extend(record.get("capabilities") or [])
    return message


def job_handle_to_proto(record: dict[str, Any]) -> Any:
    return pb.JobHandle(
        id=str(record.get("id", "")),
        state=str(record.get("state", "")),
        queued_at=_to_float(record.get("queued_at")),
    )


def _artifacts_to_proto(record: dict[str, Any]) -> Any:
    render = record.get("render") or {}

    def pick(*candidates: Any) -> str:
        for value in candidates:
            if value:
                return str(value)
        return ""

    return _with_frames(pb.Artifacts(
        state_npz=pick(record.get("state_npz")),
        scene=pick(record.get("scene")),
        simulation_json=pick(record.get("simulation_result")),
        image=pick(record.get("image"), render.get("image")),
        depth_m=pick(record.get("depth_m"), render.get("depth_m")),
        semantic_ids=pick(record.get("semantic_ids"), render.get("semantic_ids")),
        semantic_labels=pick(record.get("semantic_labels"), render.get("semantic_labels")),
    ), record)


def _with_frames(artifacts: Any, record: dict[str, Any]) -> Any:
    for item in record.get("frames") or []:
        frame = pb.FrameArtifact(
            frame=int(item.get("frame", 0)),
            state_npz=str(item.get("state_npz", "")),
        )
        for key in ("image", "depth_m", "semantic_ids", "semantic_labels"):
            value = item.get(key)
            if value:
                setattr(frame, key, str(value))
        artifacts.frames.append(frame)
    return artifacts


def _render_info_to_proto(record: dict[str, Any]) -> Any:
    return pb.RenderInfo(
        backend=str(record.get("backend", "")),
        renderer=str(record.get("renderer", "")),
        native_rtx=bool(record.get("native_rtx", False)),
        size=_to_int(record.get("size")),
        requested_spp=_to_int(record.get("requested_spp")),
        rt_subframes=_to_int(record.get("rt_subframes")),
        sampling=str(record.get("sampling", "")),
        image=str(record.get("image", "")),
        depth_m=str(record.get("depth_m", "")),
        depth_convention=str(record.get("depth_convention", "")),
        semantic_ids=str(record.get("semantic_ids", "")),
        semantic_labels=str(record.get("semantic_labels", "")),
        rgb_mean=_to_float(record.get("rgb_mean")),
        seconds=_to_float(record.get("seconds")),
    )


def job_to_proto(record: dict[str, Any]) -> Any:
    message = pb.Job(
        id=str(record.get("id", "")),
        state=str(record.get("state", "")),
        queued_at=_to_float(record.get("queued_at")),
        started_at=_to_float(record.get("started_at")),
        finished_at=_to_float(record.get("finished_at")),
        queue_seconds=_to_float(record.get("queue_seconds")),
        execution_seconds=_to_float(record.get("execution_seconds")),
        error=str(record.get("error") or ""),
        isaac_sim=str(record.get("isaac_sim", "")),
        physics_device=str(record.get("physics_device", "")),
        physical_gpu=_to_int(record.get("physical_gpu")),
        native_rtx=bool(record.get("native_rtx", False)),
        cpu_fallback=bool(record.get("cpu_fallback", False)),
        rigid_bodies=_to_int(record.get("rigid_bodies")),
        steps=_to_int(record.get("steps")),
        dt=_to_float(record.get("dt")),
        simulation_seconds=_to_float(record.get("simulation_seconds")),
        setup_seconds=_to_float(record.get("setup_seconds")),
        physics_seconds=_to_float(record.get("physics_seconds")),
        collision=str(record.get("collision", "")),
    )
    for body in record.get("bodies") or []:
        message.bodies.append(
            pb.BodyState(
                id=str(body.get("id", "")),
                prim_path=str(body.get("prim_path", "")),
                pose_xyzw=[float(v) for v in body.get("pose_xyzw") or []],
                velocity=[float(v) for v in body.get("velocity") or []],
            )
        )
    # 进度字段
    message.phase = str(record.get("phase") or "")
    message.frames_done = _to_int(record.get("frames_done"))
    message.frames_total = _to_int(record.get("frames_total"))

    # 产物清单（新契约的下载句柄）
    for artifact in record.get("artifact_manifest") or []:
        message.artifacts.append(pb.Artifact(
            artifact_id=str(artifact.get("artifact_id", "")),
            name=str(artifact.get("name", "")),
            relative_path=str(artifact.get("relative_path", "")),
            size_bytes=_to_int(artifact.get("size_bytes")),
            sha256=str(artifact.get("sha256", "")),
            media_type=str(artifact.get("media_type", "")),
        ))

    # 遗留的绝对路径产物（field 22，已更名为 outputs，供兼容）
    artifacts = _artifacts_to_proto(record)
    if any(
        (
            artifacts.state_npz,
            artifacts.scene,
            artifacts.simulation_json,
            artifacts.image,
            artifacts.depth_m,
            artifacts.semantic_ids,
            artifacts.semantic_labels,
        )
    ):
        message.outputs.CopyFrom(artifacts)
    if record.get("render"):
        message.render.CopyFrom(_render_info_to_proto(record["render"]))
    return message
