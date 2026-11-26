"""Run scene sessions on Kit's main thread behind a bounded loopback queue."""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import queue
from pathlib import Path
from typing import Any

from a100sim.network import require_loopback
from a100sim.scenes.capture_pool import CapturePool
from a100sim.scenes.contracts import OPERATIONS
from a100sim.service_queue import JobQueue, start_http

LOGGER = logging.getLogger(__name__)


def serve_sessions(connection, root):
    """Own Kit and its mutable USD stages on the child process's main thread."""
    from a100sim.app import create_app

    app = create_app()
    try:
        from a100sim.scenes.sessions import Sessions

        sessions = Sessions(root)
        connection.send((True, None))
        while True:
            try:
                payload, directory = connection.recv()
            except EOFError:
                break
            try:
                result = sessions.execute(payload, directory)
            except Exception as error:
                LOGGER.exception("Kit scene operation failed")
                connection.send((False, str(error)))
            else:
                connection.send((True, result))
    finally:
        connection.close()
        app.close()


class KitSessions:
    """Keep the loopback HTTP queue responsive while native scene calls hold the GIL."""

    def __init__(self, root, target=serve_sessions):
        self.root = root
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(target=target, args=(child, root), name="isaac-kit")
        self.process.start()
        child.close()

    def receive(self):
        try:
            ok, value = self.connection.recv()
        except EOFError as error:
            raise RuntimeError("Kit scene process exited") from error
        if not ok:
            raise ValueError(value)
        return value

    def execute(self, payload, directory):
        self.connection.send((payload, directory))
        return self.receive()

    def close(self):
        self.connection.close()
        self.process.join(5)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(5)


class SceneQueue(JobQueue):
    """Serialize Kit mutations and dispatch immutable captures to bounded workers."""

    sessions: KitSessions

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
    sessions = KitSessions(args.output / "sessions")
    try:
        sessions.receive()
        state.sessions = sessions
        state.captures = CapturePool(state)
        state.details = {
            "operations": sorted(OPERATIONS),
            "max_cameras": 32,
            "session_storage": "retained flattened USD and measured CUDA state",
        }
        state.ready = True
        print("SCENE_SERVICE_READY", json.dumps(state.health()), flush=True)
        while sessions.process.is_alive():
            try:
                item = state.jobs.get(timeout=1)
            except queue.Empty:
                continue
            state.run_one(item)
        raise RuntimeError("Kit scene process exited")
    finally:
        state.ready = False
        server.shutdown()
        if state.captures:
            state.captures.executor.shutdown(wait=True)
        sessions.close()


if __name__ == "__main__":
    main()
