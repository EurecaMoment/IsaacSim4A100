"""Discover allowed A100 devices without depending on a machine's GPU numbering."""

from __future__ import annotations

import csv
import os
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class GPU:
    """One physical device reported by the host driver."""

    index: str
    uuid: str
    name: str
    driver: str
    compute_capability: str


def list_gpus() -> list[GPU]:
    """Query the driver without creating a CUDA context."""
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,uuid,name,driver_version,compute_cap", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return [GPU(*(item.strip() for item in row)) for row in csv.reader(result.stdout.splitlines()) if len(row) == 5]


def _is_supported_gpu(gpu: GPU) -> bool:
    """Accept the Ampere SM 8.0 architecture used by the A100 adapter."""
    return gpu.compute_capability == "8.0"


def select_gpu(gpus: list[GPU], selection: str = "auto", visible: str | None = None) -> GPU:
    """Select an SM 8.0 GPU inside the inherited CUDA device mask."""
    allowed = gpus
    if visible is not None:
        tokens = [item.strip() for item in visible.split(",") if item.strip()]
        allowed = [gpu for token in tokens for gpu in gpus if token in (gpu.index, gpu.uuid)]
    if selection == "auto":
        matches = [gpu for gpu in allowed if _is_supported_gpu(gpu)]
    else:
        matches = [gpu for gpu in allowed if selection in (gpu.index, gpu.uuid)]
    if not matches:
        raise RuntimeError("No matching SM 8.0 GPU is available inside CUDA_VISIBLE_DEVICES; check the device allocation")
    selected = matches[0]
    if not _is_supported_gpu(selected):
        raise RuntimeError("The selected GPU is not an SM 8.0 GPU")
    return selected


def configured_gpu() -> GPU:
    """Resolve user configuration and preserve the process's inherited device limits."""
    gpu = select_gpu(list_gpus(), os.environ.get("ISAAC_GPU", "auto"), os.environ.get("CUDA_VISIBLE_DEVICES"))
    expected = os.environ.get("A100SIM_EXPECTED_GPU_UUID")
    if expected and gpu.uuid != expected:
        raise RuntimeError("Selected GPU does not match A100SIM_EXPECTED_GPU_UUID")
    return gpu


if __name__ == "__main__":
    print(configured_gpu().uuid)
