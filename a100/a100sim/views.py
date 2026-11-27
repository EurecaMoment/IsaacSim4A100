"""Common multi-camera and frame selectors for GPU simulation backends."""

import re

import numpy as np

from .service_queue import integer

MAX_VIEWS = 32
PORTABLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def camera_views(views):
    if not isinstance(views, list) or not 1 <= len(views) <= MAX_VIEWS:
        raise ValueError("render.views requires between 1 and 32 cameras")
    normalized, ids = [], set()
    for value in views:
        view_id = value.get("id") if isinstance(value, dict) else None
        if not isinstance(view_id, str) or not PORTABLE_ID.fullmatch(view_id) or view_id in ids:
            raise ValueError("Each camera needs a unique portable id")
        ids.add(view_id)
        result = {"id": view_id}
        for name in ("eye", "target", "up"):
            vector = np.asarray(value.get(name, [0, 0, 1] if name == "up" else []), dtype=float)
            if vector.shape != (3,) or not np.isfinite(vector).all():
                raise ValueError(f"Camera {view_id} requires a finite {name} vector")
            result[name] = vector.tolist()
        direction = np.subtract(result["target"], result["eye"])
        if np.linalg.norm(np.cross(direction, result["up"])) < 1e-8:
            raise ValueError(f"Camera {view_id} has a degenerate look-at basis")
        fov = float(value.get("fov_y_degrees", 34))
        if not 0 < fov < 180:
            raise ValueError(f"Camera {view_id} needs fov_y_degrees between 0 and 180")
        result["fov_y_degrees"] = fov
        normalized.append(result)
    return normalized


def prepare_capture(value, steps):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("capture must be an object")
    extra = set(value) - {"every", "frames"}
    if extra:
        raise ValueError("Unsupported capture fields: " + ", ".join(sorted(extra)))
    every, frames = value.get("every"), value.get("frames")
    if every is None and frames is None:
        raise ValueError("capture requires 'every' or 'frames'")
    points = set()
    if every is not None:
        every = integer(every, "capture.every", 1, steps)
        points.update(range(every, steps + 1, every))
    if frames is not None:
        if not isinstance(frames, list) or not frames:
            raise ValueError("capture.frames must be a non-empty array")
        if len(frames) > 1200:
            raise ValueError("capture.frames accepts at most 1200 entries")
        for item in frames:
            points.add(integer(item, "capture.frames[]", 1, steps))
    points.add(steps)  # 尾帧始终保留，与 state.npz 对齐
    return {"every": every, "frames": sorted(points), "render": True}
