"""Own retained USD scenes; physics and rendering share the same settled stage."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import omni.usd
from a100sim.app import Physics
from a100sim.service_queue import finite, integer, persist
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade, UsdUtils

SESSION_ID = re.compile(r"[a-f0-9]{32}\Z")


def prim_path(value: str) -> Sdf.Path:
    """Require an absolute, nonroot USD prim path for a bounded scene action."""
    path = Sdf.Path(value)
    if not path.IsAbsolutePath() or not path.IsPrimPath() or str(path) == "/":
        raise ValueError("A nonroot absolute USD prim path is required")
    return path


class Sessions:
    """Serialize independent scenes, retaining one immutable result per queued job."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def execute(self, arguments: dict[str, Any], directory: Path) -> dict[str, Any]:
        """Load an independent retained stage, execute one action and save state."""
        values = dict(arguments)
        operation = values.pop("operation")
        identity = values.pop("session_id", None)
        if operation == "asset.import" and identity is None:
            identity = uuid.uuid4().hex
            home = self.root / identity
            home.mkdir()
            metadata = {"session_id": identity, "physics_tick": 0, "dt": 1 / 60, "state": "open", "observation": None}
            context = omni.usd.get_context()
            context.new_stage()
            stage = context.get_stage()
            UsdGeom.SetStageMetersPerUnit(stage, 1)
            UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
            stage.SetDefaultPrim(UsdGeom.Xform.Define(stage, "/World").GetPrim())
        else:
            if not isinstance(identity, str) or not SESSION_ID.fullmatch(identity):
                raise ValueError("A retained session_id is required")
            home = self.root / identity
            metadata = json.loads((home / "session.json").read_text())
            if metadata["state"] != "open" and operation != "scene.close":
                raise ValueError("The retained scene session is closed")
            context = omni.usd.get_context()
            if not context.open_stage(str(home / "stage.usdc")):
                raise ValueError("The retained stage could not be opened")
            stage = context.get_stage()
        handler = getattr(self, operation.replace(".", "_"))
        result = handler(stage, metadata, values, directory)
        # Flatten references so later operations never depend on a live source stage.
        if operation in {"asset.import", "scene.edit", "physics.step"}:
            stage.Flatten().Export(str(home / "stage.usdc"))
        persist(home / "session.json", metadata)
        return {"session_id": identity, **result}

    def asset_import(
        self, stage: Usd.Stage, metadata: dict[str, Any], values: dict[str, Any], directory: Path
    ) -> dict[str, Any]:
        """Import an uploaded USD with native units, collision and contact inputs."""
        allowed = {"source_path", "path", "fix_base", "mass_kg", "source_up_axis", "physics"}
        if set(values) - allowed:
            raise ValueError("Unsupported asset import fields")
        source = Path(values["source_path"])
        if source.suffix.lower() not in {".usd", ".usda", ".usdc", ".usdz"}:
            raise ValueError("Scene import requires an uploaded USD asset")
        path = prim_path(values.get("path", "/World/Scene"))
        if stage.GetPrimAtPath(path):
            raise ValueError("The destination prim already exists")
        wrapper = UsdGeom.Xform.Define(stage, path)
        native = "fix_base" in values
        if not native:
            wrapper.GetPrim().GetReferences().AddReference(str(source))
        else:
            # Native assets keep their authored topology and material networks.
            frame = UsdGeom.Xform.Define(stage, path.AppendChild("AssetFrame"))
            child = UsdGeom.Xform.Define(stage, frame.GetPath().AppendChild("Asset"))
            child.GetPrim().GetReferences().AddReference(str(source))
            source_stage = Usd.Stage.Open(str(source))
            scale = UsdGeom.GetStageMetersPerUnit(source_stage)
            correction = Gf.Matrix4d().SetScale(Gf.Vec3d(scale))
            up = values.get("source_up_axis", UsdGeom.GetStageUpAxis(source_stage))
            if str(up).upper() == "Y":
                correction = correction * Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(1, 0, 0), 90))
            units_op = frame.AddTransformOp(opSuffix="units")
            units_op.Set(correction)
            bounds = (
                UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default", "render"])
                .ComputeWorldBound(child.GetPrim())
                .ComputeAlignedRange()
            )
            center = bounds.GetMidpoint()
            # Put native geometry's measured centre at the parent origin.
            correction.SetTranslateOnly(-center)
            units_op.Set(correction)
            native_prims = list(Usd.PrimRange(wrapper.GetPrim()))
            colliders = [
                prim
                for prim in native_prims
                if prim.HasAPI(UsdPhysics.CollisionAPI)
                and UsdPhysics.CollisionAPI(prim).GetCollisionEnabledAttr().Get()
            ]
            if not colliders:
                colliders = [
                    prim
                    for prim in native_prims
                    if prim.IsA(UsdGeom.Mesh) and UsdGeom.Imageable(prim).ComputePurpose() in ("default", "render")
                ]
            for prim in native_prims:
                if prim.HasAPI(UsdPhysics.RigidBodyAPI):
                    UsdPhysics.RigidBodyAPI(prim).CreateRigidBodyEnabledAttr(False)
            for prim in colliders:
                UsdPhysics.CollisionAPI.Apply(prim).CreateCollisionEnabledAttr(True)
                if prim.IsA(UsdGeom.Mesh):
                    UsdPhysics.MeshCollisionAPI.Apply(prim).CreateApproximationAttr(
                        "none" if values.get("fix_base", True) else "convexDecomposition"
                    )
            contact = values.get("physics", {})
            material = UsdShade.Material.Define(stage, path.AppendChild("ContactMaterial"))
            api = UsdPhysics.MaterialAPI.Apply(material.GetPrim())
            api.CreateStaticFrictionAttr(float(contact.get("static_friction", 0.5)))
            api.CreateDynamicFrictionAttr(float(contact.get("dynamic_friction", 0.5)))
            api.CreateRestitutionAttr(float(contact.get("restitution", 0)))
            for prim in colliders:
                UsdShade.MaterialBindingAPI.Apply(prim).Bind(material, materialPurpose="physics")
            if not values.get("fix_base", True):
                UsdPhysics.RigidBodyAPI.Apply(wrapper.GetPrim()).CreateRigidBodyEnabledAttr(True)
                UsdPhysics.MassAPI.Apply(wrapper.GetPrim()).CreateMassAttr(float(values.get("mass_kg", 0.25)))
        metadata["observation"] = None
        return {"path": str(path), "articulation_roots": []}

    def scene_edit(
        self, stage: Usd.Stage, metadata: dict[str, Any], values: dict[str, Any], directory: Path
    ) -> dict[str, Any]:
        """Apply bounded transforms and explicit semantic labels before settling."""
        if set(values) != {"changes"} or not isinstance(values["changes"], list) or len(values["changes"]) > 256:
            raise ValueError("scene.edit requires at most 256 changes")
        for change in values["changes"]:
            prim = stage.GetPrimAtPath(prim_path(change["path"]))
            if not prim:
                raise ValueError("The edited prim does not exist")
            if change["op"] == "transform":
                if set(change) - {"op", "path", "translation", "rotation_deg", "scale"}:
                    raise ValueError("Unsupported transform fields")
                xform = UsdGeom.Xformable(prim)
                xform.ClearXformOpOrder()
                xform.AddTranslateOp().Set(Gf.Vec3d(*change.get("translation", [0, 0, 0])))
                xform.AddRotateXYZOp().Set(Gf.Vec3f(*change.get("rotation_deg", [0, 0, 0])))
                xform.AddScaleOp().Set(Gf.Vec3f(*change.get("scale", [1, 1, 1])))
            elif (
                change["op"] == "set" and change["attribute"] == "a100sim:semanticLabel" and change["type"] == "string"
            ):
                prim.CreateAttribute("a100sim:semanticLabel", Sdf.ValueTypeNames.String).Set(str(change["value"]))
            else:
                raise ValueError("This scene edit is not implemented")
        metadata["observation"] = None
        return {"changes_applied": len(values["changes"])}

    def physics_step(
        self, stage: Usd.Stage, metadata: dict[str, Any], values: dict[str, Any], directory: Path
    ) -> dict[str, Any]:
        """Advance CUDA PhysX and retain its measured pose and velocity state."""
        if set(values) - {"steps", "dt"}:
            raise ValueError("physics.step accepts steps and dt")
        steps = integer(values.get("steps", 180), "steps", 1, 1200)
        dt = finite(values.get("dt", 1 / 60), "dt", 0.0001, 0.02)
        physics = Physics(stage)
        try:
            paths = [
                str(p.GetPath())
                for p in stage.Traverse()
                if p.HasAPI(UsdPhysics.RigidBodyAPI) and UsdPhysics.RigidBodyAPI(p).GetRigidBodyEnabledAttr().Get()
            ]
            views = [(path, physics.view.create_rigid_body_view(path)) for path in paths]
            if any(view.count != 1 for _, view in views):
                raise ValueError("A declared dynamic body is absent from CUDA PhysX")
            physics.step(steps, dt)
            physics.sync_usd()
            bodies = []
            for path, view in views:
                pose = view.get_transforms().numpy()[0].copy()
                velocity = view.get_velocities().numpy()[0].copy()
                bodies.append(
                    {
                        "prim_path": path,
                        "position_world_m": pose[:3].tolist(),
                        "orientation_xyzw": pose[3:].tolist(),
                        "linear_velocity_world_m_s": velocity[:3].tolist(),
                        "angular_velocity_world_rad_s": velocity[3:].tolist(),
                    }
                )
                body_api = UsdPhysics.RigidBodyAPI(stage.GetPrimAtPath(path))
                body_api.CreateVelocityAttr(Gf.Vec3f(*map(float, velocity[:3])))
                # USD specifies angular velocity in degrees/second; tensors use radians.
                body_api.CreateAngularVelocityAttr(Gf.Vec3f(*map(float, np.degrees(velocity[3:]))))
            metadata.update(physics_tick=metadata["physics_tick"] + steps, dt=dt)
            metadata["observation"] = {
                "bodies": bodies,
                "physics_tick": metadata["physics_tick"],
                "dt": dt,
                "device": str(physics.view.device),
                "cpu_fallback": False,
            }
            persist(directory / "physics.json", metadata["observation"])
            frozen = stage.Flatten()
            result = {**metadata["observation"], "steps": steps, "report": str(directory / "physics.json")}
        finally:
            physics.close()
        # PhysX shutdown restores initial USD poses; retain the state measured above.
        stage.GetRootLayer().TransferContent(frozen)
        return result

    def world_observe(
        self, stage: Usd.Stage, metadata: dict[str, Any], values: dict[str, Any], directory: Path
    ) -> dict[str, Any]:
        """Read measured rigid body state from the last completed physics step."""
        if set(values) - {"bodies", "imu", "contacts"} or values.get("imu") or values.get("contacts"):
            raise ValueError("This scene observer supplies rigid body state only")
        if metadata["observation"] is None:
            raise ValueError("Run physics.step after the last scene mutation")
        result = dict(metadata["observation"])
        requested = values.get("bodies")
        if requested is not None:
            by_path = {body["prim_path"]: body for body in result["bodies"]}
            if set(requested) - set(by_path):
                raise ValueError("A requested body has no retained CUDA observation")
            result["bodies"] = [by_path[path] for path in requested]
        return result

    def scene_inspect(
        self, stage: Usd.Stage, metadata: dict[str, Any], values: dict[str, Any], directory: Path
    ) -> dict[str, Any]:
        """Return a bounded page of prim transforms and physics attributes."""
        if set(values) - {"limit", "cursor"}:
            raise ValueError("Unsupported inspection fields")
        limit = integer(values.get("limit", 2000), "limit", 1, 2000)
        offset = integer(values.get("cursor", 0), "cursor", 0, 200000)
        prims = list(stage.Traverse())
        cache = UsdGeom.XformCache(Usd.TimeCode.Default())
        result = []
        for prim in prims[offset : offset + limit]:
            attributes = {}
            for name in ("size", "radius", "height", "axis", "physics:collisionEnabled", "physics:rigidBodyEnabled"):
                attr = prim.GetAttribute(name)
                if attr:
                    attributes[name] = str(attr.Get())
            result.append(
                {
                    "path": str(prim.GetPath()),
                    "type": prim.GetTypeName(),
                    "apis": prim.GetAppliedSchemas(),
                    "attributes": attributes,
                    "world_matrix": np.asarray(cache.GetLocalToWorldTransform(prim)).tolist(),
                }
            )
        more = offset + limit < len(prims)
        return {"prims": result, "has_more": more, "next_cursor": str(offset + limit) if more else ""}

    def scene_capture(
        self, stage: Usd.Stage, metadata: dict[str, Any], values: dict[str, Any], directory: Path
    ) -> dict[str, Any]:
        """Render authored cameras and annotations from the same settled stage."""
        if set(values) - {"cameras", "size", "spp", "annotations"}:
            raise ValueError("Unsupported capture fields")
        cameras = values.get("cameras")
        if not isinstance(cameras, list) or not 1 <= len(cameras) <= 32:
            raise ValueError("Capture requires 1 to 32 authored cameras")
        size = integer(values.get("size", 768), "size", 64, 1024)
        spp = integer(values.get("spp", 32), "spp", 1, 256)
        if metadata["observation"] is None:
            raise ValueError("Settle the scene before capture")
        from a100sim.usd_renderer import render_stage

        frames = []
        for index, camera in enumerate(cameras):
            prim_path(camera)
            image = directory / f"view_{index}.png"
            frame = render_stage(
                stage, camera, "mesh", image, size=size, spp=spp, annotations=values.get("annotations", True)
            )
            frame["image"] = str(image)
            for name, value in frame.get("annotations", {}).items():
                if name in {"arrays", "labels"}:
                    frame["annotations"][name] = str(directory / value)
            frames.append(frame)
        return {"frames": frames, "physics_tick": metadata["physics_tick"], "cpu_fallback": False}

    def scene_export(
        self, stage: Usd.Stage, metadata: dict[str, Any], values: dict[str, Any], directory: Path
    ) -> dict[str, Any]:
        """Export retained USD and optionally package its resolved dependencies."""
        if set(values) - {"format"} or values.get("format", "usd") not in {"usd", "usdz"}:
            raise ValueError("Scene export supports usd or usdz")
        scene = directory / "scene.usdc"
        exported = Usd.Stage.Open(stage.Flatten())
        builtin = Path(os.environ["A100SIM_ROOT"]) / "packages/isaacsim/kit/mdl/core/Base"
        for prim in exported.Traverse():
            attribute = prim.GetAttribute("info:mdl:sourceAsset")
            value = attribute.Get() if attribute else None
            if value and not value.resolvedPath and Path(value.path).name == value.path:
                source = builtin / value.path
                if not source.is_file():
                    raise ValueError("The native MDL library cannot be packaged: " + value.path)
                attribute.Set(Sdf.AssetPath(str(source)))
        exported.GetRootLayer().Export(str(scene))
        result = {"scene": str(scene)}
        if values.get("format") == "usdz":
            package = directory / "scene.usdz"
            if not UsdUtils.CreateNewUsdzPackage(Sdf.AssetPath(str(scene)), str(package)):
                raise ValueError("USD dependency packaging failed")
            result["package"] = str(package)
        return result

    def scene_close(
        self, stage: Usd.Stage, metadata: dict[str, Any], values: dict[str, Any], directory: Path
    ) -> dict[str, Any]:
        """Mark the session closed while retaining every captured artifact."""
        if set(values) - {"delete"} or values.get("delete", False):
            raise ValueError("Closing retains scenes and captured artifacts")
        metadata["state"] = "closed"
        return {"closed": True, "retained": True}
