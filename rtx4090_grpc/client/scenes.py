"""内置示例场景，以及 JSON 场景文件到 protobuf 的转换。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from server import rpc

pb = rpc.simulation_pb2


def _vec3(values: Any) -> Any:
    x, y, z = values
    return pb.Vec3(x=float(x), y=float(y), z=float(z))


def _quat(values: Any) -> Any:
    x, y, z, w = values
    return pb.QuatXYZW(x=float(x), y=float(y), z=float(z), w=float(w))


def _light(spec: dict[str, Any]) -> Any:
    light = pb.LightSpec()
    if "color" in spec:
        light.color.CopyFrom(_vec3(spec["color"]))
    for field in ("intensity", "exposure", "width", "height"):
        if field in spec:
            setattr(light, field, float(spec[field]))
    if "origin" in spec:
        light.origin.CopyFrom(_vec3(spec["origin"]))
    if "target" in spec:
        light.target.CopyFrom(_vec3(spec["target"]))
    return light


def request_from_dict(data: dict[str, Any]) -> Any:
    """把与 HTTP 一致的 JSON 场景转换为 SimulateRequest。"""
    request = pb.SimulateRequest()
    for raw in data.get("bodies") or []:
        body = request.bodies.add()
        body.id = str(raw.get("id", ""))
        body.shape = str(raw.get("shape", "cube"))
        if "position" in raw:
            body.position.CopyFrom(_vec3(raw["position"]))
        if "orientation_xyzw" in raw:
            body.orientation_xyzw.CopyFrom(_quat(raw["orientation_xyzw"]))
        if "size" in raw:
            body.size.CopyFrom(_vec3(raw["size"]))
        if "radius" in raw:
            body.radius = float(raw["radius"])
        if "mass" in raw:
            body.mass = float(raw["mass"])
        if "color" in raw:
            body.color.CopyFrom(_vec3(raw["color"]))
        if "asset" in raw:
            body.asset = str(raw["asset"])
    if "steps" in data:
        request.steps = int(data["steps"])
    if "dt" in data:
        request.dt = float(data["dt"])
    if "gravity" in data:
        request.gravity = float(data["gravity"])
    if "ground" in data:
        request.ground = bool(data["ground"])

    render_data = data.get("render")
    if render_data is not None:
        render = request.render
        if "kind" in render_data:
            render.kind = str(render_data["kind"])
        for field in ("size", "spp", "seed"):
            if field in render_data:
                setattr(render, field, int(render_data[field]))
        for field in ("azimuth", "elevation", "distance", "gaussian_gain"):
            if field in render_data:
                setattr(render, field, float(render_data[field]))
        if "target" in render_data:
            render.target.CopyFrom(_vec3(render_data["target"]))
        lighting_data = render_data.get("lighting")
        if lighting_data is not None:
            if "dome" in lighting_data:
                render.lighting.dome.CopyFrom(_light(lighting_data["dome"]))
            if "key" in lighting_data:
                render.lighting.key.CopyFrom(_light(lighting_data["key"]))
            for fill in lighting_data.get("fills") or []:
                render.lighting.fills.append(_light(fill))

    capture_data = data.get("capture")
    if capture_data is not None:
        apply_capture(request, capture_data.get("every"), capture_data.get("frames"))
    return request


def apply_capture(request: Any, every: int | None = None, frames: Any = None) -> Any:
    """给请求追加按帧采样参数（every 与 frames 可同时使用）。

    注意：CaptureSpec 存在即强制逐帧渲染（不再有独立的 render 开关）。
    """
    if every:
        request.capture.every = int(every)
    for frame in frames or ():
        request.capture.frames.append(int(frame))
    return request


def builtin(name: str) -> Any:
    """返回内置场景；未知名称抛 KeyError。"""
    builders = {"cube": _cube, "stack": _stack, "sphere_rain": _sphere_rain}
    return builders[name]()


def load(scene: str) -> Any:
    """scene 可以是内置名称，也可以是 JSON 文件路径。"""
    if scene in {"cube", "stack", "sphere_rain"}:
        return builtin(scene)
    path = Path(scene).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"未知场景名或文件不存在: {scene}")
    import json

    return request_from_dict(json.loads(path.read_text(encoding="utf-8")))


def _cube() -> Any:
    return request_from_dict(
        {
            "bodies": [
                {
                    "id": "blue-cube",
                    "shape": "cube",
                    "position": [-0.25, 0, 1.2],
                    "size": [0.2, 0.2, 0.2],
                    "color": [0.15, 0.4, 0.8],
                },
                {
                    "id": "red-sphere",
                    "shape": "sphere",
                    "position": [0.25, 0, 0.8],
                    "radius": 0.13,
                    "color": [0.8, 0.2, 0.15],
                },
            ],
            "steps": 120,
            "render": {"kind": "mesh", "size": 512, "spp": 32, "seed": 42},
        }
    )


def _stack() -> Any:
    bodies = [
        {
            "id": f"box-{i}",
            "shape": "cube",
            "position": [0, 0, 0.5 + i * 0.21],
            "size": [0.2, 0.2, 0.2],
            "color": [0.1 + i * 0.1, 0.3, 0.9 - i * 0.1],
        }
        for i in range(5)
    ]
    return request_from_dict(
        {"bodies": bodies, "steps": 200, "render": {"kind": "mesh", "size": 512, "spp": 64}}
    )


def _sphere_rain() -> Any:
    bodies = [
        {
            "id": f"ball-{i}",
            "shape": "sphere",
            "position": [(i % 5 - 2) * 0.3, (i // 5 - 2) * 0.3, 2.0 + i * 0.05],
            "radius": 0.08,
            "color": [0.9, 0.6, 0.2],
        }
        for i in range(25)
    ]
    return request_from_dict(
        {"bodies": bodies, "steps": 300, "render": {"kind": "mesh", "size": 512, "spp": 32}}
    )
