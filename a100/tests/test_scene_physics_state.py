"""Validate tensor-to-USD state retention without claiming GPU execution."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import numpy as np
from a100sim.scenes.physics_state import snapshot_physics, validate_retained_poses
from pxr import Gf, Usd, UsdGeom, UsdPhysics


def body(path: str, position: list[float], angle: float = 0) -> dict:
    """Create a measured pose with known linear and angular velocities."""
    quaternion = Gf.Rotation(Gf.Vec3d(0, 0, 1), angle).GetQuat()
    return {
        "prim_path": path,
        "position_world_m": position,
        "orientation_xyzw": [*quaternion.GetImaginary(), quaternion.GetReal()],
        "linear_velocity_world_m_s": [1, 2, 3],
        "angular_velocity_world_rad_s": [0, 0, np.pi],
    }


class PhysicsStateTests(unittest.TestCase):
    """Keep body pose, shape scale and persisted export in one state."""

    def setUp(self) -> None:
        self.stage = Usd.Stage.CreateInMemory()
        UsdGeom.SetStageMetersPerUnit(self.stage, 1)
        UsdGeom.SetStageUpAxis(self.stage, UsdGeom.Tokens.z)
        UsdGeom.Xform.Define(self.stage, "/World")
        self.cube = UsdGeom.Xform.Define(self.stage, "/World/Cube")
        self.cube.AddTranslateOp().Set(Gf.Vec3d(0, 0, 1.1))
        UsdPhysics.RigidBodyAPI.Apply(self.cube.GetPrim())
        geometry = UsdGeom.Cube.Define(self.stage, "/World/Cube/Part0")
        geometry.CreateSizeAttr(1)
        geometry.AddScaleOp().Set(Gf.Vec3f(0.2, 0.2, 0.2))

    def matrix(self, stage: Usd.Stage, path: str) -> np.ndarray:
        """Read the same default-time world matrix as the CUDA USD renderer."""
        cache = UsdGeom.XformCache(Usd.TimeCode.Default())
        return np.asarray(cache.GetLocalToWorldTransform(stage.GetPrimAtPath(path))).T

    def test_settled_snapshot_and_export_use_measured_pose_not_initial_usd(self) -> None:
        state = body("/World/Cube", [0.002, -0.003, 0.1], 90)
        snapshot = snapshot_physics(self.stage, [state])
        self.assertEqual(self.matrix(self.stage, "/World/Cube")[2, 3], 1.1)
        np.testing.assert_allclose(self.matrix(snapshot, "/World/Cube/Part0")[:3, 3], [0.002, -0.003, 0.1])
        np.testing.assert_allclose(np.linalg.norm(self.matrix(snapshot, "/World/Cube/Part0")[:3, :3], axis=0), 0.2)
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / "settled.usdc"
            snapshot.GetRootLayer().Export(str(file))
            reloaded = Usd.Stage.Open(str(file))
            validate_retained_poses(reloaded, [state])
            np.testing.assert_allclose(
                self.matrix(reloaded, "/World/Cube/Part0"), self.matrix(snapshot, "/World/Cube/Part0")
            )
            del reloaded  # Windows USD layers hold the crate file open.

    def test_parent_transform_body_scale_and_velocity_survive_snapshot(self) -> None:
        parent = UsdGeom.Xformable(self.stage.GetPrimAtPath("/World"))
        parent.AddTranslateOp().Set(Gf.Vec3d(10, 20, 3))
        parent.AddRotateZOp().Set(40)
        self.cube.AddScaleOp().Set(Gf.Vec3d(2, 3, 4))
        state = body("/World/Cube", [1, 2, 0.4], 15)
        snapshot = snapshot_physics(self.stage, [state])
        np.testing.assert_allclose(np.linalg.norm(self.matrix(snapshot, "/World/Cube")[:3, :3], axis=0), [2, 3, 4])
        api = UsdPhysics.RigidBodyAPI(snapshot.GetPrimAtPath("/World/Cube"))
        np.testing.assert_allclose(api.GetVelocityAttr().Get(), [1, 2, 3])
        np.testing.assert_allclose(api.GetAngularVelocityAttr().Get(), [0, 0, 180])
        validate_retained_poses(snapshot, [state])

    def test_nested_bodies_and_reset_stack_have_independent_world_poses(self) -> None:
        child = UsdGeom.Xform.Define(self.stage, "/World/Cube/Child")
        child.AddTranslateOp().Set(Gf.Vec3d(0, 0, 1))
        UsdPhysics.RigidBodyAPI.Apply(child.GetPrim())
        states = [body("/World/Cube/Child", [3, 4, 0.2], 60), body("/World/Cube", [1, 2, 0.1], 30)]
        validate_retained_poses(snapshot_physics(self.stage, states), states)
        child.SetResetXformStack(True)
        snapshot = snapshot_physics(self.stage, states)
        validate_retained_poses(snapshot, states)
        self.assertTrue(UsdGeom.Xformable(snapshot.GetPrimAtPath("/World/Cube/Child")).GetResetXformStack())

    def test_stale_stage_cannot_be_reported_as_settled(self) -> None:
        with self.assertRaisesRegex(ValueError, "differs from measured physics"):
            validate_retained_poses(self.stage, [body("/World/Cube", [0, 0, 0.1])])

    def test_session_retains_tensor_pose_after_physics_reset(self) -> None:
        file = Path(__file__).resolve().parents[1] / "a100sim/scenes/sessions.py"
        spec = importlib.util.spec_from_file_location("scene_sessions_fixture", file)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"omni": ModuleType("omni"), "omni.usd": ModuleType("omni.usd")}):
            spec.loader.exec_module(module)
        initial = self.stage.Flatten()
        measured = np.asarray([[0, 0, 0.1, 0, 0, 0, 1]], dtype=np.float32)
        rigid = SimpleNamespace(
            count=1,
            get_transforms=lambda: SimpleNamespace(numpy=lambda: measured),
            get_velocities=lambda: SimpleNamespace(numpy=lambda: np.zeros((1, 6), dtype=np.float32)),
        )

        class PhysicsFixture:
            """Model suppressed readback and PhysX reset while returning tensor state."""

            def __init__(self, stage: Usd.Stage) -> None:
                self.stage = stage
                self.view = SimpleNamespace(device="cuda:0", create_rigid_body_view=lambda _path: rigid)

            def step(self, _steps: int, _dt: float) -> None:
                pass

            def sync_usd(self) -> None:
                pass  # GPU tensor state does not populate authored USD.

            def close(self) -> None:
                self.stage.GetRootLayer().TransferContent(initial)

        with tempfile.TemporaryDirectory() as temporary, patch.object(module, "Physics", PhysicsFixture):
            directory = Path(temporary)
            session = module.Sessions(directory)
            metadata = {"physics_tick": 0, "observation": None}
            result = session.physics_step(self.stage, metadata, {"steps": 240, "dt": 1 / 120}, directory)
            validate_retained_poses(self.stage, result["bodies"])
            np.testing.assert_allclose(self.matrix(self.stage, "/World/Cube/Part0")[2, 3], 0.1)
            # The next call reloads this retained layer, not the old source asset.
            retained = directory / "stage.usdc"
            self.stage.Flatten().Export(str(retained))
            validate_retained_poses(Usd.Stage.Open(str(retained)), metadata["observation"]["bodies"])


if __name__ == "__main__":
    unittest.main()
