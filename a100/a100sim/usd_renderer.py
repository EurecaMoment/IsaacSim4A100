"""Render a live Isaac USD stage without requiring an RTX renderer.

This adapter covers polygon meshes and USD solid primitives, perspective cameras,
constant dome lights and rectangular lights. A mesh may provide a standard
3DGS PLY via a100sim:gaussianAsset. Unsupported render schemas raise an error
instead of silently dropping them. This is an external renderer adapter, not
a replacement implementation of Isaac Replicator or RTX sensor extensions.
"""

import math
import os
import time
from pathlib import Path

import drjit as dr
import mitsuba as mi
import numpy as np
from pxr import Usd, UsdGeom, UsdLux

mi.set_variant(os.environ.get("A100SIM_RENDER_VARIANT", "scalar_rgb"))
from . import hybrid_integrator
from .gaussian import load_gaussians, shape_dict
from .materials import MATERIAL_CHANNELS, read_usd_material, read_mesh_channels


def world_matrix(prim, cache):
    return np.asarray(cache.GetLocalToWorldTransform(prim), dtype=np.float64).T.copy()


def surface_material(prim, vertex_color=False, channels=()):
    return mi.load_dict(read_usd_material(prim, vertex_color=vertex_color, channels=channels))


def mesh_shape(prim, matrix):
    from .scenes.meshes import mesh_shape as native_mesh
    return native_mesh(prim, matrix)


def solid_shape(prim, matrix):
    """Tessellate round USD solids, preserving their authored axis and caps."""
    kind = prim.GetTypeName()
    schema = {"Capsule": UsdGeom.Capsule, "Cylinder": UsdGeom.Cylinder, "Cone": UsdGeom.Cone}[kind](prim)
    radius, height = float(schema.GetRadiusAttr().Get()), float(schema.GetHeightAttr().Get())
    axis = str(schema.GetAxisAttr().Get())
    segments = 32
    if kind == "Capsule":
        rings = [(radius * math.cos(a), -height / 2 + radius * math.sin(a))
                 for a in np.linspace(-math.pi / 2, 0, 9)]
        rings += [(radius * math.cos(a), height / 2 + radius * math.sin(a))
                  for a in np.linspace(0, math.pi / 2, 9)]
    elif kind == "Cylinder":
        rings = [(0, -height / 2), (radius, -height / 2), (radius, height / 2), (0, height / 2)]
    else:
        rings = [(0, -height / 2), (radius, -height / 2), (0, height / 2)]
    points = np.asarray([[r * math.cos(a), r * math.sin(a), z]
                         for r, z in rings for a in np.linspace(0, 2 * math.pi, segments, endpoint=False)])
    if axis == "X":
        points = points[:, [2, 0, 1]]
    elif axis == "Y":
        points = points[:, [1, 2, 0]]
    elif axis != "Z":
        raise ValueError(f"Unsupported USD primitive axis: {axis}")
    faces = []
    for ring in range(len(rings) - 1):
        for j in range(segments):
            a, b = ring * segments + j, ring * segments + (j + 1) % segments
            c, d = a + segments, b + segments
            faces.extend(((a, b, d), (a, d, c)))
    props = mi.Properties()
    props["bsdf"] = surface_material(prim)
    shape = mi.Mesh(str(prim.GetPath()), vertex_count=len(points), face_count=len(faces), props=props)
    params = mi.traverse(shape)
    params["vertex_positions"] = (points @ matrix[:3, :3].T + matrix[:3, 3]).astype(np.float32).reshape(-1)
    params["faces"] = np.asarray(faces, np.uint32).reshape(-1)
    params.update()
    return shape


def add_stage_geometry(scene, stage, mode, cache):
    """Compile visible geometry, materials and lights into a Mitsuba scene description."""
    sources, unsupported = [], []
    for prim in Usd.PrimRange.Stage(stage, Usd.TraverseInstanceProxies()):
        if not prim.IsActive():
            continue
        imageable = UsdGeom.Imageable(prim)
        if imageable and imageable.ComputeVisibility() == UsdGeom.Tokens.invisible:
            continue
        if imageable and imageable.ComputePurpose() in (UsdGeom.Tokens.guide, UsdGeom.Tokens.proxy):
            continue
        key = str(prim.GetPath()).replace("/", "_")
        matrix = world_matrix(prim, cache) if prim.IsA(UsdGeom.Xformable) else np.eye(4)
        if prim.IsA(UsdGeom.Mesh):
            gaussian = prim.GetAttribute("a100sim:gaussianAsset")
            if mode == "gaussian" and gaussian and gaussian.Get():
                value = gaussian.Get()
                path = value.resolvedPath or value.path
                data = load_gaussians(path)
                scene[key] = shape_dict(data, matrix=matrix)
                source = {
                    "prim": str(prim.GetPath()),
                    "kind": "gaussian",
                    "asset": path,
                    "gaussians": len(data["means"]),
                    "world_matrix": matrix.tolist(),
                }
            else:
                scene[key] = mesh_shape(prim, matrix)
                source = {
                    "prim": str(prim.GetPath()),
                    "kind": "mesh",
                    "triangles": len(UsdGeom.Mesh(prim).GetFaceVertexCountsAttr().Get()),
                    "world_matrix": matrix.tolist(),
                }
            sources.append(source)
        elif prim.IsA(UsdGeom.Sphere):
            radius = float(UsdGeom.Sphere(prim).GetRadiusAttr().Get())
            scene[key] = {
                "type": "sphere",
                "to_world": mi.ScalarTransform4f(matrix) @ mi.ScalarTransform4f.scale(radius),
                "bsdf": surface_material(prim),
            }
            sources.append({"prim": str(prim.GetPath()), "kind": "sphere", "world_matrix": matrix.tolist()})
        elif prim.IsA(UsdGeom.Cube):
            size_value = float(UsdGeom.Cube(prim).GetSizeAttr().Get())
            scene[key] = {
                "type": "cube",
                "to_world": mi.ScalarTransform4f(matrix) @ mi.ScalarTransform4f.scale(size_value / 2),
                "bsdf": surface_material(prim),
            }
            sources.append({"prim": str(prim.GetPath()), "kind": "cube", "world_matrix": matrix.tolist()})
        elif prim.GetTypeName() in ("Capsule", "Cylinder", "Cone"):
            scene[key] = solid_shape(prim, matrix)
            sources.append({"prim": str(prim.GetPath()), "kind": prim.GetTypeName().lower(), "world_matrix": matrix.tolist()})
        elif prim.IsA(UsdLux.DomeLight) or prim.IsA(UsdLux.RectLight):
            light = UsdLux.LightAPI(prim)
            rgb = np.asarray(light.GetColorAttr().Get(), np.float64)
            rgb *= float(light.GetIntensityAttr().Get()) * 2 ** float(light.GetExposureAttr().Get())
            if prim.IsA(UsdLux.DomeLight):
                dome = UsdLux.DomeLight(prim)
                if dome.GetTextureFileAttr().Get():
                    raise ValueError("Textured dome lights require an envmap adapter")
                scene[key] = {"type": "constant", "radiance": {"type": "rgb", "value": rgb.tolist()}}
            else:
                rect = UsdLux.RectLight(prim)
                transform = matrix @ np.diag(
                    [float(rect.GetWidthAttr().Get()) / 2, -float(rect.GetHeightAttr().Get()) / 2, -1, 1]
                )
                scene[key] = {
                    "type": "rectangle",
                    "to_world": mi.ScalarTransform4f(transform),
                    "emitter": {"type": "area", "radiance": {"type": "rgb", "value": rgb.tolist()}},
                }
            sources.append({"prim": str(prim.GetPath()), "kind": prim.GetTypeName(), "radiance": rgb.tolist()})
        elif prim.IsA(UsdGeom.Gprim) or prim.HasAPI(UsdLux.LightAPI):
            unsupported.append(f"{prim.GetPath()} ({prim.GetTypeName()})")
    if unsupported:
        raise ValueError("Unsupported USD rendering schemas: " + ", ".join(unsupported))
    return sources


def render_stage(stage, camera_path, mode, output, size=512, spp=64, seed=42, gaussian_gain=1.0,
                 annotations=False, cameras=None, individual_views=False):
    if mi.variant() not in {"cuda_ad_rgb", "llvm_ad_rgb", "scalar_rgb"}:
        raise RuntimeError("Render variant must be cuda_ad_rgb, llvm_ad_rgb, or scalar_rgb")
    expected_backend = {
        "cuda_ad_rgb": dr.JitBackend.CUDA,
        "llvm_ad_rgb": dr.JitBackend.LLVM,
        "scalar_rgb": dr.JitBackend.Invalid,
    }[mi.variant()]
    if expected_backend != dr.JitBackend.Invalid and not dr.has_backend(expected_backend):
        raise RuntimeError(f"The selected Mitsuba backend is unavailable: {mi.variant()}")
    render_backend = mi.variant()
    cpu_fallback = render_backend != "cuda_ad_rgb"
    renderer = {
        "cuda_ad_rgb": "Mitsuba CUDA path tracer",
        "llvm_ad_rgb": "Mitsuba CPU LLVM path tracer",
        "scalar_rgb": "Mitsuba CPU scalar path tracer",
    }[render_backend]
    if mode not in ("mesh", "gaussian"):
        raise ValueError("mode must be mesh or gaussian")
    if annotations and mode != "mesh":
        raise ValueError("Primary-surface annotations currently require mesh mode")
    started = time.perf_counter()
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    camera = UsdGeom.Camera.Get(stage, camera_path)
    if not camera or camera.GetProjectionAttr().Get() != "perspective":
        raise ValueError("a perspective USD camera is required")
    camera_world = world_matrix(camera.GetPrim(), cache)
    origin = camera_world[:3, 3]
    fov = math.degrees(
        2 * math.atan(float(camera.GetHorizontalApertureAttr().Get()) / (2 * float(camera.GetFocalLengthAttr().Get())))
    )
    scene = {
        "type": "scene",
        "integrator": {"type": "a100_hybrid_path" if mode == "gaussian" else "path", "max_depth": 6},
        "sensor": {
            "type": "perspective",
            "fov": fov,
            "fov_axis": "x",
            "to_world": mi.ScalarTransform4f.look_at(
                origin=origin, target=origin - camera_world[:3, 2], up=camera_world[:3, 1]
            ),
            "film": {
                "type": "hdrfilm",
                "width": size,
                "height": size,
                "pixel_format": "rgb",
                "rfilter": {"type": "box"},
            },
        },
    }
    # Bound temporary wavefront storage on CPU while retaining the requested
    # total sample count. Mitsuba accumulates all passes into the same film.
    if render_backend == "llvm_ad_rgb":
        film_pixels = size * size * (len(cameras) if cameras is not None and not individual_views else 1)
        max_pass_spp = max(1, (4 * 1024 * 1024) // film_pixels)
        if spp > max_pass_spp:
            scene["integrator"]["samples_per_pass"] = next(
                count for count in range(max_pass_spp, 0, -1) if spp % count == 0
            )
    if mode == "gaussian":
        scene["integrator"]["max_events"] = 512
        scene["integrator"]["gaussian_gain"] = float(gaussian_gain)
    sources = add_stage_geometry(scene, stage, mode, cache)
    camera_specs = None
    if cameras is not None:
        camera_specs = []
        for path in cameras:
            selected = UsdGeom.Camera.Get(stage, path)
            if not selected or selected.GetProjectionAttr().Get() != "perspective":
                raise ValueError(f"A perspective USD camera is required: {path}")
            matrix = world_matrix(selected.GetPrim(), cache)
            angle = math.degrees(2 * math.atan(float(selected.GetHorizontalApertureAttr().Get()) /
                                               (2 * float(selected.GetFocalLengthAttr().Get()))))
            sensor = {**scene["sensor"], "fov": angle, "to_world": mi.ScalarTransform4f.look_at(
                origin=matrix[:3, 3], target=matrix[:3, 3] - matrix[:3, 2], up=matrix[:3, 1])}
            camera_specs.append((path, matrix, angle, sensor))
        if not individual_views or len(camera_specs) > 1:
            batch = {"type": "batch", "film": {**scene["sensor"]["film"], "width": size * len(cameras)}}
            for index, (_, _, _, sensor) in enumerate(camera_specs):
                batch[f"camera_{index:04d}"] = {key: value for key, value in sensor.items() if key != "film"}
            scene["sensor"] = batch
    scene = mi.load_dict(scene)
    surface_ids = None
    if annotations:
        from .annotations import prepare_surface_ids
        surface_ids = prepare_surface_ids(scene, stage, sources)
    setup_seconds = time.perf_counter() - started
    def write_view(camera_path, camera_world, fov, sensor, output, setup_seconds):
        tick = time.perf_counter()
        image = mi.render(scene, sensor=sensor, spp=spp, seed=seed)
        dr.eval(image)
        dr.sync_thread()
        render_seconds = time.perf_counter() - tick
        output.parent.mkdir(parents=True, exist_ok=True)
        mi.util.write_bitmap(str(output), image, write_async=False)
        annotation_result = None
        if annotations:
            from .annotations import capture_primary_surfaces

            annotation_result = capture_primary_surfaces(scene, stage, sources, camera_world, output.with_suffix(""),
                                                         sensor=sensor, surface_ids=surface_ids)
        return {
            "mode": mode,
            "renderer": renderer,
            "render_backend": render_backend,
            "native_rtx": False,
            "cpu_fallback": cpu_fallback,
            "resolution": [size, size],
            "spp": spp,
            "gaussian_gain": float(gaussian_gain) if mode == "gaussian" else None,
            "setup_seconds": setup_seconds,
            "render_seconds": render_seconds,
            "image": output.name,
            "camera": camera_path,
            "camera_world": camera_world.tolist(),
            "fov_degrees": fov,
            "usd_sources": sources,
            **({"annotations": annotation_result} if annotation_result else {}),
        }

    output = Path(output)
    if individual_views and len(camera_specs) == 1:
        frames = []
        for index, (path, matrix, angle, config) in enumerate(camera_specs):
            sensor = scene.sensors()[0] if index == 0 else mi.load_dict(config)
            frame = write_view(path, matrix, angle, sensor, output / f"view_{index}.png",
                               setup_seconds if index == 0 else 0.0)
            frames.append(frame)
        return {"frames": frames}
    if camera_specs is None:
        return write_view(camera_path, camera_world, fov, scene.sensors()[0], output, setup_seconds)
    tick = time.perf_counter()
    image = mi.render(scene, spp=spp, seed=seed)
    dr.eval(image)
    dr.sync_thread()
    render_seconds = time.perf_counter() - tick
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if camera_specs is not None:
        # One acceleration structure and one CUDA wavefront launch for all views.
        # Read back once, then encode images independently of the simulation owner.
        pixels = np.asarray(image).copy()
        frames = []
        annotation_seconds = 0.0
        for index, (path, matrix, angle, sensor_config) in enumerate(camera_specs):
            destination = output / (f"view_{index}.png" if individual_views else f"camera-{index:03d}.png")
            mi.util.write_bitmap(str(destination), pixels[:, index * size:(index + 1) * size], write_async=False)
            annotation_result = None
            if annotations:
                from .annotations import capture_primary_surfaces
                tick = time.perf_counter()
                annotation_result = capture_primary_surfaces(
                    scene, stage, sources, matrix, destination.with_suffix(""),
                    sensor=mi.load_dict(sensor_config), surface_ids=surface_ids)
                annotation_seconds += time.perf_counter() - tick
            frames.append({"image": destination.name if individual_views else str(destination),
                           "camera": path, "camera_world": matrix.tolist(),
                           "fov_degrees": angle, "resolution": [size, size], "spp": spp,
                           "mode": mode, "native_rtx": False, "cpu_fallback": cpu_fallback,
                           "render_backend": render_backend,
                           **({"annotations": annotation_result} if annotation_result else {})})
        return {"frames": frames, "setup_seconds": setup_seconds, "render_seconds": render_seconds,
                "annotation_seconds": annotation_seconds, "camera_parallelism": len(cameras),
                "geometry_builds": 1, "render_launches": 1, "usd_sources": sources,
                "renderer": renderer, "render_backend": render_backend,
                "native_rtx": False, "cpu_fallback": cpu_fallback}


def render_cameras(stage, cameras, directory, size=512, spp=32, annotations=True):
    """Render simultaneous camera views while sharing scene/BVH and CUDA work."""
    return render_stage(stage, cameras[0], "mesh", directory, size=size, spp=spp,
                        annotations=annotations, cameras=cameras)
