"""Validate GPU evidence for a synthetic 0.2-meter cube settled on a floor.

Pass an evidence directory containing world.observe.json and scene.capture.json
job documents, downloaded capture/view_*-annotations.npz arrays, and an exported
export/scene.usdc stage. The scene must have two cameras whose world X axes are
horizontal and a measured rigid body whose prim path ends with /cube.

This checks retained poses, both camera transforms, exported USD, and world
heights reconstructed from captured depth/instance arrays. It does not invoke
or submit simulator work, and it does not accept other scene fixtures.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
from pxr import Usd, UsdGeom


def validate(directory: Path) -> dict:
    """Require all evidence channels to place the synthetic cube on the floor."""
    observed = json.loads((directory / "world.observe.json").read_text())["result"]
    captured = json.loads((directory / "scene.capture.json").read_text())["result"]
    body = next(row for row in observed["bodies"] if row["prim_path"].endswith("/cube"))
    position = np.asarray(body["position_world_m"])
    np.testing.assert_allclose(position[2], 0.1, atol=0.002, rtol=0)
    if captured["physics_tick"] != observed["physics_tick"]:
        raise ValueError("Capture and observation belong to different physics ticks")
    checked = []
    for index, frame in enumerate(captured["frames"]):
        source = next(row for row in frame["usd_sources"] if row["prim"] == body["prim_path"] + "/Part0")
        np.testing.assert_allclose(np.asarray(source["world_matrix"])[:3, 3], position, atol=1e-5, rtol=0)
        arrays_path = next((directory / "capture").rglob(f"view_{index}-annotations.npz"))
        with np.load(arrays_path) as arrays:
            labels = frame["annotations"]["instances"]
            number = int(next(key for key, row in labels.items() if row["prim_path"] == source["prim"]))
            mask = arrays["valid"] & (arrays["instance_id"] == number)
            if not mask.any():
                raise ValueError("The cube has no visible annotated pixels")
            height, width = arrays["depth_m"].shape
            yy, _xx = np.nonzero(mask)
            depth = arrays["depth_m"][mask]
            camera = np.asarray(frame["camera_world"])
            tangent = math.tan(math.radians(frame["fov_degrees"]) / 2)
            # USD cameras look along local -Z. For this fixture camera X is
            # horizontal in world space, so pixel handedness cannot affect Z.
            np.testing.assert_allclose(camera[2, 0], 0, atol=1e-8)
            vertical = (1 - 2 * (yy + 0.5) / height) * depth * tangent * height / width
            world_z = camera[2, 3] - depth * camera[2, 2] + vertical * camera[2, 1]
            if world_z.min() < -0.003 or world_z.max() > 0.203:
                raise ValueError(f"Camera {index} depth depicts an airborne cube: Z={world_z.min(), world_z.max()}")
            checked.append(
                {
                    "camera": frame["camera"],
                    "cube_pixels": int(mask.sum()),
                    "depth_world_z_range": [float(world_z.min()), float(world_z.max())],
                }
            )
    if len(checked) != 2:
        raise ValueError("This fixture requires both cameras")
    exported = next((directory / "export").rglob("scene.usdc"))
    stage = Usd.Stage.Open(str(exported))
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    transform = np.asarray(cache.GetLocalToWorldTransform(stage.GetPrimAtPath(body["prim_path"]))).T
    np.testing.assert_allclose(transform[:3, 3], position, atol=1e-5, rtol=0)
    return {"passed": True, "position_world_m": position.tolist(), "cameras": checked, "export": str(exported)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(validate(arguments.directory), indent=2))
