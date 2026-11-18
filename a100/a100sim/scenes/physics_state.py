"""Write measured CUDA rigid poses into independent retained USD snapshots."""

from __future__ import annotations

from typing import Any

import numpy as np
from pxr import Gf, Usd, UsdGeom, UsdPhysics


def snapshot_physics(stage: Usd.Stage, bodies: list[dict[str, Any]]) -> Usd.Stage:
    """Freeze tensor state without relying on suppressed PhysX USD readback.

    The copy retains geometry scale, descendant transforms and reset-stack
    semantics. Parents are updated before children so world poses remain exact.
    The running physics stage is never teleported or modified by this writer.
    """
    snapshot = Usd.Stage.Open(stage.Flatten())
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    prepared = []
    for body in bodies:
        prim = snapshot.GetPrimAtPath(body["prim_path"])
        if not prim or not prim.HasAPI(UsdPhysics.RigidBodyAPI):
            raise ValueError("Measured rigid body is absent from the retained stage")
        position = np.asarray(body["position_world_m"], dtype=float)
        quaternion = np.asarray(body["orientation_xyzw"], dtype=float)
        if position.shape != (3,) or quaternion.shape != (4,) or not np.isfinite(position).all():
            raise ValueError("Invalid measured rigid body pose")
        length = np.linalg.norm(quaternion)
        if not np.isfinite(length) or length < 1e-12:
            raise ValueError("Invalid measured rigid body quaternion")
        x, y, z, w = map(float, quaternion / length)
        # Gf.Transform preserves scale orientation as well as scale. Replacing
        # the entire world matrix with only rotation/translation shrinks assets.
        transform = Gf.Transform(cache.GetLocalToWorldTransform(prim))
        transform.SetRotation(Gf.Rotation(Gf.Quatd(w, Gf.Vec3d(x, y, z))))
        transform.SetTranslation(Gf.Vec3d(*map(float, position)))
        prepared.append((body, transform.GetMatrix()))
    for body, world in sorted(prepared, key=lambda row: row[0]["prim_path"].count("/")):
        prim = snapshot.GetPrimAtPath(body["prim_path"])
        transform = UsdGeom.Xformable(prim)
        reset = transform.GetResetXformStack()
        parent = Gf.Matrix4d(1) if reset else cache.GetLocalToWorldTransform(prim.GetParent())
        if abs(parent.GetDeterminant()) < 1e-12:
            raise ValueError("Rigid body parent has a noninvertible world transform")
        transform.MakeMatrixXform().Set(world * parent.GetInverse())
        transform.SetResetXformStack(reset)
        api = UsdPhysics.RigidBodyAPI(prim)
        api.CreateVelocityAttr(Gf.Vec3f(*map(float, body["linear_velocity_world_m_s"])))
        api.CreateAngularVelocityAttr(Gf.Vec3f(*map(float, np.degrees(body["angular_velocity_world_rad_s"]))))
        cache.Clear()
    validate_retained_poses(snapshot, bodies)
    return snapshot


def validate_retained_poses(stage: Usd.Stage, bodies: list[dict[str, Any]]) -> None:
    """Reject capture/export of stale USD instead of labeling it as settled data."""
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    for body in bodies:
        prim = stage.GetPrimAtPath(body["prim_path"])
        if not prim:
            raise ValueError("Retained stage is missing a measured body; run physics.step again")
        transform = Gf.Transform(cache.GetLocalToWorldTransform(prim))
        actual = transform.GetRotation().GetQuat()
        orientation = np.asarray([*actual.GetImaginary(), actual.GetReal()], dtype=float)
        expected = np.asarray(body["orientation_xyzw"], dtype=float)
        expected /= np.linalg.norm(expected)
        if not np.allclose(transform.GetTranslation(), body["position_world_m"], atol=1e-5, rtol=0) or not np.isclose(
            abs(float(np.dot(orientation, expected))), 1, atol=1e-6, rtol=0
        ):
            raise ValueError("Retained USD differs from measured physics state; run physics.step again before capture")
