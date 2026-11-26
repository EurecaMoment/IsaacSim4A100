"""Check native mesh corner layout and texture transfer against authored values."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(
    all(importlib.util.find_spec(name) for name in ("mitsuba", "numpy", "PIL", "pxr")),
    "Run in the initialized Isaac/Mitsuba environment",
)
class SceneMeshTests(unittest.TestCase):
    def setUp(self) -> None:
        import mitsuba as mi

        self.previous_variant = mi.variant()
        mi.set_variant("scalar_rgb")

    def tearDown(self) -> None:
        import mitsuba as mi

        if self.previous_variant:
            mi.set_variant(self.previous_variant)

    def test_triangle_and_polygon_corner_attributes(self) -> None:
        import mitsuba as mi
        import numpy as np
        from a100sim.scenes.meshes import mesh_shape
        from pxr import Sdf, Usd, UsdGeom

        for counts, indices, corners in (
            ([3, 3], [0, 1, 2, 0, 2, 3], [0, 1, 2, 3, 4, 5]),
            ([4, 3], [0, 1, 2, 3, 0, 2, 4], [0, 1, 2, 0, 2, 3, 4, 5, 6]),
        ):
            with self.subTest(counts=counts):
                stage = Usd.Stage.CreateInMemory()
                mesh = UsdGeom.Mesh.Define(stage, "/Mesh")
                points = np.array([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 1, 1)], np.float32)
                mesh.CreatePointsAttr(points)
                mesh.CreateSubdivisionSchemeAttr("none")
                mesh.CreateFaceVertexCountsAttr(counts)
                mesh.CreateFaceVertexIndicesAttr(indices)
                colors = [(1, 0, 0), (0, 1, 0)]
                mesh.CreateDisplayColorPrimvar("uniform").Set(colors)
                st = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, "faceVarying")
                uv = np.arange(2 * len(indices), dtype=np.float32).reshape(-1, 2) / 16
                st.Set(uv)
                shape = mesh_shape(mesh.GetPrim(), np.eye(4))
                params = mi.traverse(shape)
                np.testing.assert_array_equal(params["faces"], np.arange(len(corners)))
                np.testing.assert_array_equal(
                    np.asarray(params["vertex_positions"]).reshape(-1, 3), points[np.array(indices)[corners]]
                )
                uv[:, 1] = 1 - uv[:, 1]
                np.testing.assert_array_equal(np.asarray(params["vertex_texcoords"]).reshape(-1, 2), uv[corners])
                expanded_colors = np.repeat(colors, (np.array(counts) - 2) * 3, axis=0)
                np.testing.assert_array_equal(np.asarray(params["vertex_color"]).reshape(-1, 3), expanded_colors)

    def test_texture_lookup_preserves_all_byte_values(self) -> None:
        import numpy as np
        from a100sim.scenes.materials import bitmap
        from PIL import Image

        source = np.arange(256, dtype=np.uint8).reshape(16, 16)
        rgba = np.stack([np.roll(source, shift, axis=0) for shift in range(4)], axis=-1)
        values = rgba.astype(np.float32) / 255
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "channels.png")
            Image.fromarray(rgba).save(path)
            rgb = values[:, :, :3]
            linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
            for tint in ([1, 1, 1], [0.91, 0.73, 0.21]):
                np.testing.assert_array_equal(
                    np.asarray(bitmap(path, multiply=tint)["bitmap"]), (linear * tint).astype(np.float32)
                )
            scale = 0.618033988749895
            actual = np.asarray(bitmap(path, channel=1, raw=True, multiply=scale)["bitmap"]).reshape(source.shape)
            np.testing.assert_array_equal(actual, (values[:, :, 1] * np.asarray(scale)).astype(np.float32))


if __name__ == "__main__":
    unittest.main()
