"""Run scene sessions on Kit's main thread behind a bounded loopback queue."""

from __future__ import annotations

import argparse
import json
import logging
import queue
from pathlib import Path
from typing import TYPE_CHECKING, Any

from a100sim.network import require_loopback
from a100sim.scenes.capture_pool import CapturePool
from a100sim.scenes.contracts import OPERATIONS
from a100sim.service_queue import JobQueue, start_http

if TYPE_CHECKING:
    from a100sim.scenes.sessions import Sessions

LOGGER = logging.getLogger(__name__)


class SceneQueue(JobQueue):
    """Serialize Kit mutations and dispatch immutable captures to bounded workers."""

    sessions: Sessions

    captures = None

    def submit(self, value):
        with self.lock:
            if self.captures and self.jobs.qsize() + len(self.captures.pending) >= self.jobs.maxsize:
                raise queue.Full()
            return super().submit(value)

    def run_one(self, item):
        if self.captures and item[1]["operation"] == "scene.capture":
            self.captures.dispatch(*item)
        else:
            super().run_one(item)

    def health(self):
        with self.lock:
            health = super().health()
            if self.captures:
                active = sorted(self.captures.running)
                if self.active:
                    active.append(self.active)
                health.update(
                    active_jobs=active,
                    active_job=self.active or next(iter(active), None),
                    queue_depth=self.jobs.qsize() + len(self.captures.pending),
                    capture_workers=self.captures.workers,
                    capture_threads=self.captures.threads,
                    capture_running=len(self.captures.running),
                    capture_pending=len(self.captures.pending),
                )
            return health

    def prepare(self, value: dict[str, Any]) -> dict[str, Any]:
        """Reject unregistered actions before accepting a worker job."""
        if value.get("operation") not in OPERATIONS:
            raise ValueError("Unknown scene operation")
        return value

    def process(self, payload: dict[str, Any], directory: Path) -> dict[str, Any]:
        """Execute one retained scene action on Kit's main thread."""
        try:
            return {"result": self.sessions.execute(payload, directory)}
        except Exception:
            LOGGER.exception("Scene operation failed")
            raise


def main() -> None:
    """Start Kit before importing the scene implementation and accepting work."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host", type=require_loopback, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18324)
    args = parser.parse_args()
    state = SceneQueue(args.output / "jobs", 64, "Isaac Sim 6.2 retained CUDA scenes")
    server = start_http(state, args.host, args.port, "/scene")
    from a100sim.app import create_app

    app = create_app()
    try:
        from a100sim.scenes.sessions import Sessions

        state.sessions = Sessions(args.output / "sessions")
        state.captures = CapturePool(state)
        state.details = {
            "operations": sorted(OPERATIONS),
            "max_cameras": 32,
            "session_storage": "retained flattened USD and measured CUDA state",
        }
        state.ready = True
        print("SCENE_SERVICE_READY", json.dumps(state.health()), flush=True)
        state.work_forever()
    finally:
        state.ready = False
        server.shutdown()
        if state.captures:
            state.captures.executor.shutdown(wait=True)
        app.close()


if __name__ == "__main__":
    main()
