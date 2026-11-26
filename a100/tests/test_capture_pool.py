"""Exercise FIFO snapshot isolation, bounded admission and durable worker failure."""
import json
import os
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

    def configure_llvm_pool(self):
        self.state.jobs.maxsize = 64
        with patch("a100sim.scenes.capture_pool.resource_budget", return_value=(16, 8)), patch.dict(
            os.environ, {"A100SIM_RENDER_VARIANT": "llvm_ad_rgb", "A100SIM_CAPTURE_BURST_THREADS": "32"}
        ):
            self.state.captures = CapturePool(self.state)
        self.addCleanup(self.state.captures.executor.shutdown)
        return self.state.captures

    def test_complete_waiting_batch_shares_budget_equally(self):
        pool = self.configure_llvm_pool()
        pool.pending.update(range(16))
        for job_id in range(16):
            pool.pending.remove(job_id)
            self.assertEqual(pool.allocate(job_id), 8)
        self.assertEqual(sum(pool.allocations.values()), 128)

    def test_late_arrivals_start_once_and_release_all_allocations(self):
        pool = self.configure_llvm_pool()
        started, release = queue.Queue(), threading.Event()

        def render(snapshot, directory, threads):
            started.put((directory.name, threads))
            if not release.wait(15):
                raise TimeoutError("test capture timed out")
            return {"threads": threads}

        with patch("a100sim.scenes.capture_pool.render_snapshot", side_effect=render) as worker:
            jobs, assignments = [], []
            try:
                for _ in range(16):
                    job = self.capture()
                    reported_id, count = started.get(timeout=3)
                    self.assertEqual(reported_id, job)
                    jobs.append(job)
                    assignments.append(count)
                self.assertEqual(assignments, [32, 12] + [6] * 14)
                self.assertEqual(self.state.health()["capture_allocated_threads"], 128)
                self.assertEqual(self.state.health()["capture_running"], 16)
            finally:
                release.set()
            self.state.jobs.join()
            self.assertEqual(worker.call_count, len(set(jobs)))
            self.assertEqual([self.state.status(j)["capture_threads"] for j in jobs], assignments)
            self.assertFalse(pool.allocations)
            next_job = self.capture()
            self.state.jobs.join()
            self.assertEqual(self.state.status(next_job)["capture_threads"], 32)

    def test_worker_failure_returns_slot_and_keeps_session(self):
        with patch("a100sim.scenes.capture_pool.render_snapshot", side_effect=ValueError("bad camera")):
            failed = self.capture()
            self.state.jobs.join()
        self.assertEqual(self.state.status(failed)["error"], "bad camera")
        self.assertEqual(self.state.health()["failed"], 1)
        self.assertFalse(self.state.captures.allocations)
        self.assertFalse(list(self.home.glob(".capture-*")))
        self.assertEqual((self.home / "stage.usdc").read_text(), "1")
