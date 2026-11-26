"""Bounded render admission, snapshot ownership, and durable capture receipts."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from a100sim.scenes.capture_worker import render_snapshot
from a100sim.service_queue import persist


def resource_budget():
    cpus = len(os.sched_getaffinity(0))
    reserved = min(8, cpus // 4)
    threads = min(max(1, cpus - reserved), max(1, int(os.getenv("A100SIM_CAPTURE_THREADS", "8"))))
    available = (
        int(
            next(
                line.split()[1]
                for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith("MemAvailable:")
            )
        )
        * 1024
    )
    # Allow 16 GiB per process for mesh expansion, textures and frame buffers.
    # Keep at least one fourth of available memory for Kit and other processes.
    workers = min(
        16,
        max(1, int(os.getenv("A100SIM_CAPTURE_WORKERS", "16"))),
        max(1, (cpus - reserved) // threads),
        max(1, int(0.75 * available) // (16 << 30)),
    )
    if os.getenv("A100SIM_RENDER_VARIANT", "scalar_rgb") == "cuda_ad_rgb":
        workers = 1  # All children inherit this Kit service's single visible GPU.
    return workers, threads


class CapturePool:
    def __init__(self, state):
        self.state = state
        self.workers, self.threads = resource_budget()
        self.thread_budget = self.workers * self.threads
        peak = (
            int(os.getenv("A100SIM_CAPTURE_BURST_THREADS", "32"))
            if os.getenv("A100SIM_RENDER_VARIANT") == "llvm_ad_rgb"
            else self.threads
        )
        self.peak_threads = min(max(self.threads, peak), self.thread_budget - self.workers + 1)
        self.min_threads = (
            (self.thread_budget - self.peak_threads) // (self.workers - 1) if self.workers > 1 else self.threads
        )
        self.allocations = {}
        self.executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="capture-supervisor")
        self.pending = set()
        self.running = set()

    def allocate(self, job_id):
        """Assign a fixed Dr.Jit pool for this snapshot, under the queue lock."""
        free = self.thread_budget - sum(self.allocations.values())
        unused_slots = self.workers - len(self.allocations) - 1
        # Keep even a late arrival runnable. When a batch is already waiting,
        # divide the free threads across its unstarted captures.
        waiting = min(unused_slots + 1, len(self.pending) + 1)
        share = free // waiting
        threads = min(self.peak_threads, share, free - unused_slots * self.min_threads)
        self.allocations[job_id] = threads
        return threads

    def dispatch(self, job_id, payload):
        state = self.state
        directory = state.output / job_id
        snapshot = None
        try:
            identity = payload.get("session_id")
            if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity):
                raise ValueError("A retained session_id is required")
            home = state.sessions.root / identity
            snapshot = home / f".capture-{job_id}.usdc"
            metadata_path = directory / "capture-session.json"
            if not (snapshot.is_file() and metadata_path.is_file()):
                metadata = json.loads((home / "session.json").read_text())
                if metadata["state"] != "open":
                    raise ValueError("The retained scene session is closed")
                if metadata["observation"] is None:
                    raise ValueError("Settle the scene before capture")
                # Only the Kit owner writes stages. Copy between mutations since
                # USD can overwrite an inode. Preserve the asset resolver anchor.
                # On an explicitly resumed job the existing pair pins its revision.
                shutil.copyfile(home / "stage.usdc", snapshot)
                persist(metadata_path, metadata)
            with state.lock:
                self.pending.add(job_id)
            self.executor.submit(self.run, job_id, snapshot)
        except Exception as exc:
            self.finish(job_id, snapshot, None, exc)

    def run(self, job_id, snapshot):
        state = self.state
        result, error = None, None
        try:
            with state.lock:
                self.pending.discard(job_id)
                self.running.add(job_id)
                threads = self.allocate(job_id)
                record = state.status(job_id)
                now = time.time()
                record.update(
                    state="running", started_at=now, queue_seconds=now - record["queued_at"], capture_threads=threads
                )
                persist(state.output / job_id / "job.json", record)
            result = render_snapshot(snapshot, state.output / job_id, threads)
        except Exception as exc:
            error = exc
        finally:
            self.finish(job_id, snapshot, result, error)

    def finish(self, job_id, snapshot, result, error):
        state = self.state
        try:
            if snapshot is not None:
                snapshot.unlink(missing_ok=True)
        finally:
            with state.changed:
                self.allocations.pop(job_id, None)
                now = time.time()
                record = state.status(job_id)
                if error is None:
                    record.update(state="succeeded", result=result)
                    state.completed += 1
                else:
                    record.update(state="failed", error=str(error)[:1000])
                    state.failed += 1
                record.update(finished_at=now, execution_seconds=now - record.get("started_at", now))
                persist(state.output / job_id / "job.json", record)
                self.pending.discard(job_id)
                self.running.discard(job_id)
                state.changed.notify_all()
                state.jobs.task_done()
