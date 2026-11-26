"""gRPC 专属：逐帧物理状态采集 + 逐帧 RTX 渲染。

继承上游 RtxQueue（物理引擎 + RTX 渲染后端保持不变），仅覆盖两处：

1. prepare_capture() — 加 render 布尔字段解析
2. process() — 把上游 "一次 step 到底" 改成 "逐帧 step + 逐帧采集 + 逐帧渲染"

上游代码（rtx4090/service.py / a100sim/physics_service.py）零改动。
"""

from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdPhysics, Vt

from rtx4090.service import RtxQueue

from a100sim.service_queue import finite, integer, persist


class RtxQueueWithFrameCapture(RtxQueue):
    """gRPC 引擎的任务队列：逐帧采集 + 逐帧渲染。"""

    # ------------------------------------------------------------------
    # run_one：在不改动上游的前提下，接入取消 / 进度 / 产物清单收尾
    # ------------------------------------------------------------------

    def _manager(self):
        return getattr(self, "manager", None)

    def _progress(self, **kw) -> None:
        """向 JobManager 上报进度（manager 未就绪时为空操作）。"""
        mgr = self._manager()
        if mgr is not None:
            mgr.update_progress(getattr(self, "_job_id", "") or "", **kw)

    def _is_cancelled(self) -> bool:
        mgr = self._manager()
        if mgr is None:
            return False
        return mgr.is_cancelled(getattr(self, "_job_id", "") or "")

    def run_one(self, item) -> None:
        job_id, payload = item
        directory = self.output / job_id
        mgr = self._manager()
        if mgr is not None:
            mgr._begin(job_id, payload)
        started = time.time()
        with self.lock:
            record = self.status(job_id)
            record.update(state="running", started_at=started,
                          queue_seconds=started - record.get("queued_at", started))
            if mgr is not None:
                mgr._merge_progress(record, job_id)
            persist(directory / "job.json", record)
            self.active = job_id
        try:
            result = self.process(payload, directory)
            cancelled = bool(result.pop("_cancelled", False))
            record.update(result)
            record.update(id=job_id, state="interrupted" if cancelled else "succeeded")
            if not cancelled:
                self.completed += 1
        except Exception as exc:  # noqa: BLE001
            record.update(id=job_id, state="failed", error=str(exc)[:1000])
            self.failed += 1
            print(f"JOB_FAILED {job_id}: {type(exc).__name__}: {exc}", flush=True)
        finally:
            record.update(finished_at=time.time(),
                          execution_seconds=time.time() - started)
            if mgr is not None:
                mgr._merge_progress(record, job_id)
                # phase 兜底：内存里没有的话用 state
                record["phase"] = record.get("phase") or record.get("state", "")
                # 先算 manifest 写进 record，再 persist —— 避免客户端在两次写之间查询拿到空 manifest
                if record.get("state") in ("succeeded", "interrupted") \
                        and "artifact_manifest" not in record:
                    record["artifact_manifest"] = mgr._compute_manifest(job_id)
            with self.lock:
                persist(directory / "job.json", record)
                self.active = None
            self.jobs.task_done()
            if mgr is not None:
                mgr._finalize(job_id)  # 幂等：manifest 已存在则跳过
                mgr._end(job_id)

    # ------------------------------------------------------------------
    # 0. 覆盖 prepare：上游不认识 capture 字段，这里补上
    # ------------------------------------------------------------------

    def prepare(self, value: dict) -> dict:
        # 先把 capture 摘出来，让上游 prepare 能过 unknown 字段检查
        capture_value = value.get("capture")
        upstream_input = {k: v for k, v in value.items() if k != "capture"}

        # mass 语义预处理（proto 约定：省略即静态体）：
        # 上游把省略的 mass 默认成 1.0 且拒绝 0，这里在调用上游前抹平差异。
        raw_bodies = upstream_input.get("bodies")
        if isinstance(raw_bodies, list):
            upstream_input = dict(upstream_input)
            upstream_input["bodies"] = [
                {k: v for k, v in body.items() if not (k == "mass" and float(v) == 0.0)}
                if isinstance(body, dict) else body
                for body in raw_bodies
            ]
            raw_bodies = upstream_input["bodies"]

        request = super().prepare(upstream_input)

        # 标记静态体：原始请求未提供 mass，或显式 mass=0（已在上一步移除）
        for raw_body, item in zip(raw_bodies or [], request["bodies"]):
            item["static"] = not ("mass" in raw_body)

        # 再用我们自己的 prepare_capture 解析 capture（含 render 字段）
        request["capture"] = self.prepare_capture(capture_value, request["steps"])
        return request

    # ------------------------------------------------------------------
    # 1. 覆盖 prepare_capture：capture 存在即强制逐帧渲染
    # ------------------------------------------------------------------

    @staticmethod
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

    # ------------------------------------------------------------------
    # 2. 覆盖 process：完整骨架复用上游 PhysicsQueue.process()，
    #    但把 "一次 step 所有帧" 改成 "逐帧 step + 采集 + 渲染"
    # ------------------------------------------------------------------

    def process(self, request: dict[str, Any], directory: Path) -> dict[str, Any]:
        from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdPhysics, Vt
        import omni.usd

        self.body_ids = [body["id"] for body in request["bodies"]]

        started = time.perf_counter()
        context = omni.usd.get_context()
        context.new_stage()
        stage = context.get_stage()
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
        UsdGeom.SetStageMetersPerUnit(stage, 1)
        stage.SetDefaultPrim(UsdGeom.Xform.Define(stage, "/World").GetPrim())
        scene = UsdPhysics.Scene.Define(stage, "/World/PhysicsScene")
        scene.CreateGravityDirectionAttr(Gf.Vec3f(0, 0, -1))
        scene.CreateGravityMagnitudeAttr(request["gravity"])
        if request["ground"]:
            ground = UsdGeom.Cube.Define(stage, "/World/Ground")
            ground.CreateSizeAttr(1)
            ground.AddTranslateOp().Set(Gf.Vec3d(0, 0, -.1))
            ground.AddScaleOp().Set(Gf.Vec3f(200, 200, .2))
            UsdPhysics.CollisionAPI.Apply(ground.GetPrim())

        paths: list[str] = []
        for index, body in enumerate(request["bodies"]):
            path = f"/World/Bodies/B{index:04d}"
            paths.append(path)
            root = UsdGeom.Xform.Define(stage, path)
            root.AddTranslateOp().Set(Gf.Vec3d(*body["position"]))
            qx, qy, qz, qw = body["orientation_xyzw"]
            root.AddOrientOp().Set(Gf.Quatf(qw, Gf.Vec3f(qx, qy, qz)))
            rigid = UsdPhysics.RigidBodyAPI.Apply(root.GetPrim())
            if body.get("static"):
                # 静态体：运动学刚体，不受重力、不响应接触冲量，位姿保持不变
                rigid.CreateKinematicEnabledAttr(True)
            else:
                UsdPhysics.MassAPI.Apply(root.GetPrim()).CreateMassAttr(body["mass"])
            visual_path = path + "/Visual"
            if body["shape"] == "mesh":
                asset = Path(body["asset"])
                with np.load(asset / "mesh.npz", allow_pickle=False) as data:
                    vertices, faces = data["vertices"], data["faces"]
                    normals, colors = data["normals"], data["colors"]
                    if "colors_linear" not in data:
                        colors = np.where(colors <= .04045, colors / 12.92,
                                          ((colors + .055) / 1.055) ** 2.4)
                visual = UsdGeom.Mesh.Define(stage, visual_path)
                visual.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(vertices.astype(np.float32)))
                visual.CreateFaceVertexCountsAttr(Vt.IntArray.FromNumpy(np.full(len(faces), 3, np.int32)))
                visual.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(faces.reshape(-1).astype(np.int32)))
                visual.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(normals.astype(np.float32)))
                visual.SetNormalsInterpolation("vertex")
                visual.CreateSubdivisionSchemeAttr("none")
                visual.CreateDisplayColorPrimvar("vertex").Set(Vt.Vec3fArray.FromNumpy(colors.astype(np.float32)))
                UsdPhysics.MeshCollisionAPI.Apply(visual.GetPrim()).CreateApproximationAttr("convexHull")
                if (asset / "object.ply").is_file():
                    visual.GetPrim().CreateAttribute("a100sim:gaussianAsset", Sdf.ValueTypeNames.Asset).Set(
                        Sdf.AssetPath(str(asset / "object.ply")))
            elif body["shape"] == "sphere":
                visual = UsdGeom.Sphere.Define(stage, visual_path)
                visual.CreateRadiusAttr(body["radius"])
                visual.CreateDisplayColorAttr([Gf.Vec3f(*body["color"])])
            else:
                visual = UsdGeom.Cube.Define(stage, visual_path)
                visual.CreateSizeAttr(1)
                visual.AddScaleOp().Set(Gf.Vec3f(*body["size"]))
                visual.CreateDisplayColorAttr([Gf.Vec3f(*body["color"])])
            UsdPhysics.CollisionAPI.Apply(visual.GetPrim())

        physics = None
        try:
            physics = self.create_physics(stage)
            rigids = physics.view.create_rigid_body_view("/World/Bodies/B*")
            if rigids.count != len(paths):
                raise RuntimeError(f"Expected {len(paths)} GPU rigid bodies; found {rigids.count}")

            setup_seconds = time.perf_counter() - started
            tick = time.perf_counter()

            # ------- 相机 + 灯光：物理 step 之前创建 -------
            capture = request.get("render")
            if capture:
                self._setup_camera_lights(stage, capture)

            # ------- 逐帧采集 + 逐帧渲染（capture_spec 存在即强制）-------
            capture_spec = request.get("capture")
            frame_records: list[dict] = []
            cancelled = False

            if capture_spec:
                self._progress(phase="simulating",
                               frames_total=len(capture_spec["frames"]), frames_done=0)
                frames_dir = directory / "frames"
                frames_dir.mkdir(parents=True, exist_ok=True)
                frame_capture_ctx = self._init_frame_render(stage, request)
                try:
                    stepped = 0
                    for frame in capture_spec["frames"]:
                        if self._is_cancelled():
                            cancelled = True
                            break
                        physics.step(frame - stepped, request["dt"])
                        stepped = frame
                        frame_dir = frames_dir / f"frame_{frame:06d}"
                        frame_dir.mkdir(parents=True, exist_ok=True)
                        state_npz = frame_dir / "state.npz"
                        np.savez(state_npz,
                                 frame=np.int32(frame),
                                 time=np.float64(frame * request["dt"]),
                                 poses_xyzw=rigids.get_transforms().numpy().copy(),
                                 velocities=rigids.get_velocities().numpy().copy())
                        record: dict = {"frame": frame, "state_npz": str(state_npz)}
                        record.update(self._render_one_frame(frame, frame_dir, rigids, paths, frame_capture_ctx))
                        frame_records.append(record)
                        self._progress(frames_done=len(frame_records))
                finally:
                    self._shutdown_frame_render(frame_capture_ctx)
            else:
                if self._is_cancelled():
                    cancelled = True
                if not cancelled:
                    physics.step(request["steps"], request["dt"])
                self._progress(phase="simulating", frames_total=0, frames_done=0)

            physics_seconds = time.perf_counter() - tick
            poses = rigids.get_transforms().numpy().copy()
            velocities = rigids.get_velocities().numpy().copy()
            actual_paths = paths
            bodies = []
            by_path = {path: body for path, body in zip(paths, request["bodies"])}
            for path, pose, velocity in zip(actual_paths, poses, velocities):
                position = Gf.Vec3d(*[float(v) for v in pose[:3]])
                qx, qy, qz, qw = [float(v) for v in pose[3:7]]
                matrix = Gf.Matrix4d().SetRotate(Gf.Quatd(qw, Gf.Vec3d(qx, qy, qz)))
                matrix.SetTranslateOnly(position)
                xform = UsdGeom.Xformable(stage.GetPrimAtPath(path))
                xform.ClearXformOpOrder()
                xform.AddTransformOp().Set(matrix)
                body_spec = by_path[path]
                # 静态体（运动学）速度语义固定为 0
                out_velocity = [0.0] * 6 if body_spec.get("static") else velocity.tolist()
                bodies.append({"id": body_spec["id"], "prim_path": path,
                               "pose_xyzw": pose.tolist(), "velocity": out_velocity})
            np.savez(directory / "state.npz", poses_xyzw=poses, velocities=velocities)
            result = {"isaac_sim": self.runtime_version, "physics_device": str(physics.view.device),
                      "physical_gpu": self.health()["physical_gpu"], "native_rtx": self.native_rtx,
                      "cpu_fallback": False, "bodies": bodies, "rigid_bodies": len(bodies),
                      "steps": request["steps"], "dt": request["dt"],
                      "simulation_seconds": request["steps"] * request["dt"],
                      "setup_seconds": setup_seconds, "physics_seconds": physics_seconds,
                      "state_npz": str(directory / "state.npz"),
                      "collision": "PhysX primitives; convexHull for dynamic mesh",
                      "scene": str(directory / "scene.usdc")}
            if frame_records:
                result["frames"] = frame_records
                result["capture"] = {"every": capture_spec.get("every"),
                                     "requested": capture_spec["frames"],
                                     "render": True,
                                     "count": len(frame_records)}
            if self.close_physics_before_capture:
                physics.close()
                physics = None

            # ------- 尾帧渲染（取消时跳过，已写出的帧产物仍可下载）-------
            if capture and not cancelled:
                self._progress(phase="rendering")
                image = directory / "render.png"
                result["render"] = self.capture_scene(stage, capture, image)
                result["image"] = str(image)

            self._progress(phase="writing_artifacts")
            stage.GetRootLayer().Export(str(directory / "scene.usdc"))
            persist(directory / "simulation.json", result)
            result["simulation_result"] = str(directory / "simulation.json")
            if cancelled:
                result["_cancelled"] = True
            return result
        finally:
            if physics is not None:
                physics.close()

    # ------------------------------------------------------------------
    # Camera + Lights（物理 step 之前创建，逐帧和尾帧共享）
    # ------------------------------------------------------------------

    def _setup_camera_lights(self, stage: Any, capture: dict) -> None:
        """在 USD Stage 上创建 Camera + DomeLight + KeyLight + FillLights。

        与上游 PhysicsQueue.process() 末尾的创建逻辑等价，但在物理 step 之前执行。
        """
        from pxr import UsdGeom, UsdLux

        camera = UsdGeom.Camera.Define(stage, "/World/Camera")
        camera.CreateFocalLengthAttr(35)
        camera.CreateHorizontalApertureAttr(2 * 35 * math.tan(math.radians(34) / 2))
        az, el = math.radians(capture["azimuth"]), math.radians(capture["elevation"])
        target = np.asarray(capture["target"])
        eye = target + capture["distance"] * np.array([
            math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)])

        def orient(prim, eye_v, target_v):
            mat = Gf.Matrix4d().SetLookAt(Gf.Vec3d(*eye_v), Gf.Vec3d(*target_v), Gf.Vec3d(0, 0, 1))
            UsdGeom.Xformable(prim).AddTransformOp().Set(mat.GetInverse())

        orient(camera.GetPrim(), eye, target)
        lighting = capture.get("lighting") or {}
        dome_spec, key_spec = lighting.get("dome", {}), lighting.get("key", {})
        dome = UsdLux.DomeLight.Define(stage, "/World/Dome")
        dome.CreateColorAttr(Gf.Vec3f(*dome_spec.get("color", [.45, .48, .52])))
        dome.CreateIntensityAttr(float(dome_spec.get("intensity", 1)))
        if "exposure" in dome_spec:
            dome.CreateExposureAttr(float(dome_spec["exposure"]))
        lamp = UsdLux.RectLight.Define(stage, "/World/KeyLight")
        lamp.CreateWidthAttr(float(key_spec.get("width", 3.6)))
        lamp.CreateHeightAttr(float(key_spec.get("height", 3.6)))
        lamp.CreateIntensityAttr(float(key_spec.get("intensity", 5)))
        if "color" in key_spec:
            lamp.CreateColorAttr(Gf.Vec3f(*key_spec["color"]))
        if "exposure" in key_spec:
            lamp.CreateExposureAttr(float(key_spec["exposure"]))
        orient(lamp.GetPrim(), key_spec.get("origin", [-2, -3, 4]),
               key_spec.get("target", [0, 0, .5]))
        for fill_index, fill in enumerate(lighting.get("fills", [])):
            wash = UsdLux.RectLight.Define(stage, f"/World/Fill{fill_index:02d}")
            wash.CreateWidthAttr(float(fill.get("width", 2.0)))
            wash.CreateHeightAttr(float(fill.get("height", 2.0)))
            wash.CreateIntensityAttr(float(fill.get("intensity", 1)))
            if "color" in fill:
                wash.CreateColorAttr(Gf.Vec3f(*fill["color"]))
            if "exposure" in fill:
                wash.CreateExposureAttr(float(fill["exposure"]))
            orient(wash.GetPrim(), fill.get("origin", [0, 0, 3]),
                   fill.get("target", [0, 0, 0]))

    # ------------------------------------------------------------------
    # RTX 逐帧渲染辅助方法（每次任务生命周期内复用 product / annotators）
    # ------------------------------------------------------------------

    def _init_frame_render(self, stage: Any, request: dict) -> dict:
        """创建一次 render_product + annotators + warmup，复用全程。"""
        import carb
        import omni.kit.app
        import omni.replicator.core as rep
        from isaacsim.core.experimental.utils.semantics import add_labels
        from pxr import UsdLux

        render = request.get("render")
        if not render:
            raise ValueError("capture.render=true requires render spec (camera/lighting)")

        settings = carb.settings.get_settings()
        if not omni.kit.app.get_app().get_extension_manager().is_extension_enabled("omni.hydra.rtx"):
            raise RuntimeError("omni.hydra.rtx not enabled")
        settings.set("/rtx/rendermode", "RayTracedLighting")
        settings.set("/rtx/post/tonemap/op", 4)
        settings.set("/rtx/post/tonemap/filmIso", 200.0)
        settings.set("/rtx/post/tonemap/whitepoint", 6500.0)
        settings.set("/rtx/post/tonemap/enabled", True)
        rep.orchestrator.set_capture_on_play(False)

        lighting = render.get("lighting", {})
        # 转 radiance → photometric intensity（与 capture_scene 一致）
        for path, spec in [("/World/Dome", lighting.get("dome", {}))]:
            light = UsdLux.DomeLight(stage.GetPrimAtPath(path))
            if light.GetPrim().IsValid():
                light.GetIntensityAttr().Set(float(spec.get("intensity", 1)) * 300.0)
        key = UsdLux.RectLight(stage.GetPrimAtPath("/World/KeyLight"))
        if key.GetPrim().IsValid():
            key.GetIntensityAttr().Set(float(lighting.get("key", {}).get("intensity", 5)) * 300.0)
        for idx, fill in enumerate(lighting.get("fills", [])):
            light = UsdLux.RectLight(stage.GetPrimAtPath(f"/World/Fill{idx:02d}"))
            if light.GetPrim().IsValid():
                light.GetIntensityAttr().Set(float(fill.get("intensity", 1)) * 300.0)

        # 语义标签
        for idx, body_id in enumerate(self.body_ids):
            add_labels(stage.GetPrimAtPath(f"/World/Bodies/B{idx:04d}"), labels=[body_id])
        if stage.GetPrimAtPath("/World/Ground"):
            add_labels(stage.GetPrimAtPath("/World/Ground"), labels=["ground"])

        size = render["size"]
        print(f"RTX_FRAME_INIT size={size} body_count={len(self.body_ids)}", flush=True)
        product = rep.create.render_product("/World/Camera", (size, size))
        annotators = {
            "rgb": rep.AnnotatorRegistry.get_annotator("rgb"),
            "depth": rep.AnnotatorRegistry.get_annotator("distance_to_image_plane"),
            "semantic": rep.AnnotatorRegistry.get_annotator("semantic_segmentation", init_params={"colorize": False}),
        }
        for annotator in annotators.values():
            annotator.attach([product])

        # Warmup
        print("RTX_FRAME_WARMUP", flush=True)
        for _ in range(24):
            self.app.update()

        rt_subframes = max(8, render.get("spp", 8))
        ctx = {
            "stage": stage,
            "product": product,
            "annotators": annotators,
            "rt_subframes": rt_subframes,
            "size": size,
        }
        print(f"RTX_FRAME_READY rt_subframes={rt_subframes}", flush=True)
        return ctx

    def _render_one_frame(self, frame_idx: int, frame_dir: Path, rigids: Any,
                          paths: list, ctx: dict) -> dict:
        """写回 PhysX pose → RTX 渲染 → 读 annotators → 写盘。"""
        import omni.replicator.core as rep
        from pxr import Gf, UsdGeom

        stage = ctx["stage"]

        # 1. PhysX tensor → USD Stage（RTX 读 USD，不读 PhysX tensor）
        poses = rigids.get_transforms().numpy().copy()
        for path, pose in zip(paths, poses):
            position = Gf.Vec3d(*[float(v) for v in pose[:3]])
            qx, qy, qz, qw = [float(v) for v in pose[3:7]]
            matrix = Gf.Matrix4d().SetRotate(Gf.Quatd(qw, Gf.Vec3d(qx, qy, qz)))
            matrix.SetTranslateOnly(position)
            xform = UsdGeom.Xformable(stage.GetPrimAtPath(path))
            xform.ClearXformOpOrder()
            xform.AddTransformOp().Set(matrix)

        # 2. RTX 渲染（pause_timeline 防 timeline 推进）
        task = asyncio.ensure_future(rep.orchestrator.step_async(
            rt_subframes=ctx["rt_subframes"], delta_time=0.0, pause_timeline=True))
        deadline = time.monotonic() + 120
        while not task.done():
            self.app.update()
            if time.monotonic() > deadline:
                task.cancel()
                raise TimeoutError(f"RTX frame {frame_idx} timed out")
        task.result()

        # 3. 读 annotators + 写盘
        rgb = np.asarray(ctx["annotators"]["rgb"].get_data())[:, :, :3].copy()
        depth = np.asarray(ctx["annotators"]["depth"].get_data(), dtype=np.float32).squeeze()
        segmentation = ctx["annotators"]["semantic"].get_data()
        semantic = np.asarray(segmentation["data"], dtype=np.uint32).squeeze()

        size = ctx["size"]
        if rgb.shape[:2] != (size, size) or depth.shape != (size, size) or semantic.shape != (size, size):
            raise RuntimeError(f"Frame {frame_idx} bad shapes: rgb={rgb.shape} depth={depth.shape} sem={semantic.shape}")

        image_path = frame_dir / "render.png"
        Image.fromarray(rgb).save(image_path)
        np.save(frame_dir / "depth_m.npy", depth)
        np.save(frame_dir / "semantic_ids.npy", semantic)
        persist(frame_dir / "semantic_labels.json", segmentation.get("info", {}))

        print(f"RTX_FRAME_DONE frame={frame_idx} rgb_mean={rgb.mean():.1f}", flush=True)
        return {
            "image": str(image_path),
            "depth_m": str(frame_dir / "depth_m.npy"),
            "semantic_ids": str(frame_dir / "semantic_ids.npy"),
            "semantic_labels": str(frame_dir / "semantic_labels.json"),
        }

    def _shutdown_frame_render(self, ctx: dict) -> None:
        try:
            for annotator in ctx.get("annotators", {}).values():
                annotator.detach([ctx["product"]])
            ctx["product"].destroy()
        except Exception as exc:
            print(f"RTX_FRAME_SHUTDOWN_WARN {exc}", flush=True)
