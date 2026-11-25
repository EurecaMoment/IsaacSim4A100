"""Exercise FIFO snapshot isolation, bounded admission and durable worker failure."""
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from a100sim.scenes.capture_pool import CapturePool
from a100sim.scenes.service import SceneQueue
from a100sim.service_queue import persist


class CapturePoolTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.identity = "a" * 32
        self.home = root / "sessions" / self.identity
        self.home.mkdir(parents=True)
        self.update(1)
        self.state = SceneQueue(root / "jobs", 2)
        self.state.sessions = SimpleNamespace(root=self.home.parent)
        with patch("a100sim.scenes.capture_pool.resource_budget", return_value=(1, 2)):
            self.state.captures = CapturePool(self.state)
        self.state.ready = True
        self.addCleanup(self.state.captures.executor.shutdown)

    def update(self, tick):
        (self.home / "stage.usdc").write_text(str(tick))
        persist(self.home / "session.json", {"session_id": self.identity, "state": "open",
                                            "physics_tick": tick, "observation": {"bodies": []}})

    def capture(self):
        item = self.state.submit({"operation": "scene.capture", "session_id": self.identity})
        self.state.run_one(self.state.jobs.get())
        return item["id"]

    def test_fifo_capture_and_pending_limit(self):
        started, release = threading.Event(), threading.Event()

        def render(snapshot, directory, threads):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test capture timed out")
            meta = json.loads((directory / "capture-session.json").read_text())
            self.assertEqual(int(snapshot.read_text()), meta["physics_tick"])
            return {"tick": meta["physics_tick"]}

        with patch("a100sim.scenes.capture_pool.render_snapshot", side_effect=render):
            first = self.capture()
            self.assertTrue(started.wait(2))
            self.update(2)
            second = self.capture()
            self.update(3)
            third = self.capture()
            try:
                self.assertEqual(self.state.status(second)["state"], "queued")
                self.assertEqual(self.state.health()["queue_depth"], 2)
                with self.assertRaises(queue.Full):
                    self.state.submit({"operation": "scene.inspect"})
            finally:
                release.set()
            self.state.jobs.join()
        self.assertEqual([self.state.status(i)["result"]["tick"] for i in (first, second, third)], [1, 2, 3])
        self.assertEqual(self.state.health()["active_jobs"], [])
        self.assertFalse(list(self.home.glob(".capture-*")))

    def test_worker_failure_returns_slot_and_keeps_session(self):
        with patch("a100sim.scenes.capture_pool.render_snapshot", side_effect=ValueError("bad camera")):
            failed = self.capture()
            self.state.jobs.join()
        self.assertEqual(self.state.status(failed)["error"], "bad camera")
        self.assertEqual(self.state.health()["failed"], 1)
        self.assertFalse(list(self.home.glob(".capture-*")))
        self.assertEqual((self.home / "stage.usdc").read_text(), "1")
