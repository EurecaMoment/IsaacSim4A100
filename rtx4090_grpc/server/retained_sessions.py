"""Render original retained USD materials and authored cameras through Isaac Sim RTX."""

import asyncio
from contextlib import contextmanager
import os
import time
from pathlib import Path

import numpy as np
from PIL import Image
from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux

from a100sim.scenes.physics_state import validate_retained_poses
from a100sim.scenes.sessions import Sessions, prim_path
from a100sim.service_queue import integer, persist


@contextmanager
def rtx_light_units(stage):
    """Convert SceneProgram radiance lights using the same factor as RTX snapshots."""
    layer = Sdf.Layer.CreateAnonymous("rtx-light-units.usda")
    session = stage.GetSessionLayer()
    session.subLayerPaths.insert(0, layer.identifier)
    try:
        with Usd.EditContext(stage, layer):
            for prim in stage.Traverse():
                unit = prim.GetAttribute("benchforge:intensityUnit")
                if unit and unit.Get() == "linear_radiance" and prim.HasAPI(UsdLux.LightAPI):
                    intensity = UsdLux.LightAPI(prim).GetIntensityAttr()
                    intensity.Set(float(intensity.Get()) * 300.0)
        yield
    finally:
        session.subLayerPaths.remove(layer.identifier)


def calibration(stage, camera_path):
    camera = UsdGeom.Camera(stage.GetPrimAtPath(prim_path(camera_path)))
    if not camera or camera.GetProjectionAttr().Get() != UsdGeom.Tokens.perspective:
        raise ValueError("a perspective USD camera is required")
    model = camera.GetCamera(Usd.TimeCode.Default())
    return {
        "camera": camera_path,
        "camera_world": np.asarray(camera.ComputeLocalToWorldTransform(Usd.TimeCode.Default())).T.tolist(),
        "fov_degrees": model.GetFieldOfView(Gf.Camera.FOVHorizontal),
    }


def capture_view(annotators, directory, size, info):
    image = np.asarray(annotators["rgb"].get_data())
    if image.shape[:2] != (size, size) or image.ndim != 3 or image.shape[2] < 3:
        raise RuntimeError("RTX returned an incomplete RGB frame")
    path = directory / "render.png"
    Image.fromarray(image[:, :, :3]).save(path)
    result = {**info, "image": str(path), "resolution": [size, size],
              "native_rtx": True, "cpu_fallback": False, "replicator": True,
              "render_backend": "omni.hydra.rtx", "renderer": "RayTracedLighting"}
    if "depth" not in annotators:
        return result
    depth = np.asarray(annotators["depth"].get_data(), dtype=np.float32).reshape(size, size)
    ranges = np.asarray(annotators["range"].get_data(), dtype=np.float32).reshape(size, size)
    normals = np.asarray(annotators["normals"].get_data(), dtype=np.float32)[..., :3]
    semantic = annotators["semantic"].get_data()
    instance = annotators["instance"].get_data()
    if normals.shape != (size, size, 3):
        raise RuntimeError("RTX returned an incomplete normal buffer")
    valid = np.isfinite(depth) & (depth > 0) & np.isfinite(ranges)
    semantic_ids = np.asarray(semantic["data"], dtype=np.uint32).reshape(size, size)
    instance_ids = np.asarray(instance["data"], dtype=np.uint32).reshape(size, size)
    classes = {str(key): fields["class"] for key, fields in semantic["info"]["idToLabels"].items()}
    paths = instance["info"]["idToLabels"]
    instances = {}
    # The instance annotator maps each leaf USD prim to its pixel id.
    for identifier, index in zip(*np.unique(instance_ids, return_index=True)):
        prim_path = paths.get(str(identifier), paths.get(int(identifier)))
        if isinstance(prim_path, str) and prim_path.startswith("/"):
            semantic_id = int(semantic_ids.flat[index])
            instances[str(identifier)] = {"prim_path": prim_path, "semantic_id": semantic_id,
                                          "semantic_label": classes.get(str(semantic_id), "")}
    arrays_path = directory / "annotations.npz"
    np.savez_compressed(arrays_path, depth_m=np.where(valid, depth, 0),
                        range_m=np.where(valid, ranges, 0), valid=valid,
                        normal_world=np.where(valid[..., None], normals, 0),
                        semantic_id=semantic_ids, instance_id=instance_ids)
    labels_path = directory / "labels.json"
    labels = {"backend": "Isaac Sim RTX Replicator", "native_rtx": True, "replicator": True,
              "depth_convention": "camera-forward Z", "range_convention": "distance from camera origin",
              "classes": classes, "instances": instances}
    persist(labels_path, labels)
    return {**result, "annotations": {"arrays": str(arrays_path), "labels": str(labels_path)}}


class RetainedSessions(Sessions):
    def __init__(self, root, queue):
        super().__init__(root)
        self.queue = queue

    def create_physics(self, stage):
        return self.queue.create_physics(stage)

    def mdl_library(self):
        return Path(os.environ["ISAAC_SIM_DIR"]) / "kit/mdl/core/Base"

    def scene_preview(self, stage, metadata, values, directory):
        if set(values) - {"camera", "size", "spp"}:
            raise ValueError("Unsupported preview fields")
        values = {"cameras": [values["camera"]], "size": values.get("size", 256),
                  "spp": values.get("spp", 1), "annotations": False}
        frame = self._render(stage, metadata, values, directory)["frames"][0]
        stat = (self.root / metadata["session_id"] / "stage.usdc").stat()
        return {"image": frame["image"], "scene_version": f'{metadata["session_id"]}:{stat.st_mtime_ns}:{stat.st_size}'}

    def scene_capture(self, stage, metadata, values, directory):
        if metadata["observation"] is None:
            raise ValueError("Settle the scene before capture")
        validate_retained_poses(stage, metadata["observation"]["bodies"])
        return self._render(stage, metadata, values, directory)

    def _render(self, stage, metadata, values, directory):
        with rtx_light_units(stage):
            return self._render_views(stage, metadata, values, directory)

    def _render_views(self, stage, metadata, values, directory):
        import omni.replicator.core as rep
        from isaacsim.core.experimental.utils.semantics import add_labels

        if set(values) - {"cameras", "size", "spp", "annotations"}:
            raise ValueError("Unsupported capture fields")
        cameras = values.get("cameras")
        if not isinstance(cameras, list) or not 1 <= len(cameras) <= 32:
            raise ValueError("Capture requires 1 to 32 authored cameras")
        size = integer(values.get("size", 768), "size", 64, 2048)
        subframes = integer(values.get("spp", 32), "spp", 1, 1024)
        views = [calibration(stage, camera) for camera in cameras]
        self.queue._configure_rtx()
        annotations = values.get("annotations", True)
        if annotations:
            for prim in stage.Traverse():
                label = prim.GetAttribute("a100sim:semanticLabel")
                if label and label.Get():
                    add_labels(prim, labels=[str(label.Get())])
        products = []
        names = {"rgb": "rgb"}
        if annotations:
            names.update(depth="distance_to_image_plane", range="distance_to_camera", normals="normals",
                         instance="instance_id_segmentation", semantic="semantic_segmentation")
        try:
            for view in views:
                product = rep.create.render_product(view["camera"], (size, size))
                annotators = {}
                products.append({"product": product, "annotators": annotators})
                for key, name in names.items():
                    params = {"init_params": {"colorize": False}} if key in ("instance", "semantic") else {}
                    annotator = rep.AnnotatorRegistry.get_annotator(name, **params)
                    annotators[key] = annotator
                    annotator.attach([product])
            for _ in range(24):
                self.queue.app.update()
            step = asyncio.ensure_future(rep.orchestrator.step_async(
                rt_subframes=subframes, delta_time=0.0, pause_timeline=True))
            deadline = time.monotonic() + float(os.environ.get("RTX4090_FRAME_TIMEOUT_SECONDS", "600"))
            while not step.done():
                self.queue.app.update()
                if time.monotonic() > deadline:
                    step.cancel()
                    raise TimeoutError("Retained RTX frame timed out")
            step.result()
            frames = []
            for index, (view, product) in enumerate(zip(views, products)):
                target = directory / f"view_{index:02d}"
                target.mkdir()
                frames.append(capture_view(product["annotators"], target, size, {**view, "rt_subframes": subframes}))
            return {"frames": frames, "physics_tick": metadata["physics_tick"], "cpu_fallback": False,
                    "native_rtx": True, "replicator": True}
        finally:
            self.queue._shutdown_frame_render({"products": products})

