# rtx4090_grpc

服务通过 `isaacsim_rpc.v1.SimulationService` 提供 RTX 快照和持久 USD 场景。快照与持久场景共用 GPU 队列，在 Kit 主线程执行。每个持久场景拥有独立的 USD 状态，上传资产可包含 USD、纹理及其他依赖。导入保留 USD 材质网络，RTX 渲染使用场景内的相机和灯光。

两者可以同时部署（建议使用不同的 `ISAAC_GPU` 和不同的产物目录），互不依赖、互不转发。

```
          ┌── HTTP  :18409  (rtx4090/service.py)  ──┐
客户端 ───┤                                         ├──► Isaac Sim 引擎 (Kit/PhysX/RTX)
          └── gRPC  :18500  (rtx4090_grpc)        ──┘
```

## 接口映射

| gRPC 方法 | 用途 |
| --- | --- |
| `Health`、`GetCapabilities`、`GetApiCatalog` | 读取运行状态、当前支持的操作和参数结构 |
| `UploadAsset`、`ListAssets` | 上传不可变文件，按同一资产 ID 和相对路径组织 USD 与纹理 |
| `SubmitJob` | 根据工作负载字段提交快照、持久场景任务或目录功能，返回作业 ID |
| `GetJob`、`WatchJob`、`CancelJob` | 查询、观察或请求取消已受理的作业 |
| `DownloadArtifact`、`AckJob` | 按作业清单下载产物，确认后允许服务清理已下载的文件 |

通过 `asset.import` 建立场景后，用返回的 `session_id` 继续编辑、推进物理、检查、采集或导出。`scene.preview` 返回当前持久 USD 版本的一张 PNG。`scene.capture` 使用最近一次物理推进后保存的状态，同时渲染指定的相机。开启标注时每个视角附有深度、距离、法线、实例、语义及有效像素掩码。

错误码映射：参数错误→`INVALID_ARGUMENT`、任务不存在→`NOT_FOUND`、队列满→`RESOURCE_EXHAUSTED`、引擎未就绪→`UNAVAILABLE`。

## 目录结构

```
rtx4090_grpc/
├── proto/isaacsim_rpc/v1/
│   └── simulation.proto        # proto 统一定义目录
├── generated/                  # protoc 生成产物（不入库）
├── server/
│   ├── config.py               # 配置：命令行 > .env > 默认
│   ├── rpc.py                  # 生成代码统一导入入口
│   ├── engine.py               # 创建 SimulationApp + RtxQueue，主线程作业循环
│   ├── codec.py                # protobuf ⇄ 上游 JSON 转换
│   ├── servicer.py             # 三个 RPC，直接操作本地队列
│   └── main.py                 # 服务入口
├── client/
│   ├── scenes.py               # 内置场景 + JSON 场景解析
│   └── cli.py                  # health / submit / status / run
├── start.sh                    # 用户入口：后台启动
├── shutdown.sh                 # 用户入口：优雅停止
├── restart.sh                  # 用户入口：重启
├── scripts/
│   ├── lib/
│   │   └── common.sh           # 公共函数：.env 加载、PID/端口检查
│   ├── system/                 # 系统脚本：环境准备与运行时启动
│   │   ├── setup_venv.sh       # 一键：venv + 依赖 + 代码生成 + 运行时库
│   │   ├── gen_proto.sh        # 由 proto 生成 pb2
│   │   ├── install_runtime_deps.sh  # 为 Isaac Python 3.12 装 gRPC 运行库
│   │   └── run_server.sh       # 前台启动引擎 + gRPC（被 start.sh 调用）
│   └── test/                   # 测试脚本
│       └── test_api.sh         # 接口测试入口（读取 .env 中的地址）
├── tests/
│   ├── test_servicer_unit.py   # 不加载引擎的 RPC 层单测
│   ├── test_api.py             # 接口级测试（Health/SubmitJob/GetJob/错误码）
│   └── smoke_test.py           # 端到端最小冒烟
├── runtime_python/             # Isaac Python 3.12 的 gRPC 运行库（不入库）
├── state/                      # 运行时：grpc.pid / grpc.log / jobs/（不入库）
├── .env                        # 生效配置（不入库）
├── .env.example
├── requirements.txt            # 开发侧（客户端/代码生成）
├── requirements-runtime.txt    # 引擎侧（Isaac Python 3.12）
└── README.md
```

## 快速开始

```bash
cd /mnt/data/zhouwei/IsaacSim4A100/rtx4090_grpc

# 1) 首次：venv + 依赖 + 生成 pb2 + 安装 Isaac Python 运行库
bash scripts/system/setup_venv.sh

# 2) 启停（后台常驻，自动管理 PID 与日志）
bash start.sh               # 立即返回，服务后台加载，日志 state/grpc.log
bash start.sh --wait        # 阻塞等待就绪（脚本编排时用）
bash shutdown.sh            # 优雅停止，30s 未退出则强制结束
bash restart.sh             # 重启

# 3) 接口测试（Health / SubmitJob / GetJob + 错误码）
bash scripts/test/test_api.sh
```

也可以前台启动、直接看输出：

```bash
bash scripts/system/run_server.sh                  # 前台运行（默认取 .env）
ISAAC_GPU=1 bash scripts/system/run_server.sh      # 临时换卡
```

其它测试：

```bash
.venv/bin/python tests/test_servicer_unit.py    # RPC 层单测，不加载引擎、不占 GPU
.venv/bin/python tests/smoke_test.py            # 端到端最小冒烟
```

客户端：

```bash
.venv/bin/python -m client.cli health
.venv/bin/python -m client.cli submit --scene cube
.venv/bin/python -m client.cli status --job-id <32位hex>
.venv/bin/python -m client.cli run --scene cube
.venv/bin/python -m client.cli run --scene ./my_scene.json
```

## 配置

优先级：命令行参数 > `.env` / 环境变量 > 内置默认值。

| 变量 | 默认 | 说明 |
|------|------|------|
| `GRPC_HOST` | `127.0.0.1` | 监听地址 |
| `GRPC_PORT` | `18500` | 监听端口 |
| `GRPC_MAX_WORKERS` | `16` | gRPC 工作线程数 |
| `GRPC_QUEUE_SIZE` | `64` | 作业队列容量 |
| `GRPC_REFLECTION` | `1` | 是否开启 gRPC 反射 |
| `ISAAC_SIM_DIR` | 复用 `rtx4090/.env` | Isaac Sim 安装目录 |
| `ISAAC_GPU` | 复用 `rtx4090/.env` | 物理 GPU 序号 |
| `RTX4090_GRPC_STATE_ROOT` | `<本目录>/state` | 产物根目录 |

`scripts/system/run_server.sh` 会先读 `rtx4090/.env`（仅当 `ISAAC_SIM_DIR` 未设置），再读本目录 `.env`，
使 gRPC 参数与引擎参数互不覆盖。

## 与 HTTP 版的差异

| 维度 | HTTP 版 | gRPC 版 |
|------|---------|---------|
| 协议 | JSON over HTTP | protobuf over HTTP/2 |
| 默认端口 | 18409 | 18500 |
| 引擎 | `RtxQueue` | 同一个 `RtxQueue` |
| 产物目录 | `--output` 参数 | `RTX4090_GRPC_STATE_ROOT/jobs` |
| Python 环境 | Isaac Python 3.12 | Isaac Python 3.12 + `runtime_python/` |
| 服务发现 | 无 | gRPC 反射（grpcurl 可用） |

## 说明

- **产物路径**：返回的是服务端本地绝对路径，gRPC 不传输文件本身。
- **只支持 mesh 渲染**：RTX 后端不接受 `render.kind=gaussian`，会返回 `INVALID_ARGUMENT`。
- **同一张卡不要双开**：gRPC 版与 HTTP 版同时跑时请用 `ISAAC_GPU` 错开。
- **健康检查语义**：`Health` 始终返回响应，`status` 字段给出 `ready/initializing/failed`。

## 常见问题

| 现象 | 原因与处理 |
|------|-----------|
| `Warning: running in conda env` | Isaac 的 `python.sh` 仅提示不中断；如遇异常先 `conda deactivate` |
| `CUDA error 304 / Failed to query CUDA device count` | 当前进程拿不到 GPU 设备（受限容器、沙箱或驱动不可见），需在可访问 `/dev/nvidia*` 的普通 shell 中启动 |
| `GLIBCXX_3.4.2x not found` | 父 shell 的 `LD_LIBRARY_PATH` 注入了旧版 `libstdc++`，启动前先 `unset LD_LIBRARY_PATH`，或只保留 `/usr/local/nvidia/lib*` |
| `缺少 runtime_python` | 执行 `bash scripts/system/install_runtime_deps.sh` |
| 端口被占用 | `--port` 换一个端口，或先停掉旧实例 |

