"""统一加载由 proto 生成的模块。

生成产物位于 <repo>/generated，目录本身作为 Python 搜索路径根。
"""

from __future__ import annotations

import sys
from pathlib import Path

GENERATED = Path(__file__).resolve().parents[1] / "generated"
if str(GENERATED) not in sys.path:
    sys.path.insert(0, str(GENERATED))

from isaacsim_rpc.v1 import simulation_pb2, simulation_pb2_grpc  # noqa: E402

__all__ = ["GENERATED", "simulation_pb2", "simulation_pb2_grpc"]
