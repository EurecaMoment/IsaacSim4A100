"""Render a fixed USD snapshot in a fresh process without starting Kit or PhysX."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from a100sim.service_queue import integer, persist


def run_capture(stage, metadata, values, directory):
    from a100sim.scenes.physics_state import validate_retained_poses
    from a100sim.usd_renderer import render_stage
    from pxr import Sdf

    if set(values) - {"cameras", "size", "spp", "annotations"}:
        raise ValueError("Unsupported capture fields")
    cameras = values.get("cameras")
    if not isinstance(cameras, list) or not 1 <= len(cameras) <= 32:
        raise ValueError("Capture requires 1 to 32 authored cameras")
    for camera in cameras:
        path = Sdf.Path(camera)
        if not path.IsAbsolutePath() or not path.IsPrimPath() or str(path) == "/":
            raise ValueError("A nonroot absolute USD prim path is required")
    size = integer(values.get("size", 768), "size", 64, 2048)
    spp = integer(values.get("spp", 32), "spp", 1, 1024)
    if metadata["observation"] is None:
        raise ValueError("Settle the scene before capture")
    validate_retained_poses(stage, metadata["observation"]["bodies"])
    frames = []
    for index, camera in enumerate(cameras):
        image = directory / f"view_{index}.png"
        frame = render_stage(
            stage, camera, "mesh", image, size=size, spp=spp, annotations=values.get("annotations", True)
        )
        frame["image"] = str(image)
        for key in ("arrays", "labels"):
            if key in frame.get("annotations", {}):
                frame["annotations"][key] = str(directory / frame["annotations"][key])
        frames.append(frame)
    return {
        "frames": frames,
        "physics_tick": metadata["physics_tick"],
        "cpu_fallback": any(frame.get("cpu_fallback", False) for frame in frames),
    }


def worker_env(threads):
    env = os.environ.copy()
    root = Path(env["A100SIM_ROOT"]) / "packages" / "isaacsim"
    (usd,) = (root / "extscache").glob("omni.usd.libs-*")
    env["LD_LIBRARY_PATH"] = os.pathsep.join(
        map(str, (usd / "bin", root / "kit", root / "kit/kernel/plugins", env.get("LD_LIBRARY_PATH", "")))
    )
    env["PYTHONPATH"] = str(usd) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(
        PXR_WORK_THREAD_LIMIT=str(threads),
        TBB_NUM_THREADS=str(threads),
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
    )
    return env


def render_snapshot(snapshot, directory, threads):
    """Use exec, never fork a copy of a running Kit/CUDA context."""
    with (directory / "capture-worker.log").open("w") as log:
        command = [sys.executable, "-m", "a100sim.scenes.capture_worker", str(snapshot), str(directory), str(threads)]
        proc = subprocess.run(command, env=worker_env(threads), stdout=log, stderr=subprocess.STDOUT, check=False)
    receipt = directory / "capture-result.json"
    if not receipt.exists():
        raise RuntimeError(f"Capture worker exited {proc.returncode}; see {directory / 'capture-worker.log'}")
    result = json.loads(receipt.read_text())
    if proc.returncode or "error" in result:
        raise ValueError(result.get("error", f"Capture worker exited {proc.returncode}"))
    return result


def main():
    import traceback

    snapshot, directory = map(Path, sys.argv[1:3])
    try:
        import drjit as dr
        from pxr import Usd

        dr.set_thread_count(int(sys.argv[3]))
        metadata = json.loads((directory / "capture-session.json").read_text())
        request = json.loads((directory / "request.json").read_text())
        values = {k: v for k, v in request.items() if k not in ("session_id", "operation")}
        stage = Usd.Stage.Open(str(snapshot))
        if not stage:
            raise ValueError("The retained stage could not be opened")
        result = {"session_id": metadata["session_id"], **run_capture(stage, metadata, values, directory)}
    except Exception as exc:
        traceback.print_exc()
        persist(directory / "capture-result.json", {"error": str(exc)})
        return 1
    persist(directory / "capture-result.json", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
