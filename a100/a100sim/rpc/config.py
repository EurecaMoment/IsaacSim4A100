"""Read portable gateway settings without importing Kit or CUDA libraries."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from a100sim.network import require_worker_url


@dataclass(frozen=True)
class Settings:
    """Paths and bounds owned by one gateway deployment."""

    state_root: Path
    asset_root: Path
    artifact_roots: tuple[Path, ...]
    backend_urls: dict[str, str]
    code_root: Path
    preview_url: str = ""
    listen: str = "127.0.0.1:50051"
    capacity: int = 64
    workers: int = 32
    retained_jobs: int = 4096
    max_upload_bytes: int = 512 * 1024 * 1024
    token: str = ""
    enable_workflows: bool = False
    retention_seconds: int = 7 * 86400
    min_free_bytes: int = 2 * 1024**3

    @classmethod
    def from_environment(cls) -> Settings:
        """Resolve defaults from the same environment as the Bash launchers."""
        code = Path(__file__).resolve().parents[2]
        runtime = Path(os.environ.get("A100SIM_ROOT") or code).resolve()
        state = Path(os.environ.get("A100SIM_STATE_ROOT") or runtime / "state").resolve() / "grpc"
        default_local = (
            Path("/var/tmp") / f"a100sim-{os.getuid()}" if hasattr(os, "getuid") else runtime / "state/local"
        )
        local = Path(os.environ.get("A100SIM_LOCAL_STATE") or default_local).resolve()
        features = Path(os.environ.get("A100SIM_FEATURES_ROOT") or runtime / "features").resolve()
        ports = {
            "resident": ("RESIDENT", 8321),
            "generic": ("GENERIC", 18323),
            "features": ("FEATURE", 8322),
            "render": ("RENDER", 8320),
        }
        urls = {
            name: os.environ.get(f"A100SIM_GRPC_{name.upper()}_URL")
            or f"http://127.0.0.1:{os.environ.get(f'A100SIM_{key}_PORT') or port}"
            for name, (key, port) in ports.items()
        }
        return cls(
            state_root=state,
            asset_root=Path(os.environ.get("A100SIM_ASSET_ROOT") or runtime / "assets").resolve(),
            artifact_roots=(runtime / "results", local / "jobs", features / "reports", state / "workflows"),
            backend_urls=urls,
            code_root=code,
            preview_url=os.environ.get("A100SIM_PREVIEW_URL", "").strip(),
            listen=os.environ.get("A100SIM_GRPC_LISTEN") or "127.0.0.1:50051",
            capacity=int(os.environ.get("A100SIM_GRPC_CAPACITY") or 64),
            workers=int(os.environ.get("A100SIM_GRPC_WORKERS") or 32),
            max_upload_bytes=int(os.environ.get("A100SIM_GRPC_MAX_UPLOAD_BYTES") or cls.max_upload_bytes),
            token=os.environ.get("A100SIM_GRPC_TOKEN") or "",
            enable_workflows=os.environ.get("A100SIM_GRPC_WORKFLOWS", "0") == "1",
            retention_seconds=int(os.environ.get("GRPC_RETENTION_SECONDS", 7 * 86400)),
            min_free_bytes=int(os.environ.get("GRPC_MIN_FREE_BYTES", 2 * 1024**3)),
        )

    def validate(self) -> None:
        """Reject settings that cannot provide bounded execution."""
        if not 1 <= self.capacity <= 4096 or not 1 <= self.workers <= 128:
            raise ValueError("Gateway capacity must be 1–4096 and workers must be 1–128")
        if self.retained_jobs < self.capacity + self.workers:
            raise ValueError("Retained job limit must cover queued and running work")
        if self.max_upload_bytes < 1:
            raise ValueError("Maximum asset upload size must be positive")
        if self.retention_seconds < 1 or self.min_free_bytes < 0:
            raise ValueError("Retention must be positive and minimum free space cannot be negative")
        for value in self.backend_urls.values():
            require_worker_url(value)
        if self.preview_url:
            require_worker_url(self.preview_url)
