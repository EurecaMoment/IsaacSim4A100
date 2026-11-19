"""gRPC 服务配置。

gRPC 版与 HTTP 版（rtx4090/service.py）平级：自行加载 Isaac Sim 引擎，
直接驱动作业队列，不依赖也不经过 HTTP 服务。

优先级：命令行参数 > 环境变量 / .env 文件 > 内置默认值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = ROOT / ".env"


def load_env_file(path: Path) -> None:
    """把 KEY=VALUE 形式的环境文件注入 os.environ，已存在的变量不覆盖。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            os.environ.setdefault(key, value)


def _flag(name: str, default: bool) -> bool:
    return os.environ.get(name, "1" if default else "0").lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Settings:
    """服务运行参数。"""

    # gRPC 监听
    host: str = "127.0.0.1"
    port: int = 18500
    max_workers: int = 16
    reflection: bool = True

    # 作业队列
    capacity: int = 64

    # 引擎
    isaac_sim_dir: Path = Path("/mnt/data/zhouwei/isaacsim")
    isaac_gpu: int = 0
    state_root: Path = ROOT / "state"

    @property
    def output(self) -> Path:
        """产物根目录，任务在其下按 job_id 建子目录。"""
        return self.state_root / "jobs"

    @classmethod
    def load(cls, env_file: Path | None = DEFAULT_ENV_FILE) -> "Settings":
        if env_file is not None:
            load_env_file(env_file)
        defaults = cls()
        return cls(
            host=os.environ.get("GRPC_HOST", defaults.host),
            port=int(os.environ.get("GRPC_PORT", str(defaults.port))),
            max_workers=int(os.environ.get("GRPC_MAX_WORKERS", str(defaults.max_workers))),
            reflection=_flag("GRPC_REFLECTION", defaults.reflection),
            capacity=int(os.environ.get("GRPC_QUEUE_SIZE", str(defaults.capacity))),
            isaac_sim_dir=Path(os.environ.get("ISAAC_SIM_DIR", str(defaults.isaac_sim_dir))),
            isaac_gpu=int(os.environ.get("ISAAC_GPU", str(defaults.isaac_gpu))),
            state_root=Path(os.environ.get("RTX4090_GRPC_STATE_ROOT", str(defaults.state_root))),
        )
