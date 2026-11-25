"""Capture deterministic primary-surface annotations through Mitsuba.

These are custom Mitsuba outputs. They do not invoke RTX sensors or Replicator.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def prepare_surface_ids(scene: Any, stage: Any, sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Build shape-ID lookups shared by all cameras in this snapshot."""
    import drjit as dr
    import mitsuba as mi
    import numpy as np

    shapes = scene.shapes()
    scalar = mi.variant() == "scalar_rgb"
    # Scalar Mitsuba has Python shape objects and integer scalar types, rather
    # than the JIT pointer registry exposed by `ShapePtr` in CUDA/LLVM variants.
    identifiers = (np.arange(1, len(shapes) + 1, dtype=np.uint32) if scalar else
                   np.asarray(dr.reinterpret_array(mi.UInt32, mi.ShapePtr(shapes)), dtype=np.uint32))
    if len(identifiers) and (not identifiers.all() or len(np.unique(identifiers)) != len(shapes)):
        raise RuntimeError("Rendered shapes must have distinct nonzero registry IDs")
    by_identifier = {item["prim"].replace("/", "_"): item["prim"] for item in sources}
    by_identifier.update({item["prim"]: item["prim"] for item in sources})
    classes, labels, scalar_ids = {"unlabeled": 0}, {}, {}
    length = int(identifiers.max()) + 1 if len(identifiers) else 1
    instance_lut, semantic_lut = np.zeros(length, np.uint32), np.zeros(length, np.uint32)
    for number, (identifier, shape) in enumerate(zip(identifiers, shapes), start=1):
        path = by_identifier.get(shape.id())
        if path is None:
            raise RuntimeError(f"Unable to map rendered shape to USD: {shape.id()}")
        prim = stage.GetPrimAtPath(path)
        label = "unlabeled"
        while prim and not prim.IsPseudoRoot():
            attribute = prim.GetAttribute("a100sim:semanticLabel")
            if attribute and attribute.Get():
                label = str(attribute.Get())
                break
            prim = prim.GetParent()
        semantic_id = classes.setdefault(label, len(classes))
        instance_lut[identifier], semantic_lut[identifier] = number, semantic_id
        labels[str(number)] = {"prim_path": path, "semantic_id": semantic_id, "semantic_label": label}
        scalar_ids[shape.id()] = (number, semantic_id)
    return {"instances": labels, "classes": {str(value): key for key, value in classes.items()},
            "instance_lut": instance_lut if scalar else mi.UInt32(instance_lut),
            "semantic_lut": semantic_lut if scalar else mi.UInt32(semantic_lut),
            "scalar_ids": scalar_ids}


def _capture_scalar_surfaces(
    scene: Any, sensor: Any, camera_world: Any, meters: float,
    width: int, height: int, surface_ids: dict[str, Any],
) -> dict[str, Any]:
    """Trace pixel centers using the scalar API, without JIT-only arrays."""
    import math

    import mitsuba as mi
    import numpy as np

    arrays = {
        "depth_m": np.zeros((height, width), dtype=np.float32),
        "range_m": np.zeros((height, width), dtype=np.float32),
        "normal_world": np.zeros((height, width, 3), dtype=np.float32),
        "instance_id": np.zeros((height, width), dtype=np.uint32),
        "semantic_id": np.zeros((height, width), dtype=np.uint32),
        "valid": np.zeros((height, width), dtype=np.bool_),
    }
    origin = camera_world[:3, 3]
    forward = -camera_world[:3, 2]
    shape_ids = surface_ids["scalar_ids"]
    sample_ray, ray_intersect = sensor.sample_ray, scene.ray_intersect
    aperture = mi.Point2f(0.5)
    for y in range(height):
        for x in range(width):
            coordinates = mi.Point2f((x + 0.5) / width, (y + 0.5) / height)
            ray, _ = sample_ray(0.0, 0.5, coordinates, aperture)
            intersection = ray_intersect(ray)
            if not intersection.is_valid():
                continue
            dx = float(intersection.p.x) - origin[0]
            dy = float(intersection.p.y) - origin[1]
            dz = float(intersection.p.z) - origin[2]
            instance_id, semantic_id = shape_ids[intersection.shape.id()]
            arrays["depth_m"][y, x] = (dx * forward[0] + dy * forward[1] + dz * forward[2]) * meters
            arrays["range_m"][y, x] = math.sqrt(dx * dx + dy * dy + dz * dz) * meters
            arrays["normal_world"][y, x] = intersection.n
            arrays["instance_id"][y, x] = instance_id
            arrays["semantic_id"][y, x] = semantic_id
            arrays["valid"][y, x] = True
    return arrays


def capture_primary_surfaces(
    scene: Any, stage: Any, sources: list[dict[str, Any]], camera_world: Any, prefix: Path,
    sensor: Any = None, surface_ids: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Save metric depth, world normals, and explicit USD instance/semantic IDs.

    Rays use pixel centers without motion blur. Depth is camera-forward Z in
    meters, range is Euclidean distance from the camera center. Misses are zero.
    Semantic classes must be authored on a prim or ancestor with the custom
    ``a100sim:semanticLabel`` string attribute. Unlabeled surfaces use ID zero.
    """
    import drjit as dr
    import mitsuba as mi
    import numpy as np
    from PIL import Image
    from pxr import UsdGeom

    if mi.variant() not in {"cuda_ad_rgb", "llvm_ad_rgb", "scalar_rgb"}:
        raise RuntimeError("Annotations require a configured Mitsuba rendering backend")
    expected_backend = {
        "cuda_ad_rgb": dr.JitBackend.CUDA,
        "llvm_ad_rgb": dr.JitBackend.LLVM,
        "scalar_rgb": dr.JitBackend.Invalid,
    }[mi.variant()]
    if expected_backend != dr.JitBackend.Invalid and not dr.has_backend(expected_backend):
        raise RuntimeError(f"The selected Mitsuba annotation backend is unavailable: {mi.variant()}")
    sensor = sensor or scene.sensors()[0]
    width, height = [int(value) for value in sensor.film().crop_size()]
    meters = float(UsdGeom.GetStageMetersPerUnit(stage))
    surface_ids = surface_ids if surface_ids is not None else prepare_surface_ids(scene, stage, sources)
    labels, classes = surface_ids["instances"], surface_ids["classes"]
    if mi.variant() == "scalar_rgb":
        arrays = _capture_scalar_surfaces(scene, sensor, camera_world, meters, width, height, surface_ids)
    else:
        pixels = dr.arange(mi.UInt32, width * height)
        coordinates = mi.Point2f((mi.Float(pixels % width) + 0.5) / width,
                                 (mi.Float(pixels // width) + 0.5) / height)
        rays, _ = sensor.sample_ray(0.0, 0.5, coordinates, mi.Point2f(0.5))
        intersections = scene.ray_intersect(rays)
        valid = intersections.is_valid()
        delta = intersections.p - mi.Point3f(camera_world[:3, 3])
        depth = dr.select(valid, dr.dot(delta, mi.Vector3f(-camera_world[:3, 2])) * meters, 0.0)
        distance = dr.select(valid, dr.norm(delta) * meters, 0.0)
        normal = dr.select(valid, intersections.n, mi.Normal3f(0))
        registry_ids = dr.reinterpret_array(mi.UInt32, intersections.shape)
        instances = dr.gather(mi.UInt32, surface_ids["instance_lut"], registry_ids, valid)
        semantics = dr.gather(mi.UInt32, surface_ids["semantic_lut"], registry_ids, valid)
        dr.eval(depth, distance, normal, instances, semantics, valid)
        dr.sync_thread()
        arrays = {
            "depth_m": np.asarray(depth).reshape(height, width),
            "range_m": np.asarray(distance).reshape(height, width),
            "normal_world": np.asarray(normal).T.reshape(height, width, 3),
            "instance_id": np.asarray(instances).reshape(height, width),
            "semantic_id": np.asarray(semantics).reshape(height, width),
            "valid": np.asarray(valid).reshape(height, width),
        }
    np.savez_compressed(str(prefix) + "-annotations.npz", **arrays)
    metadata = {
        "backend": {"cuda_ad_rgb": "Mitsuba CUDA primary-ray intersections",
                    "llvm_ad_rgb": "Mitsuba LLVM CPU primary-ray intersections",
                    "scalar_rgb": "Mitsuba scalar CPU primary-ray intersections"}[mi.variant()],
        "render_backend": mi.variant(),
        "native_rtx": False,
        "replicator": False,
        "resolution": [width, height],
        "depth_unit": "meter",
        "depth_convention": "camera-forward Z",
        "range_convention": "distance from camera center",
        "normal_frame": "world",
        "sampling": "one ray per pixel center, no motion blur",
        "miss_value": 0,
        "instances": labels,
        "classes": classes,
    }
    Path(str(prefix) + "-labels.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    valid_np, depth_np = arrays["valid"], arrays["depth_m"]
    scaled = np.zeros_like(depth_np)
    if valid_np.any():
        near, far = np.percentile(depth_np[valid_np], [2, 98])
        scaled[valid_np] = 1 - np.clip((depth_np[valid_np] - near) / max(far - near, 1e-6), 0, 1)
    Image.fromarray((scaled * 255).astype("uint8")).save(str(prefix) + "-depth.png")
    normal_rgb = np.where(valid_np[:, :, None], arrays["normal_world"] * 0.5 + 0.5, 0)
    Image.fromarray((np.clip(normal_rgb, 0, 1) * 255).astype("uint8")).save(str(prefix) + "-normals.png")
    ids = arrays["instance_id"]
    colors = np.stack([(ids * 67) % 255, (ids * 131) % 255, (ids * 197) % 255], axis=-1).astype("uint8")
    colors[~valid_np] = 0
    Image.fromarray(colors).save(str(prefix) + "-instances.png")
    return {
        "arrays": str(prefix) + "-annotations.npz",
        "labels": str(prefix) + "-labels.json",
        "depth_preview": str(prefix) + "-depth.png",
        "normal_preview": str(prefix) + "-normals.png",
        "instance_preview": str(prefix) + "-instances.png",
        "valid_pixels": int(valid_np.sum()),
        "instance_count": len(labels),
        "class_count": len(classes),
        **metadata,
    }
