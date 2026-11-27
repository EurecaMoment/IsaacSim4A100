"""Validation and per-view receipts for the RTX capture protocol."""

from pathlib import Path

import numpy as np

from a100sim.views import PORTABLE_ID


def registered_mesh(asset_root: Path, body: dict) -> dict:
    asset_id = body["asset_id"]
    if not isinstance(asset_id, str) or not PORTABLE_ID.fullmatch(asset_id):
        raise ValueError("asset_id must be a portable directory name")
    if body.get("shape") != "mesh" or "asset" in body:
        raise ValueError("asset_id requires shape=mesh and cannot be combined with asset")
    directory = (asset_root / asset_id).resolve()
    mesh = (directory / "mesh.npz").resolve()
    if not directory.is_relative_to(asset_root) or not mesh.is_relative_to(directory) or not mesh.is_file():
        raise ValueError(f"Registered mesh {asset_id} requires mesh.npz under the asset root")
    return {key: value for key, value in body.items() if key != "asset_id"} | {"asset": str(directory)}


def visible_objects(semantic, labels):
    """Visible pixel bounding boxes, with inclusive image coordinates."""
    objects = []
    for label_id in np.unique(semantic):
        label = labels.get(str(label_id), labels.get(int(label_id), {}))
        if not isinstance(label, dict) or label.get("class") in (None, "BACKGROUND", "UNLABELLED"):
            continue
        ys, xs = np.nonzero(semantic == label_id)
        objects.append({"id": str(label["class"]), "semantic_id": int(label_id), "pixels": len(xs),
                        "bbox_xyxy_inclusive": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]})
    return objects
