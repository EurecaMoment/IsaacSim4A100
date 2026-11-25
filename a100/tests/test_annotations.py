"""Exercise scalar primary-ray annotations against real Mitsuba geometry."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(
    all(importlib.util.find_spec(name) for name in ("mitsuba", "numpy", "PIL", "pxr")),
    "Run in the initialized Isaac/Mitsuba environment",
)
class ScalarAnnotationTests(unittest.TestCase):
    """Keep metric geometry, IDs, misses, and scalar API compatibility correct."""

    def setUp(self) -> None:
        import mitsuba as mi
        import numpy as np
        from pxr import Sdf, Usd, UsdGeom

        self.previous_variant = mi.variant()
        mi.set_variant("scalar_rgb")
        self.stage = Usd.Stage.CreateInMemory()
        UsdGeom.SetStageMetersPerUnit(self.stage, 0.01)
        for path, label in (("/World/Cube", "cube"), ("/World/Sphere", "sphere")):
            prim = self.stage.DefinePrim(path, "Xform")
            prim.CreateAttribute("a100sim:semanticLabel", Sdf.ValueTypeNames.String).Set(label)
        self.stage.DefinePrim("/World/Cube/Visual", "Cube")
        self.sources = [{"prim": "/World/Cube/Visual"}, {"prim": "/World/Sphere"}]
        self.scene = mi.load_dict({
            "type": "scene",
            "cube": {"type": "cube", "id": "_World_Cube_Visual"},
            "sphere": {
                "type": "sphere", "id": "_World_Sphere",
                "to_world": mi.ScalarTransform4f().translate([2.4, 0, 0]).scale(0.5),
            },
            "sensor": {
                "type": "perspective", "fov": 65,
                "to_world": mi.ScalarTransform4f().look_at(
                    origin=[0, 0, 4], target=[0, 0, 0], up=[0, 1, 0]),
                "film": {"type": "hdrfilm", "width": 32, "height": 32, "rfilter": {"type": "box"}},
            },
        })
        self.camera_world = np.eye(4)
        self.camera_world[2, 3] = 4

    def tearDown(self) -> None:
        import mitsuba as mi

        self.scene = None
        self.stage = None
        if self.previous_variant:
            mi.set_variant(self.previous_variant)

    def test_scalar_capture_preserves_metric_geometry_and_semantic_instance_arrays(self) -> None:
        import numpy as np

        from a100sim.annotations import capture_primary_surfaces

        with tempfile.TemporaryDirectory() as temporary:
            result = capture_primary_surfaces(
                self.scene, self.stage, self.sources, self.camera_world, Path(temporary) / "surface")
            with np.load(result["arrays"]) as arrays:
                valid = arrays["valid"]
                self.assertEqual(arrays["depth_m"].shape, (32, 32))
                self.assertEqual(arrays["normal_world"].shape, (32, 32, 3))
                self.assertTrue(valid.any())
                self.assertTrue((~valid).any())
                self.assertAlmostEqual(float(arrays["depth_m"][16, 16]), 0.03, places=6)
                np.testing.assert_allclose(arrays["normal_world"][16, 16], [0, 0, 1], atol=1e-6)
                self.assertTrue(np.all(arrays["range_m"][valid] >= arrays["depth_m"][valid] - 1e-6))
                np.testing.assert_allclose(np.linalg.norm(arrays["normal_world"][valid], axis=-1), 1, atol=1e-6)
                self.assertEqual(set(np.unique(arrays["instance_id"])), {0, 1, 2})
                self.assertEqual(set(np.unique(arrays["semantic_id"])), {0, 1, 2})
                for name in ("depth_m", "range_m", "normal_world", "instance_id", "semantic_id"):
                    self.assertFalse(arrays[name][~valid].any(), name)
                cube_id = int(arrays["instance_id"][16, 16])
                self.assertEqual(result["instances"][str(cube_id)]["prim_path"], "/World/Cube/Visual")
                self.assertEqual(result["instances"][str(cube_id)]["semantic_label"], "cube")
            for key in ("arrays", "labels", "depth_preview", "normal_preview", "instance_preview"):
                self.assertTrue(Path(result[key]).is_file(), key)
            self.assertEqual(result["render_backend"], "scalar_rgb")

    def test_shared_shape_mapping_does_not_require_jit_pointer_bindings(self) -> None:
        import mitsuba as mi

        from a100sim.annotations import prepare_surface_ids

        self.assertIs(mi.UInt32, int)
        self.assertFalse(hasattr(mi, "ShapePtr"))
        surface_ids = prepare_surface_ids(self.scene, self.stage, self.sources)
        self.assertEqual(set(surface_ids["scalar_ids"]), {"_World_Cube_Visual", "_World_Sphere"})
        self.assertEqual(surface_ids["instance_lut"].tolist(), [0, 1, 2])
        self.assertEqual(set(surface_ids["classes"].values()), {"unlabeled", "cube", "sphere"})


if __name__ == "__main__":
    unittest.main()
