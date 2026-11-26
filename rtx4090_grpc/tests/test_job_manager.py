#!/usr/bin/env python3
"""JobManager 单元测试：用假队列驱动，不加载 Isaac Sim。

运行：
    .venv/bin/python tests/test_job_manager.py
"""

from __future__ import annotations

import hashlib
import json
import queue as queue_module
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import job_manager  # noqa: E402
from server.config import Settings  # noqa: E402

JOB_ID = "a" * 32


def _persist(path: Path, value: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value), encoding="utf-8")
    tmp.replace(path)


class FakeQueue:
    """模仿上游 JobQueue 的最小接口（磁盘持久化 + 有界队列）。"""

    def __init__(self, output: Path, capacity: int = 64) -> None:
        self.output = output.resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = queue_module.Queue(maxsize=capacity)
        self.ready = True
        self.active = None
        self.completed = 0
        self.failed = 0
        self.started = time.time()

    def health(self) -> dict:
        return {"status": "ready", "engine": "fake", "queue_depth": self.jobs.qsize(),
                "queue_capacity": self.jobs.maxsize, "completed": self.completed, "failed": self.failed}

    def status(self, job_id: str) -> dict | None:
        path = self.output / job_id / "job.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def submit(self, payload: dict) -> dict:
        if not self.ready:
            raise RuntimeError("GPU service is initializing")
        if payload.get("_bad_shape"):
            raise ValueError("shape must be cube, sphere or mesh")
        if payload.get("_full"):
            raise queue_module.Full()
        job_id = uuid.uuid4().hex
        directory = self.output / job_id
        with self.lock:
            if self.jobs.full():
                raise queue_module.Full()
            directory.mkdir()
            _persist(directory / "request.json", payload)
            record = {"id": job_id, "state": "queued", "queued_at": time.time()}
            _persist(directory / "job.json", record)
            self.jobs.put_nowait((job_id, payload))
        return record.copy()


def _make_settings(tmp: Path, **overrides) -> Settings:
    base = dict(retention_seconds=0, max_download_streams=2,
                download_chunk_bytes=4, download_chunk_max=8,
                min_free_bytes=0, state_root=tmp)
    base.update(overrides)
    return Settings(**base)


def _run_job(mgr: job_manager.JobManager, job_id: str, files: dict[str, bytes]) -> None:
    """模拟引擎跑完一个任务：写文件 + 标记 succeeded + 触发 finalize。"""
    job_dir = mgr.output / job_id
    for rel, data in files.items():
        path = job_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    record = mgr._read_record(job_id)
    record.update(state="succeeded", finished_at=time.time(),
                  started_at=time.time() - 1, execution_seconds=1.0,
                  phase="writing_artifacts", frames_done=2, frames_total=2)
    mgr._write_record(job_id, record)
    mgr._finalize(job_id)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        settings = _make_settings(tmp_path)
        q = FakeQueue(settings.output)
        mgr = job_manager.JobManager(q, settings)

        # ---- 1. 幂等提交 ----
        payload = {"steps": 10, "bodies": [{"id": "c"}]}
        rec1 = mgr.submit("req-1", payload)
        job_id = rec1["id"]
        rec2 = mgr.submit("req-1", payload)  # 同 request_id + 同参数
        assert rec2["id"] == job_id, "幂等命中应返回同一 job_id"

        try:
            mgr.submit("req-1", {"steps": 99})
            raise AssertionError("同 request_id 不同参数应抛 IdempotencyConflict")
        except job_manager.IdempotencyConflict as exc:
            assert exc.job_id == job_id

        try:
            mgr.submit("", payload)
            raise AssertionError("空 request_id 应抛 ValueError")
        except ValueError:
            pass

        # 不同 request_id 产生新任务
        rec3 = mgr.submit("req-2", payload)
        assert rec3["id"] != job_id
        assert len(mgr._request_ids) == 2

        # ---- 2. 产物清单 ----
        files = {
            "state.npz": b"STATE" * 10,
            "scene.usdc": b"SCENE",
            "simulation.json": b"{}",
            "render.png": b"PNGDATA",
            "frames/frame_000001/render.png": b"F1",
            "frames/frame_000001/state.npz": b"F1STATE",
        }
        _run_job(mgr, job_id, files)
        record = mgr.status(job_id)
        manifest = record.get("artifact_manifest") or []
        rels = {a["relative_path"] for a in manifest}
        # job.json / request.json 不应出现在清单
        assert f"{job_id}/job.json" not in rels
        assert f"{job_id}/request.json" not in rels
        # 所有写入文件都在清单，且 size/sha256 正确
        by_rel = {a["relative_path"]: a for a in manifest}
        for rel, data in files.items():
            full = f"{job_id}/{rel}"
            assert full in by_rel, f"缺少产物 {full}"
            a = by_rel[full]
            assert a["size_bytes"] == len(data), (a["size_bytes"], len(data))
            assert a["sha256"] == _sha(data), a["sha256"]
            assert a["artifact_id"], "artifact_id 为空"
        # media_type 推断
        assert by_rel[f"{job_id}/render.png"]["media_type"] == "image/png"
        assert by_rel[f"{job_id}/state.npz"]["media_type"] == "application/x-npz"
        # artifact_id 在任务内唯一
        ids = [a["artifact_id"] for a in manifest]
        assert len(ids) == len(set(ids))

        # ---- 3. 下载：全量 + 续传 ----
        target = by_rel[f"{job_id}/render.png"]
        aid = target["artifact_id"]
        # 全量
        handle = mgr.download_open(job_id, aid, 0, 0)
        data = b"".join(chunk for _, chunk in mgr.stream_chunks(handle))
        assert data == files["render.png"], data
        # 续传 offset=4
        handle = mgr.download_open(job_id, aid, 4, 2)
        chunks = list(mgr.stream_chunks(handle))
        assert chunks[0][0] == 4
        joined = b"".join(c for _, c in chunks)
        assert joined == files["render.png"][4:], joined
        # offset == size → 空流
        handle = mgr.download_open(job_id, aid, len(files["render.png"]), 0)
        assert list(mgr.stream_chunks(handle)) == []
        # offset > size → OUT_OF_RANGE
        try:
            mgr.download_open(job_id, aid, 9999, 0)
            raise AssertionError("应抛 OutOfRange")
        except job_manager.OutOfRange:
            pass

        # 并发上限
        settings2 = _make_settings(tmp_path, max_download_streams=1)
        q2 = FakeQueue(settings2.output)
        mgr2 = job_manager.JobManager(q2, settings2)
        r = mgr2.submit("req-x", payload)
        _run_job(mgr2, r["id"], {"state.npz": b"X" * 100})
        a = (mgr2.status(r["id"])["artifact_manifest"])[0]["artifact_id"]
        h1 = mgr2.download_open(r["id"], a, 0, 0)
        try:
            mgr2.download_open(r["id"], a, 0, 0)
            raise AssertionError("应抛 TooManyStreams")
        except job_manager.TooManyStreams:
            pass
        list(mgr2.stream_chunks(h1))  # 释放槽位
        h2 = mgr2.download_open(r["id"], a, 0, 0)  # 现在应成功
        list(mgr2.stream_chunks(h2))

        # ---- 4. 下载错误码路径 ----
        # artifact 不属于该 job
        try:
            mgr.download_open(job_id, "deadbeefdeadbeef", 0, 0)
            raise AssertionError("应抛 ArtifactNotFound")
        except job_manager.ArtifactNotFound:
            pass
        # 不存在的 job
        try:
            mgr.download_open("b" * 32, aid, 0, 0)
            raise AssertionError("应抛 ArtifactNotFound")
        except job_manager.ArtifactNotFound:
            pass
        # 未成功的 job 不可下载
        queued = mgr.submit("req-queued", payload)
        try:
            mgr.download_open(queued["id"], aid, 0, 0)
            raise AssertionError("应抛 JobNotReady")
        except job_manager.JobNotReady:
            pass

        # ---- 5. Ack 落盘确认（不立即删产物，统一由保留期线程清理）----
        before = mgr.status(job_id)
        size_on_disk = sum(a["size_bytes"] for a in before["artifact_manifest"])
        result = mgr.ack(job_id)
        # 新语义：Ack 只标记 acked=True，不立即释放
        assert result["released"] is False
        assert result["freed_bytes"] == 0
        # 文件仍在
        assert (mgr.output / job_id / "render.png").is_file()
        # job.json 保留，GetJob 仍可查
        after = mgr.status(job_id)
        assert after is not None
        assert after["acked"] is True
        # 重复 Ack 幂等
        again = mgr.ack(job_id)
        assert again["released"] is False and again["freed_bytes"] == 0
        # Ack 后下载仍可用（保留期内）
        ctx = mgr.download_open(job_id, aid, 0, 0)
        chunks = list(mgr.stream_chunks(ctx))
        assert sum(len(c) for _, c in chunks) > 0

        # ---- 6. 保留期线程到期清理 ----
        # 手动把 finished_at 改到很久以前，触发清理
        with mgr.lock:
            rec = mgr._read_record(job_id)
            rec["finished_at"] = time.time() - mgr.settings.retention_seconds - 60
            mgr._write_record(job_id, rec)
        mgr._sweep_retention()
        # 文件已删除
        assert not (mgr.output / job_id / "render.png").is_file()
        # job.json 仍在
        assert mgr.status(job_id) is not None
        # 下载转 ArtifactReleased
        try:
            mgr.download_open(job_id, aid, 0, 0)
            raise AssertionError("应抛 ArtifactReleased")
        except job_manager.ArtifactReleased:
            pass

        # ---- 6. 取消排队任务 ----
        canc = mgr.submit("req-cancel", payload)
        assert q.jobs.qsize() >= 1
        out = mgr.cancel(canc["id"])
        assert out["state"] == "interrupted", out["state"]
        # 已从队列移除（其它任务可能还在，但被取消的不再出现）
        remaining = []
        while not q.jobs.empty():
            remaining.append(q.jobs.get_nowait()[0])
        assert canc["id"] not in remaining
        # 终态取消幂等
        again = mgr.cancel(canc["id"])
        assert again["state"] == "interrupted"
        # 不存在的 job
        assert mgr.cancel("c" * 32) is None

        # ---- 7. 保留期清理 ----
        old = mgr.submit("req-old", payload)
        _run_job(mgr, old["id"], {"state.npz": b"OLD"})
        # 把 finished_at 改成很久以前
        rec = mgr._read_record(old["id"])
        rec["finished_at"] = time.time() - 1000
        mgr._write_record(old["id"], rec)
        mgr._sweep_retention()
        rec = mgr._read_record(old["id"])
        assert rec.get("artifacts_released") is True
        assert not (mgr.output / old["id"] / "state.npz").is_file()
        # 已 Ack 的不重复处理
        assert mgr.ack(old["id"])["freed_bytes"] == 0

        # ---- 8. WatchJob 推送 ----
        watch = mgr.submit("req-watch", payload)
        seen = []

        def collect():
            active = [True]
            for rec in mgr.watch_iter(watch["id"], lambda: active[0]):
                seen.append(rec["state"])
                if rec["state"] in {"succeeded", "failed", "interrupted"}:
                    active[0] = False

        t = threading.Thread(target=collect)
        t.start()
        time.sleep(0.2)
        _run_job(mgr, watch["id"], {"state.npz": b"W"})
        t.join(timeout=5)
        assert "succeeded" in seen, seen

        # ---- 9. 磁盘水位拒绝 ----
        settings3 = _make_settings(tmp_path, min_free_bytes=10 ** 18)
        q3 = FakeQueue(settings3.output)
        mgr3 = job_manager.JobManager(q3, settings3)
        try:
            mgr3.submit("req-full", payload)
            raise AssertionError("应抛 DiskFull")
        except job_manager.DiskFull:
            pass

        # ---- 10. 进度在运行中可见（status 合并内存进度）----
        prog = mgr.submit("req-prog", payload)
        mgr._begin(prog["id"], payload)
        mgr.update_progress(prog["id"], phase="simulating", frames_done=3, frames_total=10)
        live = mgr.status(prog["id"])
        assert live["phase"] == "simulating" and live["frames_done"] == 3 and live["frames_total"] == 10
        mgr._end(prog["id"])

    print("PASS: JobManager 单元测试通过（幂等 / 清单 / 下载 / Ack / 取消 / 保留期 / Watch）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
