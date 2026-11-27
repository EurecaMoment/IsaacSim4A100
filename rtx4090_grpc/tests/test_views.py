"""Exercise the public camera and registered mesh contract without a GPU."""

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "a100"))
from server import codec, rpc
from a100sim.views import camera_views
from server.views import registered_mesh, visible_objects


class ViewsTest(unittest.TestCase):
    def test_request_round_trip(self):
        pb = rpc.simulation_pb2
        request = pb.SimulateRequest(steps=4)
        request.bodies.add(id="chair", shape="mesh", asset_id="trellis-chair")
        for view_id, x in (("left", -2), ("right", 2)):
            request.render.views.add(id=view_id, eye=pb.Vec3(x=x, z=1), target=pb.Vec3())
        payload = codec.simulate_request_to_payload(pb.SimulateRequest.FromString(request.SerializeToString()))
        self.assertEqual(payload["bodies"][0]["asset_id"], "trellis-chair")
        views = camera_views(payload["render"]["views"])
        self.assertEqual([v["eye"][0] for v in views], [-2, 2])
        self.assertEqual(views[1]["up"], [0, 0, 1])
        with self.assertRaisesRegex(ValueError, "unique"):
            camera_views([views[0], views[0]])
        with self.assertRaisesRegex(ValueError, "degenerate"):
            camera_views([{**views[0], "up": [2, 0, -1]}])

    def test_registered_mesh(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            body = {"shape": "mesh", "asset_id": "trellis-chair"}
            asset = root / "trellis-chair"
            asset.mkdir()
            with self.assertRaisesRegex(ValueError, "mesh.npz"):
                registered_mesh(root, body)
            (asset / "mesh.npz").touch()
            self.assertEqual(registered_mesh(root, body), {"shape": "mesh", "asset": str(asset)})
            with self.assertRaisesRegex(ValueError, "portable"):
                registered_mesh(root, {**body, "asset_id": "../malformed"})

    def test_visible_boxes_are_computed_per_mask(self):
        labels = {"0": {"class": "BACKGROUND"}, "2": {"class": "chair"}}
        mask = np.array([[0, 0, 2], [0, 2, 2]], dtype=np.uint32)
        result = visible_objects(mask, labels)
        self.assertEqual(result, [{"id": "chair", "semantic_id": 2, "pixels": 3, "bbox_xyxy_inclusive": [1, 0, 2, 1]}])
        self.assertEqual(visible_objects(mask * 0, labels), [])


if __name__ == "__main__":
    unittest.main()
