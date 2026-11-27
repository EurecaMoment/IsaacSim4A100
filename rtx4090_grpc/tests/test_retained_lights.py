"""Exercise render-only light opinions against a real OpenUSD stage."""
import sys
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root.parent / "a100"))


class RetainedLightTests(unittest.TestCase):
    def test_only_declared_linear_radiance_lights_convert_and_the_stage_is_restored(self):
        try:
            from pxr import Sdf, Usd, UsdLux
        except ImportError:
            self.skipTest("OpenUSD required")
        from server.retained_sessions import rtx_light_units

        stage = Usd.Stage.CreateInMemory()
        authored = UsdLux.DomeLight.Define(stage, "/Authored")
        marked = UsdLux.RectLight.Define(stage, "/Marked")
        for light in (authored, marked):
            light.CreateIntensityAttr(2.0)
        marked.GetPrim().CreateAttribute("benchforge:intensityUnit", Sdf.ValueTypeNames.String).Set("linear_radiance")
        original = stage.GetRootLayer().ExportToString()
        with rtx_light_units(stage):
            self.assertEqual(marked.GetIntensityAttr().Get(), 600)
            self.assertEqual(authored.GetIntensityAttr().Get(), 2)
        self.assertEqual(marked.GetIntensityAttr().Get(), 2)
        self.assertEqual(stage.GetRootLayer().ExportToString(), original)


if __name__ == "__main__":
    unittest.main()

