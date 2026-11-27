"""Native USD material dispatch stays on the explicit PBR adapter path."""

from unittest.mock import Mock, patch
import unittest

from a100sim.scenes.materials import native_surface


class NativeMaterialTests(unittest.TestCase):
    def test_preview_surface_returned_by_mdl_query_uses_preview_adapter(self):
        prim = Mock()
        shader = Mock()
        shader.GetIdAttr.return_value.Get.return_value = "UsdPreviewSurface"
        material = Mock()
        material.ComputeSurfaceSource.return_value = (shader, "")

        with patch("a100sim.scenes.materials.UsdShade.MaterialBindingAPI") as binding, patch(
            "a100sim.scenes.materials.read_usd_material", return_value={"type": "diffuse"}
        ) as read_preview:
            binding.return_value.ComputeBoundMaterial.return_value = (material, None)
            surface, emitter = native_surface(prim, vertex_color=True, channels=("metallic",))

        self.assertEqual(surface, {"type": "diffuse"})
        self.assertIsNone(emitter)
        read_preview.assert_called_once_with(prim, vertex_color=True, channels=("metallic",))
