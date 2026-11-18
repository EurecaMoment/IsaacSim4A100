"""Protect discovery contracts without importing the simulator or transport SDK."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from a100sim.scenes.contracts import OPERATIONS, PARAMETER_SCHEMAS
from a100sim.service_queue import integer


class SceneContractTests(unittest.TestCase):
    """Keep public scene discovery complete and compatible with existing clients."""

    def test_contract_import_does_not_require_kit_or_transport_packages(self) -> None:
        root = str(Path(__file__).resolve().parents[1])
        code = (
            f"import sys; sys.path.insert(0, {root!r}); "
            "from a100sim.scenes.contracts import PARAMETER_SCHEMAS; "
            "import json; print(json.dumps(sorted(PARAMETER_SCHEMAS)))"
        )
        output = subprocess.check_output([sys.executable, "-S", "-c", code], text=True)
        self.assertEqual(set(json.loads(output)), set(OPERATIONS))

    def test_every_operation_has_a_closed_top_level_schema(self) -> None:
        self.assertEqual(set(PARAMETER_SCHEMAS), set(OPERATIONS))
        for name, schema in PARAMETER_SCHEMAS.items():
            with self.subTest(operation=name):
                self.assertEqual(schema["type"], "object")
                self.assertFalse(schema["additionalProperties"])
                self.assertTrue(schema["properties"])
                self.assertTrue(set(schema["required"]) <= schema["properties"].keys())
                self.assertNotIn("source_path", schema["properties"])
                self.assertNotIn("operation", schema["properties"])
                if name != "asset.import":
                    self.assertIn("session_id", schema["required"])

    def test_native_import_advertises_optional_physics_and_source_axis(self) -> None:
        schema = PARAMETER_SCHEMAS["asset.import"]
        self.assertEqual(schema["required"], ["source"])
        fields = schema["properties"]
        self.assertTrue({"fix_base", "mass_kg", "source_up_axis", "physics"} <= fields.keys())
        self.assertNotIn("default", fields["fix_base"])
        self.assertNotIn("default", fields["source_up_axis"])
        self.assertTrue(fields["physics"]["additionalProperties"])
        self.assertTrue({"collider", "mass_source"} <= fields["physics"]["properties"].keys())
        self.assertEqual(set(fields["source"]["required"]), {"asset_id", "relative_path"})
        self.assertFalse(fields["source"]["additionalProperties"])

    def test_defaults_preserve_minimal_valid_requests(self) -> None:
        expected = {
            "physics.step": {"session_id"},
            "world.observe": {"session_id"},
            "scene.inspect": {"session_id"},
            "scene.export": {"session_id"},
            "scene.close": {"session_id"},
        }
        for name, required in expected.items():
            with self.subTest(operation=name):
                schema = PARAMETER_SCHEMAS[name]
                self.assertEqual(set(schema["required"]), required)
        changes = PARAMETER_SCHEMAS["scene.edit"]["properties"]["changes"]
        self.assertEqual(changes.get("minItems", 0), 0)
        transform = changes["items"]["oneOf"][0]
        self.assertEqual(set(transform["required"]), {"op", "path"})

    def test_discovery_excludes_unsupported_retained_scene_features(self) -> None:
        observe = PARAMETER_SCHEMAS["world.observe"]["properties"]
        self.assertNotIn("robots", observe)
        self.assertEqual(observe["imu"]["enum"], [False])
        self.assertEqual(observe["contacts"]["enum"], [False])
        self.assertNotIn("path", PARAMETER_SCHEMAS["scene.inspect"]["properties"])
        self.assertEqual(PARAMETER_SCHEMAS["scene.export"]["properties"]["format"]["enum"], ["usd", "usdz"])
        self.assertEqual(PARAMETER_SCHEMAS["scene.close"]["properties"]["delete"]["enum"], [False])

    def test_returned_cursor_strings_are_valid_offsets(self) -> None:
        cursor = PARAMETER_SCHEMAS["scene.inspect"]["properties"]["cursor"]
        self.assertEqual({variant["type"] for variant in cursor["anyOf"]}, {"integer", "string"})
        for offset in (0, 2000, 198000, 200000):
            with self.subTest(offset=offset):
                self.assertEqual(integer(str(offset), "cursor", 0, 200000), offset)
        for value in ("", "-1", "200001", "1.5", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                integer(value, "cursor", 0, 200000)


if __name__ == "__main__":
    unittest.main()
