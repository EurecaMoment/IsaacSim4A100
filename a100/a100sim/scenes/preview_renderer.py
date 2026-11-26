"""Render an interactive view from the production USD geometry and materials."""

from __future__ import annotations

import math
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from queue import LifoQueue

import drjit as dr
import mitsuba as mi
import numpy as np
from a100sim.usd_renderer import add_stage_geometry, world_matrix
from PIL import Image
from pxr import Usd, UsdGeom


class DirectPreview(mi.SamplingIntegrator):
    """Deterministic direct lighting from the USD emitters plus diffuse environment fill."""

    def sample(self, scene, sampler, ray, medium=None, active=True):
        si = scene.ray_intersect(ray, active=active)
        hit = si.is_valid() & active
        bsdf = si.bsdf(ray)
        color = si.emitter(scene).eval(si, active)
        for emitter in scene.emitters():
            if emitter.is_environment():
                # Uniform dome radiance integrated against a Lambertian cosine.
                radiance = emitter.eval(si, hit)
                color += bsdf.eval_diffuse_reflectance(si, hit) * radiance
                continue
            ds, weight = emitter.sample_direction(si, mi.Point2f(0.5, 0.5), hit)
            visible = hit & (ds.pdf > 0)
            visible &= ~scene.ray_test(si.spawn_ray_to(ds.p), visible)
            value = bsdf.eval(mi.BSDFContext(), si, si.to_local(ds.d), visible)
            color += dr.select(visible, weight * value, 0)
        return color, hit, []


@dataclass
class PreparedPreview:
    scene: mi.Scene
    cameras: dict
    size: int
    channels: LifoQueue
    sensor_configs: dict


class PreviewEngine:
    def prepare(self, stage_path: Path, *, size: int) -> PreparedPreview:
        if not 64 <= size <= 1024:
            raise ValueError("Interactive preview size must be between 64 and 1024")
        stage = Usd.Stage.Open(str(stage_path))
        if not stage:
            raise ValueError(f"Could not open scene: {stage_path}")
        cache = UsdGeom.XformCache(Usd.TimeCode.Default())
        cameras = {}
        for prim in Usd.PrimRange.Stage(stage, Usd.TraverseInstanceProxies()):
            if not prim.IsA(UsdGeom.Camera):
                continue
            camera = UsdGeom.Camera(prim)
            if camera.GetProjectionAttr().Get() != "perspective":
                continue
            matrix = world_matrix(prim, cache)
            fov = math.degrees(
                2
                * math.atan(
                    float(camera.GetHorizontalApertureAttr().Get()) / (2 * float(camera.GetFocalLengthAttr().Get()))
                )
            )
            cameras[str(prim.GetPath())] = {
                "type": "perspective",
                "fov": fov,
                "fov_axis": "x",
                "to_world": mi.ScalarTransform4f.look_at(
                    origin=matrix[:3, 3], target=matrix[:3, 3] - matrix[:3, 2], up=matrix[:3, 1]
                ),
                "film": {
                    "type": "hdrfilm",
                    "width": size,
                    "height": size,
                    "pixel_format": "rgb",
                    "rfilter": {"type": "box"},
                },
            }
        if not cameras:
            raise ValueError("Scene contains no perspective cameras")
        scene = {"type": "scene", "integrator": DirectPreview(mi.Properties())}
        add_stage_geometry(scene, stage, "mesh", cache)
        sensors = {path: mi.load_dict(config) for path, config in cameras.items()}
        channels = LifoQueue()
        # Two independent films keep each scene responsive during bursts of new views.
        for _ in range(2):
            channels.put(DirectPreview(mi.Properties()))
        return PreparedPreview(mi.load_dict(scene), sensors, size, channels, cameras)

    def render(self, scene: PreparedPreview, camera: str, *, size: int, spp: int = 1, yaw_degrees: float = 0) -> bytes:
        if size != scene.size or not 1 <= spp <= 4:
            raise ValueError("Invalid preview resolution or samples per pixel")
        if camera not in scene.cameras:
            raise ValueError(f"Unknown perspective camera: {camera}")
        # Independent films and integrators let multiple camera requests trace simultaneously.
        integrator = scene.channels.get()
        try:
            config = dict(scene.sensor_configs[camera])
            if yaw_degrees:
                config["to_world"] = config["to_world"] @ mi.ScalarTransform4f.rotate([0, 1, 0], yaw_degrees)
            sensor = mi.load_dict(config)
            image = mi.render(scene.scene, sensor=sensor, integrator=integrator, spp=spp, seed=42)
            image = dr.maximum(image, 0)
            image = image / (1 + image)
            bitmap = mi.Bitmap(image).convert(mi.Bitmap.PixelFormat.RGB, mi.Struct.Type.UInt8, True)
            pixels = np.array(bitmap)
        finally:
            scene.channels.put(integrator)
        encoded = BytesIO()
        Image.fromarray(pixels).save(encoded, format="PNG", compress_level=0)
        return encoded.getvalue()
