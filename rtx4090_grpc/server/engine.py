"""作业运行时：直接承载 Isaac Sim 引擎。

gRPC 与 HTTP 平级部署，各自独立：
  * create_app() 来自上游 rtx4090.service（只负责启动 SimulationApp，零业务逻辑）
  * RtxQueueWithFrameCapture 是 gRPC 专属子类（本目录 frame_capture.py），
    继承上游 RtxQueue 的物理引擎 + RTX 渲染后端，仅覆盖两处：
      - prepare_capture() 加 render 字段
      - process() 改成逐帧 step + 逐帧采集 + 逐帧渲染
  * 上游 a100sim/physics_service.py 和 rtx4090/service.py 零改动
"""

from __future__ import annotations

import os
import queue as queue_module
from typing import Any

from .config import Settings


class Engine:
    """创建 SimulationApp，并暴露 JobQueue 语义。"""

    def __init__(self, settings: Settings) -> None:
        if not (settings.isaac_sim_dir / "python.sh").is_file():
            raise FileNotFoundError(
                f"ISAAC_SIM_DIR 无效，未找到 python.sh: {settings.isaac_sim_dir}"
            )

        # 这些环境变量是 rtx4090.service 的输入，必须在导入前设置
        os.environ["ISAAC_SIM_DIR"] = str(settings.isaac_sim_dir)
        os.environ["ISAAC_GPU"] = str(settings.isaac_gpu)
        os.environ.setdefault("RTX4090_STATE_ROOT", str(settings.state_root))

        from rtx4090.service import create_app  # 只负责启动 SimulationApp

        settings.output.mkdir(parents=True, exist_ok=True)
        self._app = create_app()

        # gRPC 专属子类：继承上游 RtxQueue，覆盖逐帧逻辑
        from .frame_capture import RtxQueueWithFrameCapture

        self.queue: Any = RtxQueueWithFrameCapture(settings.output, self._app, settings.capacity)
        self._warmup()

    def _warmup(self) -> None:
        """跑一次极小物理仿真，确保引擎可用后再对外提供服务。

        不包含渲染，避免首次 RTX capture 在冷启动时超时。
        RTX 管线会在首个带 render 的任务里按需初始化。
        """
        warmup = self.queue.output / "warmup"
        warmup.mkdir(parents=True, exist_ok=True)
        request = self.queue.prepare(
            {"bodies": [{"id": "warmup", "shape": "cube", "size": [0.1, 0.1, 0.1],
                         "position": [0, 0, 1.0], "mass": 1.0, "color": [0.5, 0.5, 0.5]}],
             "steps": 1}
        )
        self.queue.process(request, warmup)
        self.queue.ready = True

    def serve_forever(self, stop) -> None:
        """主线程循环：从队列取任务并执行。"""
        while not stop.is_set():
            try:
                item = self.queue.jobs.get(timeout=0.2)
            except queue_module.Empty:
                continue
            self.queue.run_one(item)

    def close(self) -> None:
        self.queue.ready = False
        self._app.close()
