"""JobManager：在不改动上游队列的前提下，补齐 gRPC 服务端能力。

在 RtxQueueWithFrameCapture 之上叠加：
  * request_id 幂等提交
  * 运行中 / 排队中任务取消
  * 进度字段（phase / frames_done / frames_total）
  * 产物清单（artifact_id / relative_path / size_bytes / sha256 / media_type）
  * 流式下载（支持 offset 续传 + 单 job 并发上限）
  * AckJob 落盘确认与磁盘回收
  * 保留期清理
  * WatchJob 服务端推送

所有持久化状态写入每个任务目录下的 job.json（与上游共用同一文件），
上游 a100sim / rtx4090 代码零改动。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue as queue_module
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from .config import Settings

# 不参与下载的元数据文件
_META_FILES = {"job.json", "request.json"}
# 终态
_TERMINAL = {"succeeded", "failed", "interrupted"}
# 允许下载的状态（含取消后已写出部分产物）
_DOWNLOADABLE = {"succeeded", "interrupted"}


# ---------------------------------------------------------------------------
# 异常类型（servicer 负责映射到 gRPC 状态码）
# ---------------------------------------------------------------------------

class JobManagerError(Exception):
    """基类。"""


class IdempotencyConflict(JobManagerError):
    """同 request_id 但参数不同。"""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"request_id 已存在但参数不同: job_id={job_id}")
        self.job_id = job_id


class DiskFull(JobManagerError):
    """磁盘水位低于阈值。"""


class ArtifactNotFound(JobManagerError):
    pass


class ArtifactReleased(JobManagerError):
    pass


class JobNotReady(JobManagerError):
    pass


class OutOfRange(JobManagerError):
    pass


class TooManyStreams(JobManagerError):
    pass


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _persist(path: Path, value: dict[str, Any]) -> None:
    """原子写 JSON（tmp + replace）。"""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _media_type(suffix: str) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".npy": "application/x-npy",
        ".npz": "application/x-npz",
        ".json": "application/json",
        ".usdc": "model/vnd.usdc",
        ".usd": "model/vnd.usd",
        ".ply": "application/x-ply",
    }.get(suffix.lower(), "application/octet-stream")


def _artifact_id(relative_path: str) -> str:
    """任务内稳定唯一 id（由相对路径派生）。"""
    return hashlib.sha1(relative_path.encode("utf-8")).hexdigest()[:16]


def _payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# JobManager
# ---------------------------------------------------------------------------

class JobManager:
    def __init__(self, queue: Any, settings: Settings) -> None:
        self.queue = queue
        self.settings = settings
        self.output: Path = queue.output
        # 复用队列的可重入锁，保证与上游 run_one 串行
        self.lock = queue.lock

        # request_id -> job_id（启动时从磁盘重建）
        self._request_ids: dict[str, str] = {}
        # job_id -> cancel event
        self._cancel: dict[str, threading.Event] = {}
        # job_id -> {phase, frames_done, frames_total}
        self._progress: dict[str, dict[str, Any]] = {}
        # job_id -> 正在进行的下载流数
        self._downloads: dict[str, int] = {}
        # job_id -> 上次进度落盘时间戳（节流）
        self._progress_flush: dict[str, float] = {}
        # WatchJob 通知
        self._watch_cond = threading.Condition(self.lock)

        queue.manager = self  # 让 process() 能回调进度 / 取消

        self._rebuild_index()

    # ------------------------------------------------------------------
    # 启动期：从磁盘重建 request_id 索引
    # ------------------------------------------------------------------

    def _rebuild_index(self) -> None:
        for path in self.output.glob("*/job.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            request_id = record.get("request_id")
            if request_id:
                self._request_ids[request_id] = record.get("id") or path.parent.name

    # ------------------------------------------------------------------
    # 记录读写
    # ------------------------------------------------------------------

    def _job_dir(self, job_id: str) -> Path:
        return self.output / job_id

    def _job_json(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "job.json"

    def _read_record(self, job_id: str) -> dict[str, Any] | None:
        path = self._job_json(job_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write_record(self, job_id: str, record: dict[str, Any]) -> None:
        _persist(self._job_json(job_id), record)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        record = self.queue.health()
        try:
            free = shutil.disk_usage(self.output).free
        except OSError:
            free = 0
        record.update(
            api_version=self.settings.api_version,
            artifacts_root=str(self.output),
            artifacts_free_bytes=free,
            retention_seconds=self.settings.retention_seconds,
        )
        return record

    # ------------------------------------------------------------------
    # 提交（幂等）
    # ------------------------------------------------------------------

    def submit(self, request_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = (request_id or "").strip()
        if not request_id:
            raise ValueError("request_id 不能为空")
        digest = _payload_digest(payload)

        with self.lock:
            existing = self._request_ids.get(request_id)
            if existing is not None:
                record = self._read_record(existing)
                if record is not None and record.get("payload_digest") == digest:
                    # 幂等命中：返回原任务
                    return record
                raise IdempotencyConflict(existing)

            # 磁盘水位保护
            try:
                free = shutil.disk_usage(self.output).free
            except OSError:
                free = 0
            if free < self.settings.min_free_bytes:
                raise DiskFull(
                    f"磁盘剩余 {free} 字节，低于阈值 {self.settings.min_free_bytes}"
                )

            record = self.queue.submit(payload)  # 可能抛 queue.Full / RuntimeError / ValueError
            job_id = record["id"]

            # 把 request_id / 摘要 / 初始 phase 写入 job.json
            full = self._read_record(job_id) or record
            full.update(request_id=request_id, payload_digest=digest, phase="queued")
            self._write_record(job_id, full)
            self._request_ids[request_id] = job_id
            self._notify_watchers()
            return full

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def status(self, job_id: str) -> dict[str, Any] | None:
        record = self._read_record(job_id)
        if record is None:
            return None
        # 运行中：把内存里的实时进度合并进去（job.json 可能尚未刷盘）
        progress = self._progress.get(job_id)
        if progress:
            record = dict(record)
            record["phase"] = progress.get("phase", record.get("phase", ""))
            record["frames_done"] = progress.get("frames_done", record.get("frames_done", 0))
            record["frames_total"] = progress.get("frames_total", record.get("frames_total", 0))
        return record

    # ------------------------------------------------------------------
    # 运行时钩子（由 frame_capture.process() 回调）
    # ------------------------------------------------------------------

    def _begin(self, job_id: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self._cancel[job_id] = threading.Event()
            self._progress[job_id] = {"phase": "loading_scene", "frames_done": 0, "frames_total": 0}
            self.queue._job_id = job_id  # 让 process() 知道当前 job
            self._notify_watchers()

    def update_progress(self, job_id: str, *, phase: str | None = None,
                        frames_done: int | None = None,
                        frames_total: int | None = None) -> None:
        with self.lock:
            progress = self._progress.setdefault(
                job_id, {"phase": "", "frames_done": 0, "frames_total": 0}
            )
            phase_changed = phase is not None and phase != progress.get("phase")
            if phase is not None:
                progress["phase"] = phase
            if frames_done is not None:
                progress["frames_done"] = frames_done
            if frames_total is not None:
                progress["frames_total"] = frames_total
            # 内存始终最新（status 合并）；磁盘写入节流：phase 变化立即刷，否则最多 0.25s 一次
            now = time.time()
            last = self._progress_flush.get(job_id, 0.0)
            if phase_changed or now - last >= 0.25:
                record = self._read_record(job_id)
                if record is not None:
                    record.update(phase=progress["phase"],
                                  frames_done=progress["frames_done"],
                                  frames_total=progress["frames_total"])
                    self._write_record(job_id, record)
                self._progress_flush[job_id] = now
            self._notify_watchers()

    def is_cancelled(self, job_id: str) -> bool:
        event = self._cancel.get(job_id)
        return bool(event and event.is_set())

    def _merge_progress(self, record: dict[str, Any], job_id: str) -> None:
        """把内存进度覆盖到待落盘的 record（供 run_one 调用）。"""
        progress = self._progress.get(job_id)
        if progress:
            record["phase"] = progress.get("phase", record.get("phase", ""))
            record["frames_done"] = progress.get("frames_done", record.get("frames_done", 0))
            record["frames_total"] = progress.get("frames_total", record.get("frames_total", 0))

    def _notify_watchers(self) -> None:
        # 必须已持有 self.lock
        self._watch_cond.notify_all()

    def _end(self, job_id: str) -> None:
        with self.lock:
            self._progress.pop(job_id, None)
            self._progress_flush.pop(job_id, None)
            self._cancel.pop(job_id, None)
            if getattr(self.queue, "_job_id", None) == job_id:
                self.queue._job_id = None
            self._notify_watchers()

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        record = self._read_record(job_id)
        if record is None:
            return None
        state = record.get("state")
        if state in _TERMINAL:
            return record  # 幂等：终态直接返回
        with self.lock:
            if state == "queued":
                self._remove_from_queue(job_id)
                record = self._read_record(job_id) or record
                record.update(state="interrupted", error="cancelled by client",
                              finished_at=time.time(), phase="interrupted")
                self._write_record(job_id, record)
                self._progress.pop(job_id, None)
            else:  # running
                event = self._cancel.get(job_id)
                if event is not None:
                    event.set()
                record = self.status(job_id)  # 返回实际状态
            self._notify_watchers()
            return record

    def _remove_from_queue(self, job_id: str) -> None:
        """从排队队列里移除指定 job（未被执行的）。"""
        items: list = []
        while True:
            try:
                items.append(self.queue.jobs.get_nowait())
            except queue_module.Empty:
                break
        removed = False
        for item in items:
            if item[0] == job_id:
                removed = True
                self.queue.jobs.task_done()  # 平衡 put 的计数
                continue
            self.queue.jobs.put_nowait(item)
        if not removed:
            # 可能已被引擎取走，忽略
            return

    # ------------------------------------------------------------------
    # 产物清单
    # ------------------------------------------------------------------

    def _compute_manifest(self, job_id: str) -> list[dict[str, Any]]:
        job_dir = self._job_dir(job_id)
        if not job_dir.is_dir():
            return []
        manifest: list[dict[str, Any]] = []
        for path in sorted(job_dir.rglob("*")):
            if not path.is_file() or path.name in _META_FILES:
                continue
            try:
                rel = path.relative_to(self.output)
            except ValueError:
                continue
            manifest.append({
                "artifact_id": _artifact_id(rel.as_posix()),
                "name": path.name,
                "relative_path": rel.as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
                "media_type": _media_type(path.suffix),
            })
        return manifest

    def _finalize(self, job_id: str) -> None:
        """任务结束后：补全最终进度 + 计算产物清单并落盘。"""
        with self.lock:
            record = self._read_record(job_id)
            if record is None:
                return
            self._merge_progress(record, job_id)
            state = record.get("state")
            if state in _DOWNLOADABLE and "artifact_manifest" not in record:
                record["artifact_manifest"] = self._compute_manifest(job_id)
            record["phase"] = record.get("phase") or state or ""
            self._write_record(job_id, record)
            self._notify_watchers()

    def _manifest_index(self, job_id: str) -> dict[str, dict[str, Any]]:
        record = self._read_record(job_id) or {}
        return {a["artifact_id"]: a for a in record.get("artifact_manifest") or []}

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    def download_open(self, job_id: str, artifact_id: str, offset: int,
                      chunk_bytes: int) -> dict[str, Any]:
        """下载前校验，返回 {path, size}；失败抛对应异常。"""
        record = self._read_record(job_id)
        if record is None:
            raise ArtifactNotFound(f"任务不存在: {job_id}")
        state = record.get("state")
        if state not in _DOWNLOADABLE:
            raise JobNotReady(f"任务尚未成功，当前状态: {state}")
        if record.get("artifacts_released"):
            raise ArtifactReleased("artifacts_released: 任务产物已被清理")
        index = self._manifest_index(job_id)
        artifact = index.get(artifact_id)
        if artifact is None:
            raise ArtifactNotFound(f"artifact_id 不属于该任务: {artifact_id}")
        size = int(artifact["size_bytes"])
        if offset > size:
            raise OutOfRange(f"offset {offset} 超过文件大小 {size}")

        # 并发流控制
        with self.lock:
            active = self._downloads.get(job_id, 0)
            if active >= self.settings.max_download_streams:
                raise TooManyStreams(
                    f"任务 {job_id} 并发下载流已达上限 {self.settings.max_download_streams}"
                )
            self._downloads[job_id] = active + 1

        rel = artifact["relative_path"]
        path = (self.output / rel).resolve()
        if not path.is_relative_to(self.output.resolve()) or not path.is_file():
            with self.lock:
                self._release_download_slot(job_id)
            raise ArtifactNotFound(f"产物文件缺失: {rel}")
        return {"path": path, "size": size, "job_id": job_id,
                "offset": int(offset), "chunk_bytes": int(chunk_bytes)}

    def _release_download_slot(self, job_id: str) -> None:
        # 必须已持有 self.lock
        active = self._downloads.get(job_id, 0) - 1
        if active <= 0:
            self._downloads.pop(job_id, None)
        else:
            self._downloads[job_id] = active

    def stream_chunks(self, handle: dict[str, Any]) -> Iterator[tuple[int, bytes]]:
        path: Path = handle["path"]
        size: int = handle["size"]
        offset: int = handle["offset"]
        requested = handle["chunk_bytes"] or self.settings.download_chunk_bytes
        chunk = max(1, min(requested, self.settings.download_chunk_max))
        job_id = handle["job_id"]
        try:
            with path.open("rb") as fh:
                if offset:
                    fh.seek(offset)
                while offset < size:
                    data = fh.read(min(chunk, size - offset))
                    if not data:
                        break
                    yield offset, data
                    offset += len(data)
        finally:
            with self.lock:
                self._release_download_slot(job_id)

    # ------------------------------------------------------------------
    # Ack / 磁盘回收
    # ------------------------------------------------------------------

    def ack(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            record = self._read_record(job_id)
            if record is None:
                raise ArtifactNotFound(f"任务不存在: {job_id}")
            if record.get("artifacts_released"):
                # 幂等：已被保留期清理或之前 Ack 过，未释放任何新字节
                return {"released": True, "freed_bytes": 0}
            # 只标记 acked=True，不立即删产物；统一由保留期线程到期清理
            record.update(acked=True)
            self._write_record(job_id, record)
            self._notify_watchers()
            # freed_bytes=0 表示本次未立即释放，等保留期到期
            return {"released": False, "freed_bytes": 0}

    def _delete_artifacts(self, job_id: str) -> int:
        """删除任务目录下除元数据外的所有文件，返回释放字节数。"""
        job_dir = self._job_dir(job_id)
        if not job_dir.is_dir():
            return 0
        freed = 0
        for path in list(job_dir.rglob("*")):
            if path.is_file() and path.name not in _META_FILES:
                try:
                    freed += path.stat().st_size
                    path.unlink()
                except OSError:
                    pass
        # 清理空子目录
        for path in sorted(job_dir.rglob("*"), reverse=True):
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass
        return freed

    # ------------------------------------------------------------------
    # 保留期清理
    # ------------------------------------------------------------------

    def start_retention(self, stop: threading.Event) -> None:
        thread = threading.Thread(
            target=self._retention_loop, args=(stop,),
            name="grpc-retention", daemon=True,
        )
        thread.start()

    def _retention_loop(self, stop: threading.Event) -> None:
        while not stop.wait(60):
            try:
                self._sweep_retention()
            except Exception as exc:  # noqa: BLE001
                print(f"RETENTION_SWEEP_ERROR: {exc}", flush=True)

    def _sweep_retention(self) -> None:
        now = time.time()
        cutoff = now - self.settings.retention_seconds
        with self.lock:
            for path in self.output.glob("*/job.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if record.get("artifacts_released"):
                    continue
                finished = record.get("finished_at")
                if not finished or finished > cutoff:
                    continue
                # 不管 acked 还是未 acked，到期都清理 —— 统一由保留期兜底
                job_id = record.get("id") or path.parent.name
                freed = self._delete_artifacts(job_id)
                record.update(artifacts_released=True, freed_bytes=freed,
                              retention_released=True)
                _persist(path, record)

    # ------------------------------------------------------------------
    # WatchJob
    # ------------------------------------------------------------------

    def watch_iter(self, job_id: str, is_active) -> Iterator[dict[str, Any]]:
        last_sig = None
        while is_active():
            record = self.status(job_id)
            if record is None:
                return
            sig = (record.get("state"), record.get("phase"),
                   record.get("frames_done"), record.get("frames_total"))
            if sig != last_sig:
                yield record
                last_sig = sig
                if record.get("state") in _TERMINAL:
                    return
            with self._watch_cond:
                self._watch_cond.wait(0.5)
