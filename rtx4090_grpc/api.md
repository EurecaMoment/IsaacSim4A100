# Isaac Sim RTX 4090 gRPC 服务 API 文档

> 协议：gRPC / proto3  ·  包：`isaacsim_rpc.v1`  ·  服务：`SimulationService`
> 默认地址：`127.0.0.1:18500`  ·  GPU：1（与 HTTP 服务平级独立）

---

## 目录

1. [RPC 接口总览](#1-rpc-接口总览)
2. [Health — 健康检查](#2-health--健康检查)
3. [SubmitJob — 提交仿真任务（幂等）](#3-submitjob--提交仿真任务幂等)
4. [GetJob — 查询任务 + 产物清单](#4-getjob--查询任务--产物清单)
5. [DownloadArtifact — 流式下载产物](#5-downloadartifact--流式下载产物)
6. [AckJob — 落盘确认（不立即删除）](#6-ackjob--落盘确认不立即删除)
7. [CancelJob — 取消任务](#7-canceljob--取消任务)
8. [WatchJob — 服务端推送状态](#8-watchjob--服务端推送状态)
9. [错误码汇总](#9-错误码汇总)

---

## 1. RPC 接口总览

| RPC | 请求类型 | 响应类型 | 说明 |
|-----|---------|---------|------|
| `Health` | `HealthRequest` | `HealthResponse` | 引擎就绪状态、GPU 信息、渲染能力、产物根与剩余空间 |
| `SubmitJob` | `SubmitJobRequest` | `JobHandle` | 提交仿真任务，**带幂等键 `request_id`**，立即返回 32 位十六进制 job_id |
| `GetJob` | `GetJobRequest` | `Job` | 轮询任务状态；成功时返回产物清单 `artifacts`（含 artifact_id / size / sha256，供下载）|
| `DownloadArtifact` | `DownloadArtifactRequest` | 流 `DownloadChunk` | 流式下载单个产物，支持 `offset` 断点续传、并发上限 |
| `AckJob` | `AckJobRequest` | `AckJobResponse` | 客户端落盘确认（仅打标记，不立即删产物）；所有产物统一由保留期到期回收 |
| `CancelJob` | `GetJobRequest` | `Job` | 取消排队或运行中的任务（尽力中断；取消后已写出的产物仍可下载）|
| `WatchJob` | `GetJobRequest` | 流 `Job` | 服务端推送状态与进度，终态后结束（可选；GetJob 轮询始终可用）|

---

## 2. Health — 健康检查

### 2.1 Request：`HealthRequest`

无字段（空 message）。

### 2.2 Response：`HealthResponse`

| # | 字段名 | 类型 | 示例 | 字段含义说明 |
|---|--------|------|------|-------------|
| 1 | `status` | `string` | `"ready"` | 引擎状态机：`initializing` → `ready` / `failed` |
| 2 | `engine` | `string` | `"Isaac Sim"` | 引擎名称，固定值 |
| 3 | `physical_gpu` | `int32` | `1` | 当前引擎绑定的物理 GPU 编号 |
| 4 | `cuda_required` | `bool` | `true` | 是否强制要求 CUDA |
| 5 | `cpu_fallback` | `bool` | `false` | 是否降级到 CPU 渲染（RTX 引擎固定 false）|
| 6 | `native_rtx` | `bool` | `true` | 是否使用原生 RTX 渲染后端 |
| 7 | `render_backend` | `string` | `"native_rtx"` | 渲染后端标识 |
| 8 | `gpu_model` | `string` | `"NVIDIA GeForce RTX 4090"` | GPU 型号字符串 |
| 9 | `gpu_uuid` | `string` | `"GPU-3b8913b2-..."` | NVIDIA GPU UUID |
| 10 | `isaac_sim` | `string` | `"6.0.1-rc.7+release.42383.32955d8d.gl"` | Isaac Sim 精确版本号 |
| 11 | `renderer` | `string` | `"RayTracedLighting"` | Hydra 渲染器名称 |
| 12 | `supported_shapes` | `repeated string` | `["cube","sphere","mesh"]` | 支持的几何类型列表 |
| 13 | `annotators` | `repeated string` | `["rgb","distance_to_image_plane","semantic_segmentation"]` | 可用的 RTX 标注器（决定了逐帧渲染能输出什么）|
| 14 | `uptime_seconds` | `double` | `328.7` | 自服务启动以来运行秒数 |
| 15 | `queue_depth` | `uint32` | `0` | 当前任务队列中待执行的任务数 |
| 16 | `queue_capacity` | `uint32` | `8` | 队列最大容量 |
| 17 | `active_job` | `string` | `"c1c65002..."` | 正在执行的 job_id；无任务时为空串 |
| 18 | `completed` | `uint32` | `12` | 自服务启动以来成功完成的任务数 |
| 19 | `failed` | `uint32` | `0` | 自服务启动以来失败的任务数 |
| 20 | `error` | `string` | — | 仅 `status="failed"` 时填充，引擎初始化错误信息 |
| 21 | `api_version` | `string` | `"1.0.0"` | 服务端契约版本，客户端启动时断言兼容 |
| 22 | `artifacts_root` | `string` | `"/mnt/data/.../state/jobs"` | 产物根目录（诊断用，不参与路径拼接）|
| 23 | `artifacts_free_bytes` | `int64` | `21474836480` | 产物所在磁盘剩余字节；低于水位时 SubmitJob 拒绝新任务 |
| 24 | `retention_seconds` | `int32` | `604800` | 产物保留期（秒）：任务结束后（无论是否 Ack）到期自动清理。对应 `.env` 的 `GRPC_RETENTION_SECONDS` |

---

## 3. SubmitJob — 提交仿真任务（幂等）

### 3.0 外层包装：`SubmitJobRequest`

| # | 字段名 | 类型 | 示例 | 字段含义说明 |
|---|--------|------|------|-------------|
| 1 | `request_id` | `string` | `"c1c65002..."`（32 位 hex） | **幂等键，必填**。客户端生成，建议 32 位小写十六进制（UUID4 即可）。同一 `request_id` 在服务端生命周期内唯一 |
| 2 | `simulation` | `SimulateRequest` | 见 §3.1 | 仿真参数（原 SubmitJob 的请求体）|

**幂等语义**：
- 同 `request_id` + **同参数** → 返回原任务的 `JobHandle`（状态可能已是 `succeeded`），不会重复入队
- 同 `request_id` + **不同参数** → `ALREADY_EXISTS`
- `request_id` 为空 → `INVALID_ARGUMENT`
- `request_id` 索引随 `job.json` 落盘，服务重启后仍可识别（参数比较基于 `simulation` 的归一化 JSON 摘要）

**错误码**：`INVALID_ARGUMENT`（空 request_id / 非法 shape / 越界参数）、`ALREADY_EXISTS`（同键异参）、`RESOURCE_EXHAUSTED`（队列满 / 磁盘低于水位）、`UNAVAILABLE`（引擎未就绪）。

### 3.1 Request：`SimulateRequest`

| # | 字段名 | 类型 | 示例 | 字段含义说明 |
|---|--------|------|------|-------------|
| 1 | `bodies` | `repeated BodySpec` | 见 §3.1.1 | 刚体列表。**留空**时上游自动生成 64 个默认立方体（动态，mass=1.0） |
| 2 | `steps` | `uint32` | `120` | 物理仿真步数；范围 1–1200；缺省 240 |
| 3 | `dt` | `double` | `0.008333` | 每步物理时长（秒）；范围 0.0001–0.02。1/120 ≈ 0.008333 即 120Hz；缺省 1/120 |
| 4 | `gravity` | `double` | `9.81` | 重力加速度（m/s²）；范围 0–100；缺省 9.81 |
| 5 | `ground` | `bool` | `true` | 是否创建地平面；缺省 true |
| 6 | `render` | `RenderSpec` | 见 §3.1.2 | 相机 / 灯光 / 渲染参数；缺省使用上游默认（见 §3.1.2）|
| 7 | `capture` | `CaptureSpec` | 见 §3.1.3 | **逐帧图文对采集**。**设了就强制逐帧渲染**（RGB + depth + semantic + state）。缺省 = 不采样，只输出尾帧 |

#### 3.1.1 `BodySpec` — 刚体定义

| # | 字段名 | 类型 | 示例 | 字段含义说明 |
|---|--------|------|------|-------------|
| 1 | `id` | `string` | `"blue-cube"` | 刚体唯一标识；**同时作为语义分割标签名**。留空则服务端按索引生成 `B0000`, `B0001`, … |
| 2 | `shape` | `string` | `"cube"` | 几何类型。可选：`"cube"` / `"sphere"` / `"mesh"` |
| 3 | `position` | `Vec3` | `{"x":-0.25,"y":0.0,"z":1.2}` | 世界坐标，单位米 |
| 4 | `orientation_xyzw` | `QuatXYZW` | `{"x":0,"y":0,"z":0,"w":1}` | 姿态四元数，xyzw 顺序；默认 `(0,0,0,1)` 无旋转 |
| 5 | `size` | `Vec3` | `{"x":0.2,"y":0.2,"z":0.2}` | **仅 cube 必须提供**。三轴尺寸，单位米 |
| 6 | `radius` | `double` | `0.13` | **仅 sphere 必须提供**。半径，单位米 |
| 7 | `mass` | `double` | `1.0` | 质量（kg），范围 `0.0001–100000`。**缺省或显式 `mass=0` 表示静态/运动学刚体**：不受重力、不响应接触冲量，`Job.bodies[].velocity` 恒为 0，位姿保持不变。显式正数则创建动态刚体 |
| 8 | `color` | `Vec3` | `{"x":0.15,"y":0.4,"z":0.8}` | 显示颜色 RGB，范围 0–1 |
| 9 | `asset` | `string` | `"/abs/path/to/mesh_dir"` | **仅 mesh**。目录绝对路径，需包含 `mesh.npz` |

**静态体与动态体**

| 输入 | 创建方式 | 行为 |
|---|---|---|
| `mass` 省略 | `RigidBodyAPI` + `KinematicEnabled=true`（不写 `MassAPI`） | 不受重力、不响应接触冲量；可与动态体碰撞并推开对方；位姿保持不变；`Job.bodies[].velocity` 恒为 `[0,0,0,0,0,0]` |
| `mass=0` | 同上（作为静态体的等价写法，不会报错） | 同上 |
| `mass>0` | `RigidBodyAPI` + `MassAPI(mass)` | 正常动态刚体，受重力与碰撞影响 |

> 说明：`mass` 的合法正数范围是 `0.0001–100000`；负数、0 与 0.0001 之间的值仍返回 `INVALID_ARGUMENT`。静态体同样占用 `rigid_bodies` 计数与 `Job.bodies` 条目，只是速度为 0。

#### 3.1.2 `RenderSpec` — 相机 / 灯光 / 渲染

| # | 字段名 | 类型 | 示例 | 字段含义说明 |
|---|--------|------|------|-------------|
| 1 | `kind` | `string` | `"mesh"` | 渲染后端类型；RTX 固定 `"mesh"` |
| 2 | `size` | `uint32` | `512` | 图像边长像素；范围 64–1024；缺省 512 |
| 3 | `spp` | `uint32` | `32` | RTX Hydra 子帧数（等价于 `rt_subframes`）；范围 1–256；缺省 32 |
| 4 | `seed` | `uint32` | `42` | 随机种子，影响初始抖动模式 |
| 5 | `azimuth` | `double` | `-45.0` | 相机方位角（度）；绕 z 轴旋转 |
| 6 | `elevation` | `double` | `30.0` | 相机仰角（度）；0° 水平，90° 正上方 |
| 7 | `distance` | `double` | `6.0` | 相机与 `target` 的距离（米）|
| 8 | `target` | `Vec3` | `{"x":0,"y":0,"z":0.5}` | 相机朝该点看 |
| 9 | `gaussian_gain` | `double` | `1.0` | 渲染器增益 |
| 10 | `lighting` | `LightingSpec` | 见下方 | 灯光配置。缺省：DomeLight + RectLight(Key) + 若干 RectLight(Fill) |

**`LightingSpec`（灯光组）**

| # | 字段名 | 类型 | 示例 | 字段含义说明 |
|---|--------|------|------|-------------|
| 1 | `dome` | `LightSpec` | `color=[0.45,0.48,0.52], intensity=1.0` | 半球环境光（`UsdLux.DomeLight`）|
| 2 | `key` | `LightSpec` | `width=3.6, height=3.6, intensity=5, origin=[-2,-3,4], target=[0,0,0.5]` | 主方向光（`UsdLux.RectLight`）|
| 3 | `fills` | `repeated LightSpec` | `[{intensity:1,width:2,height:2}]` | 补光列表，多个 RectLight |

**`LightSpec`（单灯光）**

| # | 字段名 | 类型 | 示例 | 字段含义说明 |
|---|--------|------|------|-------------|
| 1 | `color` | `Vec3` | `{"x":0.45,"y":0.48,"z":0.52}` | 颜色 RGB，范围 0–1 |
| 2 | `intensity` | `double` | `1.0` | 光强 |
| 3 | `exposure` | `double` | — | 可选曝光值 |
| 4 | `width` | `double` | `3.6` | 仅 RectLight（key / fills），单位米 |
| 5 | `height` | `double` | `3.6` | 仅 RectLight（key / fills），单位米 |
| 6 | `origin` | `Vec3` | `{"x":-2,"y":-3,"z":4}` | 仅 RectLight，光源位置 |
| 7 | `target` | `Vec3` | `{"x":0,"y":0,"z":0.5}` | 仅 RectLight，光源朝向点 |

#### 3.1.3 `CaptureSpec` — 逐帧图文对采集

> **关键约束**：本 message **存在即强制逐帧渲染**（不需要独立的 render 开关）。

| # | 字段名 | 类型 | 示例 | 字段含义说明 |
|---|--------|------|------|-------------|
| 1 | `every` | `uint32` | `20` | 每 N 帧采一次。与 `frames` 同时给出时取并集；**自动把尾帧（= steps）加入集合** |
| 2 | `frames` | `repeated uint32` | `[10,50,120]` | 指定帧号列表；范围 1–steps；自动去重排序；最多 1200 项 |

**帧号集合最终确定逻辑**：`{every, 2×every, …, ≤steps} ∪ frames_set ∪ {steps}`

#### 3.1.4 共享类型：`Vec3` / `QuatXYZW`

**`Vec3`**

| # | 字段名 | 类型 | 示例 | 含义 |
|---|--------|------|------|------|
| 1 | `x` | `double` | `0.0` | — |
| 2 | `y` | `double` | `0.0` | — |
| 3 | `z` | `double` | `0.0` | — |

**`QuatXYZW`**

| # | 字段名 | 类型 | 示例 | 含义 |
|---|--------|------|------|------|
| 1 | `x` | `double` | `0.0` | 向量部分 x |
| 2 | `y` | `double` | `0.0` | 向量部分 y |
| 3 | `z` | `double` | `0.0` | 向量部分 z |
| 4 | `w` | `double` | `1.0` | 实部；(0,0,0,1) 表示无旋转 |

### 3.2 Response：`JobHandle`

立即返回的任务句柄，用于后续 `GetJob` 轮询。

| # | 字段名 | 类型 | 示例 | 字段含义说明 |
|---|--------|------|------|-------------|
| 1 | `id` | `string` | `"c1c6500206c345668c6859c6ed69a46f"` | 32 位小写十六进制 job_id |
| 2 | `state` | `string` | `"queued"` | 初始状态固定 `"queued"` |
| 3 | `queued_at` | `double` | `1794992479.573` | Unix 秒，浮点精度 |

---

## 4. GetJob — 查询任务 + 产物清单

### 4.1 Request：`GetJobRequest`

| # | 字段名 | 类型 | 示例 | 字段含义说明 |
|---|--------|------|------|-------------|
| 1 | `job_id` | `string` | `"c1c6500206c345668c6859c6ed69a46f"` | 32 位小写十六进制 |

### 4.2 Response：`Job`

| # | 字段名 | 类型 | 示例 | 字段含义说明 |
|---|--------|------|------|-------------|
| 1 | `id` | `string` | `"c1c6500206c345668c6859c6ed69a46f"` | job_id |
| 2 | `state` | `string` | `"succeeded"` | 状态机：`queued` → `running` → `succeeded`/`failed`/`interrupted` |
| 3 | `queued_at` | `double` | `1794992479.573` | 入队 Unix 秒 |
| 4 | `started_at` | `double` | `1794992479.584` | 开始执行 Unix 秒 |
| 5 | `finished_at` | `double` | `1794992485.422` | 结束 Unix 秒（成功或失败）|
| 6 | `queue_seconds` | `double` | `0.011` | 排队耗时（秒）= started_at − queued_at |
| 7 | `execution_seconds` | `double` | `5.838` | 执行耗时（秒）= finished_at − started_at |
| 8 | `error` | `string` | — | 仅 `state="failed"` 时填充，Python traceback 摘要 |
| 9 | `isaac_sim` | `string` | `"6.0.1-rc.7+..."` | Isaac Sim 版本 |
| 10 | `physics_device` | `string` | `"cuda:1"` | PhysX 物理引擎绑定的 CUDA 设备 |
| 11 | `physical_gpu` | `int32` | `1` | 物理 GPU 编号 |
| 12 | `native_rtx` | `bool` | `true` | 是否原生 RTX 后端 |
| 13 | `cpu_fallback` | `bool` | `false` | 是否降级到 CPU |
| 14 | `rigid_bodies` | `uint32` | `2` | 本次仿真的刚体数量 |
| 15 | `steps` | `uint32` | `120` | 物理仿真步数 |
| 16 | `dt` | `double` | `0.008333` | 每步时长（秒）|
| 17 | `simulation_seconds` | `double` | `1.0` | 仿真总时长 = steps × dt |
| 18 | `setup_seconds` | `double` | `3.0` | 场景构建 + USD 导入耗时 |
| 19 | `physics_seconds` | `double` | `5.8` | 物理 step + 逐帧渲染总耗时 |
| 20 | `collision` | `string` | `"PhysX primitives; convexHull for dynamic mesh"` | 碰撞体策略说明 |
| 21 | `bodies` | `repeated BodyState` | 见 §4.2.1 | 尾帧每个刚体的物理状态 |
| 22 | `outputs` | `Artifacts` | 见 §4.2.2 | **遗留字段**：全部产物的服务端**绝对路径**（原 `artifacts`，已重命名为 `outputs` 以避让新清单；新代码请用 `artifacts` 清单 + DownloadArtifact）|
| 23 | `render` | `RenderInfo` | 见 §4.2.3 | 尾帧渲染元信息（非路径，是数值 + 路径混合）|
| 24 | `artifacts` | `repeated Artifact` | 见 §4.2.4 | **产物清单（新）**：每个产物的 `artifact_id` / `name` / `relative_path` / `size_bytes` / `sha256` / `media_type`，供 `DownloadArtifact` 拉取 |
| 25 | `phase` | `string` | `"simulating"` | 当前阶段：`queued` / `loading_scene` / `simulating` / `rendering` / `writing_artifacts` / 终态 |
| 26 | `frames_done` | `uint32` | `40` | 已完成采集的帧数（capture 模式）；运行中实时更新 |
| 27 | `frames_total` | `uint32` | `120` | 计划采集的总帧数（capture 模式）|

#### 4.2.4 `Artifact` — 单个产物清单条目

| # | 字段名 | 类型 | 示例 | 字段含义说明 |
|---|--------|------|------|-------------|
| 1 | `artifact_id` | `string` | `"a1b2c3d4e5f60718"` | 任务内稳定唯一 id，由 `relative_path` 派生；传给 `DownloadArtifact.artifact_id` |
| 2 | `name` | `string` | `"render.png"` | 文件名 |
| 3 | `relative_path` | `string` | `"<job_id>/frames/frame_000040/render.png"` | 相对服务端 `artifacts_root` 的路径 |
| 4 | `size_bytes` | `int64` | `3145728` | 文件字节数；`DownloadArtifact` 的 `offset` 必须 ≤ 此值 |
| 5 | `sha256` | `string` | `"e3b0c442..."`（64 位 hex） | 文件内容 sha256，客户端下载后校验完整性 |
| 6 | `media_type` | `string` | `"image/png"` | MIME 类型：`.png`→image/png，`.npy`→application/x-npy，`.npz`→application/x-npz，`.json`→application/json，`.usdc`→model/vnd.usdc |

#### 4.2.1 `BodyState` — 单个刚体尾帧状态

| # | 字段名 | 类型 | 示例 | 字段含义说明 |
|---|--------|------|------|-------------|
| 1 | `id` | `string` | `"blue-cube"` | 与请求中的 `BodySpec.id` 一致 |
| 2 | `prim_path` | `string` | `"/World/Bodies/B0000"` | USD Stage 中对应的 prim 路径 |
| 3 | `pose_xyzw` | `repeated double (7)` | `[-0.246, 0.005, 0.100, 0, 0, 0, 1.0]` | **7 维数组**：`[x, y, z, qx, qy, qz, qw]` |
| 4 | `velocity` | `repeated double (6)` | `[5e-5, -3e-5, -1e-4, 8e-4, 1e-4, -2e-6]` | **6 维数组**：`[vx, vy, vz, ωx, ωy, ωz]`。静态/运动学刚体（`BodySpec.mass` 省略或为 0）恒为全 0 |

#### 4.2.2 `Artifacts` — 全部产物文件路径

> **所有路径均为服务端文件系统的绝对路径**，指向的文件格式见 §4.3。

| # | 字段名 | 类型 | 路径模式 | 文件说明（见 §4.3 小节）|
|---|--------|------|---------|------------------------|
| 1 | `state_npz` | `string` | `.../jobs/<job_id>/state.npz` | 尾帧全局物理状态 — §4.3.1 |
| 2 | `scene` | `string` | `.../jobs/<job_id>/scene.usdc` | 完整 USD 场景 — §4.3.7 |
| 3 | `simulation_json` | `string` | `.../jobs/<job_id>/simulation.json` | 与本 Job 响应等价的 JSON 快照 — §4.3.8 |
| 4 | `image` | `string` | `.../jobs/<job_id>/render.png` | 尾帧 RGB 图像 — §4.3.2 |
| 5 | `depth_m` | `string` | `.../jobs/<job_id>/depth_m.npy` | 尾帧深度图 — §4.3.3 |
| 6 | `semantic_ids` | `string` | `.../jobs/<job_id>/semantic_ids.npy` | 尾帧语义分割 — §4.3.4 |
| 7 | `semantic_labels` | `string` | `.../jobs/<job_id>/semantic_labels.json` | 语义 ID → 类名映射 — §4.3.5 |
| 8 | `frames` | `repeated FrameArtifact` | 见下方 | **逐帧采样产物**；未设置 `capture` 时为空列表 |

**`FrameArtifact`（单帧路径集合，按帧号排序）**

> 每个元素对应一帧，全部字段都是指向该帧子目录的绝对路径。

| # | 字段名 | 类型 | 路径模式 | 文件说明 |
|---|--------|------|---------|---------|
| 1 | `frame` | `uint32` | — | 帧号（标量）|
| 2 | `state_npz` | `string` | `.../jobs/<id>/frames/frame_XXXXXX/state.npz` | 该帧的物理状态 — §4.3.6 |
| 3 | `image` | `string` | `.../jobs/<id>/frames/frame_XXXXXX/render.png` | 该帧的 RGB 图像 — §4.3.2 |
| 4 | `depth_m` | `string` | `.../jobs/<id>/frames/frame_XXXXXX/depth_m.npy` | 该帧的深度图 — §4.3.3 |
| 5 | `semantic_ids` | `string` | `.../jobs/<id>/frames/frame_XXXXXX/semantic_ids.npy` | 该帧的语义分割 — §4.3.4 |
| 6 | `semantic_labels` | `string` | `.../jobs/<id>/frames/frame_XXXXXX/semantic_labels.json` | 语义标签表（与尾帧同内容）— §4.3.5 |

#### 4.2.3 `RenderInfo` — 尾帧渲染元信息

| # | 字段名 | 类型 | 示例 | 字段含义说明 |
|---|--------|------|------|-------------|
| 1 | `backend` | `string` | `"omni.hydra.rtx"` | Hydra 渲染后端标识 |
| 2 | `renderer` | `string` | `"RayTracedLighting"` | RTX 渲染器名称 |
| 3 | `native_rtx` | `bool` | `true` | 是否原生 RTX |
| 4 | `size` | `uint32` | `512` | 图像边长像素 |
| 5 | `requested_spp` | `uint32` | `32` | 请求的子帧数 |
| 6 | `rt_subframes` | `uint32` | `32` | 实际使用的 Hydra 子帧数 |
| 7 | `sampling` | `string` | `"RTX real-time subframes; not Mitsuba path-tracing spp"` | 采样模式说明 |
| 8 | `image` | `string` | 同 outputs.image | 尾帧 RGB 路径（重复返回，方便直接访问）|
| 9 | `depth_m` | `string` | 同 outputs.depth_m | 尾帧深度路径 |
| 10 | `depth_convention` | `string` | `"distance_to_image_plane in stage meters; background may be infinity"` | 深度值语义说明 |
| 11 | `semantic_ids` | `string` | 同 outputs.semantic_ids | 尾帧语义分割路径 |
| 12 | `semantic_labels` | `string` | 同 outputs.semantic_labels | 语义标签表路径 |
| 13 | `rgb_mean` | `double` | `98.75` | 尾帧 render.png 的全局像素均值（0–255）|
| 14 | `seconds` | `double` | `1.476` | 尾帧 render 的 wall-clock 耗时（秒）|

---

## 4.3 产物文件格式详解

> 以下所有文件均位于**服务端** `.../state/jobs/<job_id>/` 目录下。  
> §4.3.1–4.3.5 为**尾帧**产物（始终存在）；§4.3.6 为**逐帧**产物（仅设置了 `capture` 时存在）。

### 4.3.1 `state.npz` — 尾帧全局物理状态

| 属性 | 值 |
|------|-----|
| 文件格式 | NumPy 压缩包（`np.savez`），`.npz` 是一个 ZIP |
| 加载方式 | `np.load(path)` |

**内部 key 详解**

| key | shape | dtype | 含义 | 数值解读示例 |
|-----|-------|-------|------|-------------|
| `poses_xyzw` | `(N, 7)` | float32 | N = 刚体数量。每行 7 维：`[x, y, z, qx, qy, qz, qw]` | `(-0.246, 0.005, 0.100, 0, 0, 0, 1.0)` → 该刚体中心在 (-0.246, 0.005, 0.100)m，无旋转（四元数 = (0,0,0,1)）|
| `velocities` | `(N, 6)` | float32 | 每行 6 维：`[vx, vy, vz, ωx, ωy, ωz]` | `(5e-5, -3e-5, -1e-4, 8e-4, 1e-4, -2e-6)` → 线速度 m/s，角速度 rad/s |

**Python 读取示例**

```python
import numpy as np
d = np.load("state.npz")
print(list(d.files))       # ['poses_xyzw', 'velocities']
poses = d["poses_xyzw"]    # shape=(2, 7)
vel   = d["velocities"]    # shape=(2, 6)
```

---

### 4.3.2 `render.png` — RGB 图像（尾帧 + 每帧同格式）

| 属性 | 值 |
|------|-----|
| 格式 | PNG，uint8，RGB 三通道 |
| 加载方式 | `PIL.Image.open(path)` 或 `plt.imread(path)` |
| 分辨率 | 由 `RenderSpec.size` 决定（默认 512×512）|
| dtype | uint8（0–255）|

**物理含义**：RTX `RayTracedLighting` 实时光线追踪渲染结果。  
**与 depth_m.npy / semantic_ids.npy 对齐**：三者 shape 完全相同 `(size, size)`，像素索引一一对应。

```
render.png[y, x] → (R, G, B) uint8
```

---

### 4.3.3 `depth_m.npy` — 深度图（尾帧 + 每帧同格式）

| 属性 | 值 |
|------|-----|
| 文件格式 | NumPy `.npy`（`np.save`），未压缩 |
| 加载方式 | `np.load(path)` |
| shape | `(size, size)`，与 render.png 完全相同 |
| dtype | float32 |
| 单位 | **米** |

**物理语义**：`distance_to_image_plane` — 相机**射线方向**到物体表面的距离。

```
depth_m.npy[y, x] = 该像素对应射线打到物体表面的距离（米）
```

**注意事项**：
- 地平线 / 无遮挡区域值为 `+inf`，读取时用 `np.isfinite(d)` 过滤
- 近物体像素值小，远物体像素值大
- 如果要转换到相机空间（XYZ point cloud），需要结合相机内参和外参（`RenderSpec.target` / `azimuth` / `elevation` / `distance`）反投影

**Python 读取示例**

```python
import numpy as np
d = np.load("depth_m.npy")
finite = d[np.isfinite(d)]
print(f"min={finite.min():.3f}m  max={finite.max():.3f}m  mean={finite.mean():.3f}m")
```

---

### 4.3.4 `semantic_ids.npy` — 语义分割（尾帧 + 每帧同格式）

| 属性 | 值 |
|------|-----|
| 文件格式 | NumPy `.npy` |
| shape | `(size, size)`，与 render.png / depth_m.npy 完全相同 |
| dtype | **uint32** |

**值的语义**：每个像素是一个整数 ID，通过 §4.3.5 的 `semantic_labels.json` 反查类名。

```
semantic_ids.npy[y, x] = 该像素所属物体的语义 ID
```

**常见 ID 分布**（以示例任务为例）：

| ID | 类名 | 像素数 | 占比 |
|----|------|-------|------|
| 0 | `BACKGROUND` | 0 | 0% |
| 1 | `UNLABELLED` | 0 | 0% |
| 2 | `blue-cube` | ~1700 | 0.7% |
| 3 | `red-sphere` | ~1440 | 0.6% |
| 4 | `ground` | ~259000 | 98.8% |

**Python 读取示例**

```python
import numpy as np
s = np.load("semantic_ids.npy")
unique, counts = np.unique(s, return_counts=True)
for uid, cnt in zip(unique, counts):
    print(f"id={uid}: {cnt} px ({cnt/s.size*100:.1f}%)")
```

---

### 4.3.5 `semantic_labels.json` — 语义 ID → 类名映射

**文件格式**：JSON，固定结构。

**示例内容**

```json
{
  "idToLabels": {
    "0": {"class": "BACKGROUND"},
    "1": {"class": "UNLABELLED"},
    "2": {"class": "blue-cube"},
    "3": {"class": "red-sphere"},
    "4": {"class": "ground"}
  }
}
```

**规则**：
- `"0"` (`BACKGROUND`) 和 `"1"` (`UNLABELLED`) 是 Isaac Sim 预定义常量，始终存在
- `"2"` 起依次对应你请求中的每个 `BodySpec.id`（顺序与请求中的 bodies 数组一致）
- 固定包含 `"ground"`（地平面）
- **同一 job 的尾帧和每帧子目录里的 `semantic_labels.json` 内容相同**

---

### 4.3.6 `frames/frame_XXXXXX/state.npz` — 每帧独立物理快照

| 属性 | 值 |
|------|-----|
| 与全局 state.npz 差异 | **多了 3 个标量字段**：`frame`、`time` |
| 所在目录 | `.../jobs/<job_id>/frames/frame_XXXXXX/` |

**内部 key 详解**

| key | shape | dtype | 含义 | 示例 |
|-----|-------|-------|------|------|
| `frame` | `()` 标量 | int32 | 帧号（与目录名 XXXXXX 一致）| `20` |
| `time` | `()` 标量 | float64 | 仿真时间 = frame × dt 秒 | `0.1667` |
| `poses_xyzw` | `(N, 7)` | float32 | 该帧的刚体位姿 | 同 §4.3.1 |
| `velocities` | `(N, 6)` | float32 | 该帧的刚体速度 | 同 §4.3.1 |

---

### 4.3.7 `scene.usdc` — 完整 USD 场景文件

| 属性 | 值 |
|------|-----|
| 文件格式 | OpenUSD crate 格式（`.usdc` = compressed binary）|
| 典型大小 | ~5 KB（两个刚体 + 地面 + 相机 + 灯光）|
| 加载方式 | `pxr.Usd.Stage.Open(path)` |

**包含的 Prim（路径 → 类型）**

| Prim 路径 | 类型 | 说明 |
|-----------|------|------|
| `/World` | `UsdStage` root | 根 prim |
| `/World/Bodies/B0000` | `UsdGeom.Cube`（或 `UsdGeom.Sphere`）| 刚体 0（blue-cube）|
| `/World/Bodies/B0001` | `UsdGeom.Sphere` | 刚体 1（red-sphere）|
| `/World/Camera` | `UsdGeom.Camera` | 渲染相机 |
| `/World/Dome` | `UsdLux.DomeLight` | 半球环境光 |
| `/World/KeyLight` | `UsdLux.RectLight` | 主方向光 |
| `/World/Fill00` ~ `/World/FillNN` | `UsdLux.RectLight` | 补光列表 |

**包含的属性**：
- 每个刚体的：物理材质（`UsdPhysics.MaterialAPI`）、碰撞体（`UsdPhysics.CollisionAPI`）、刚体标签（`Semantics.LabelsAPI`，这就是语义分割 ID 的来源）、显示颜色
- 相机的：焦距、光圈尺寸（从 `RenderSpec.size` / `spp` 派生）
- 灯光的：颜色、强度、曝光、宽高、origin/target

**可用工具**：
- USD Composer（GUI 可视化）
- Isaac Sim（在同一引擎中打开回放）
- `pxr.Usd.Stage.Open()`（Python 程序化读取）

---

### 4.3.8 `simulation.json` — 仿真元数据快照

| 属性 | 值 |
|------|-----|
| 文件格式 | JSON |
| 内容 | 与本 `Job` gRPC 响应等价的字典序列化 |
| 用途 | 方便离线查看，不依赖 gRPC 连接 |

**包含字段**：与 §4.2 中 `Job` response 字段一一对应。

---

## 4.4 服务端目录结构总图

```
state/jobs/<job_id>/                          ← 一个仿真任务
├── request.json                              ← 你的原始请求（SimulateRequest 序列化）
├── job.json                                  ← 服务端内部记录
├── simulation.json                           ← 仿真元数据（= Job 响应序列化）
│
├── scene.usdc                                ← 完整 USD 场景
│
├── state.npz                                 ← 尾帧物理状态
│   ├── poses_xyzw  (N, 7) float32
│   └── velocities  (N, 6) float32
│
├── render.png                                ← 尾帧 RGB (size×size×3 uint8)
├── depth_m.npy                               ← 尾帧深度 (size×size float32, 单位米)
├── semantic_ids.npy                          ← 尾帧语义 (size×size uint32)
├── semantic_labels.json                      ← {idToLabels: {"2": {"class": "blue-cube"}, ...}}
│
└── frames/                                   ← 仅设置 capture 时存在
    ├── frame_000020/
    │   ├── state.npz                         ← 帧号/时间/poses/velocities
    │   ├── render.png                       ← RGB
    │   ├── depth_m.npy                      ← 深度
    │   ├── semantic_ids.npy                 ← 语义分割
    │   └── semantic_labels.json             ← 同尾帧（idToLabels 一致）
    ├── frame_000040/  ...
    └── frame_000120/  ...
```

## 4.5 图文对数据的对齐关系

```
帧号 frame_000060
┌─ state.npz ──────────────────────────────────┐
│  frame = 60   time = 0.500s                  │
│  poses_xyzw[0] = [-0.248, 0.002, 0.101, 0,0,0,1]  ← cube 触地  │
└──────────────────────────────────────────────┘
┌─ render.png ──────────────────────────────────┐
│  shape=(512,512,3) dtype=uint8                │
│  [y=100, x=200] → [R=38, G=102, B=204]        │
└──────────────────────────────────────────────┘
┌─ depth_m.npy ─────────────────────────────────┐
│  shape=(512,512) dtype=float32  ← 与 png 对齐 │
│  [y=100, x=200] → 7.347 米                    │
└──────────────────────────────────────────────┘
┌─ semantic_ids.npy ───────────────────────────┐
│  shape=(512,512) dtype=uint32  ← 与 png 对齐 │
│  [y=100, x=200] → 2                          │
└──────────────────────────────────────────────┘
┌─ semantic_labels.json ────────────────────────┐
│  "2" → {"class": "blue-cube"}                │
└──────────────────────────────────────────────┘
```

**三者对齐保证**：`render.png` / `depth_m.npy` / `semantic_ids.npy` 的 **shape 完全相同** `(size, size)`，像素索引 `[y, x]` 在三者中指向同一物理点。你可以：
- 拿 `semantic_ids` 当 mask 把物体从 `render.png` 中分割出来
- 用 `depth_m` + 相机参数反投影恢复 3D 点云
- 拿 `state.npz` 的 poses 当 "ground truth" 监督视觉重建

---

## 5. DownloadArtifact — 流式下载产物

### 5.1 Request：`DownloadArtifactRequest`

| # | 字段名 | 类型 | 示例 | 字段含义说明 |
|---|--------|------|------|-------------|
| 1 | `job_id` | `string` | `"c1c6..."` | 32 位小写十六进制 |
| 2 | `artifact_id` | `string` | `"a1b2c3..."` | 来自 `GetJob` 返回的 `Job.artifacts[].artifact_id` |
| 3 | `offset` | `int64` | `0` | 起始字节偏移；`0` 从头开始，`>0` 断点续传。必须 ≤ 产物 `size_bytes` |
| 4 | `chunk_bytes` | `uint32` | `0` | 期望块大小；`0` 用服务端默认（1 MiB），上限 4 MiB |

### 5.2 响应流：`DownloadChunk`

| # | 字段名 | 类型 | 说明 |
|---|--------|------|------|
| 1 | `offset` | `int64` | 本块数据在文件中的起始偏移 |
| 2 | `data` | `bytes` | 文件内容块 |

### 5.3 行为与错误码

- 任务状态必须为 `succeeded`（或取消后的 `interrupted`，已写出的产物仍可下载），否则 `FAILED_PRECONDITION`
- 产物已被保留期清理 → `NOT_FOUND`（details 含 `artifacts_released`）
- `artifact_id` 不属于该任务或任务不存在 → `NOT_FOUND`
- `offset > size_bytes` → `OUT_OF_RANGE`；`offset == size_bytes` → 空流（正常结束）
- 单任务并发下载流数超过上限（默认 8）→ `RESOURCE_EXHAUSTED`
- 客户端应把各块按 `offset` 拼接，并用 `Job.artifacts[].sha256` 校验整体完整性后再 `AckJob`

---

## 6. AckJob — 落盘确认（不立即删除）

### 6.1 Request：`AckJobRequest`

| # | 字段名 | 类型 | 示例 | 字段含义说明 |
|---|--------|------|------|-------------|
| 1 | `job_id` | `string` | `"c1c6..."` | 32 位小写十六进制 |

### 6.2 Response：`AckJobResponse`

| # | 字段名 | 类型 | 示例 | 字段含义说明 |
|---|--------|------|------|-------------|
| 1 | `released` | `bool` | `false` | **始终为 false**（本版 Ack 不立即释放产物；保留期到期由后台线程统一清理）。保留该字段供未来恢复"Ack 立即删"语义时使用 |
| 2 | `freed_bytes` | `int64` | `0` | **始终为 0**（本版 Ack 不立即释放）。保留该字段供未来恢复"Ack 立即删"语义时使用 |

### 6.3 行为

- Ack 的**唯一作用**是在 `job.json` 上标记 `acked=True`，表示客户端已完成校验落盘、消费方已确认。**不触发任何磁盘删除**
- 产物的唯一回收路径是**保留期清理**（`Health.retention_seconds`，默认 7 天）。后台守护线程每 60s 扫描一次，只要任务已结束且 `finished_at` 距今超过保留期，**无论是否 Ack**，一律清理产物目录
- Ack 后产物仍完整保留、`DownloadArtifact` 仍可正常下载，直到保留期到期
- 重复调用幂等：多次 Ack 不会重复标记、不会报错
- 任务不存在 → `NOT_FOUND`
- **磁盘水位保护独立工作**：即使所有任务都 Ack 了，但磁盘剩余低于 `GRPC_MIN_FREE_BYTES`，新 SubmitJob 仍会被拒绝

### 6.4 回收模型总结

| 路径 | 触发条件 | 执行时机 | 覆盖范围 |
|---|---|---|---|
| **保留期清理（唯一）** | 任务已结束 + `finished_at` 距今 > `retention_seconds` | 后台线程每 60s 扫描 | 所有到期任务，**不管 acked=True 还是 False** |
| 主动 Ack | 客户端显式调用 | 仅标记，不删文件 | — |

> **历史说明**：旧版 Ack 会立即删除产物并回收磁盘（返回 `released=true`、`freed_bytes>0`）。2026-11-25 起改为"统一由保留期清理"模型，Ack 仅保留确认语义。如需恢复旧行为，可在 `job_manager.py` 的 `ack()` 里加回 `_delete_artifacts` 调用，并在 `_sweep_retention()` 里跳过 `acked=True` 的任务。

---

## 7. CancelJob — 取消任务

### 7.1 Request / Response

请求复用 `GetJobRequest`（仅需 `job_id`）；响应为 `Job`（返回取消后的实际状态）。

### 7.2 行为

- `queued`：立即从队列移除，状态置 `interrupted`，不会开始执行
- `running`：尽力中断（逐帧循环每帧检查取消标志），已写出的帧产物保留，状态置 `interrupted`，仍可下载已写出产物
- 终态（`succeeded` / `failed` / `interrupted`）：幂等返回当前状态
- 任务不存在 → `NOT_FOUND`

---

## 8. WatchJob — 服务端推送状态

> 可选能力（P2）。`GetJob` 轮询始终可用；`WatchJob` 用于减少轮询开销、更快获得进度更新。

### 8.1 Request / 响应流

请求复用 `GetJobRequest`；服务端推送 `Job` 流。

### 8.2 行为

- 首次立即推送一次当前状态，之后状态 / `phase` / `frames_done` / `frames_total` 变化时推送
- 任务进入终态（`succeeded` / `failed` / `interrupted`）后推送最终状态并结束流
- 客户端断开（`context.cancel`）即停止推送，不影响任务执行
- 任务不存在 → 首个消息前 `NOT_FOUND`

---

## 9. 错误码汇总

| 状态码 | 触发场景 |
|--------|---------|
| `INVALID_ARGUMENT` | job_id 非 32 位 hex；request_id 为空；非法 shape / 越界参数 |
| `NOT_FOUND` | job_id 不存在；artifact_id 不属于任务；产物已被保留期清理（`artifacts_released`）|
| `ALREADY_EXISTS` | 同 request_id 但参数不同 |
| `FAILED_PRECONDITION` | 任务尚未 succeeded 就尝试下载 |
| `OUT_OF_RANGE` | DownloadArtifact 的 offset 超过文件大小 |
| `RESOURCE_EXHAUSTED` | 队列满；磁盘低于水位；单任务下载并发超上限 |
| `UNAVAILABLE` | 引擎未就绪（initializing / failed）|
| `INTERNAL` | 未预期的服务端错误 |

---

## 附录 A：Python gRPC 完整调用链

```python
import grpc, sys, time, hashlib, uuid
sys.path.insert(0, "generated")
from proto.isaacsim_rpc.v1 import simulation_pb2, simulation_pb2_grpc

ch = grpc.insecure_channel("127.0.0.1:18500")
stub = simulation_pb2_grpc.SimulationServiceStub(ch)

# 1. 健康检查 + 契约版本断言
h = stub.Health(simulation_pb2.HealthRequest())
assert h.status == "ready" and h.api_version.startswith("1.")
print(f"GPU={h.physical_gpu}  renderer={h.renderer}  free={h.artifacts_free_bytes}")

# 2. 构造仿真参数（capture=every 20，自动逐帧渲染）
sim = simulation_pb2.SimulateRequest(
    steps=120, dt=1.0/120, gravity=9.81, ground=True,
    bodies=[
        simulation_pb2.BodySpec(id="blue-cube", shape="cube",
            position=simulation_pb2.Vec3(x=-0.25, y=0.0, z=1.2),
            size=simulation_pb2.Vec3(x=0.2, y=0.2, z=0.2), mass=1.0,
            color=simulation_pb2.Vec3(x=0.15,y=0.4,z=0.8)),
        simulation_pb2.BodySpec(id="red-sphere", shape="sphere",
            position=simulation_pb2.Vec3(x=0.25, y=0.0, z=0.8),
            radius=0.13, mass=1.0, color=simulation_pb2.Vec3(x=0.8,y=0.2,z=0.15)),
    ],
    capture=simulation_pb2.CaptureSpec(every=20),
)
# 3. 带幂等键提交
handle = stub.SubmitJob(simulation_pb2.SubmitJobRequest(request_id=uuid.uuid4().hex, simulation=sim))
job_id = handle.id

# 4. 轮询（或改用 WatchJob 推送）
job = None
for _ in range(600):
    job = stub.GetJob(simulation_pb2.GetJobRequest(job_id=job_id))
    if job.state == "running":
        print(f"  {job.phase} {job.frames_done}/{job.frames_total}")
        time.sleep(0.5); continue
    break

# 5. 拿产物清单并下载尾帧 render.png，校验 sha256
assert job.state == "succeeded", job.error
render = next(a for a in job.artifacts if a.name == "render.png")
hasher = hashlib.sha256()
with open("render.png", "wb") as out:
    for chunk in stub.DownloadArtifact(simulation_pb2.DownloadArtifactRequest(
            job_id=job_id, artifact_id=render.artifact_id, offset=0, chunk_bytes=0)):
        out.write(chunk.data); hasher.update(chunk.data)
assert hasher.hexdigest() == render.sha256, "校验失败"

# 6. 全部落盘校验后，标记 Ack 确认（仅标记，不删文件；保留期到期后台自动清理）
ack = stub.AckJob(simulation_pb2.AckJobRequest(job_id=job_id))
print(f"Ack 确认 released={ack.released} freed_bytes={ack.freed_bytes}（新语义均为 false/0）")
```

## 附录 B：CLI 快捷命令

```bash
cd /mnt/data/zhouwei/IsaacSim4A100/rtx4090_grpc

# 健康检查
.venv/bin/python -m client.cli health

# 提交并等待（capture=every 20，自动逐帧渲染）；--request-id 缺省自动生成
.venv/bin/python -m client.cli run --scene cube --capture-every 20 --request-id <32hex>

# 指定帧号
.venv/bin/python -m client.cli run --scene cube --capture-frames 10,50,120

# 查询（含产物清单 artifacts、phase、frames_done/frames_total）
.venv/bin/python -m client.cli status --job-id <JOB_ID>

# 列出产物清单
.venv/bin/python -m client.cli list --job-id <JOB_ID>

# 下载单个产物（按 artifact_id / name / relative_path），自动校验 sha256
.venv/bin/python -m client.cli download --job-id <JOB_ID> render.png --out-dir ./out

# 客户端校验落盘后，标记 Ack 确认（仅标记，不删文件；保留期到期后台自动清理）
.venv/bin/python -m client.cli ack --job-id <JOB_ID>

# 取消排队 / 运行中的任务
.venv/bin/python -m client.cli cancel --job-id <JOB_ID>

# 停服务
bash shutdown.sh
```
