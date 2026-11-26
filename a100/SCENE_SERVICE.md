# Retained scene capture on A100

This worker implements the registered scene operations used by BenchForge. The
existing `a100sim.v1.Simulator` gateway remains the only public interface. Kit,
CUDA PhysX and rendering run in the simulator worker, outside DSH and outside the
gateway Python environment.

Start the worker with the installed A100 runtime:

```bash
bash start-scenes.sh
```

`A100SIM_SCENE_GPU` selects its device (otherwise it inherits `ISAAC_GPU`). The
worker listens on loopback port 18324. Set
`A100SIM_SCENE_URL=http://127.0.0.1:18324` in the gateway environment and start
`serve-grpc.sh`. Supervise both processes with the deployment's service manager.
LLVM captures share a Dr.Jit thread budget of `capture_workers × capture_threads`.
A single capture can use up to `A100SIM_CAPTURE_BURST_THREADS` (default 32). Each
job keeps its assignment until it finishes. The pool divides free threads among
waiting snapshots and reserves a minimum for every unoccupied worker slot.
The health endpoint reports the budget, peak, guaranteed minimum, and current
allocation. Each job receipt records its assigned `capture_threads`. With 16
slots and 8 threads per slot, the budget is 128 and the late-arrival minimum is 6.

The scene worker needs the same asset root and result directories as the gateway.

Operations are discovered through `GetCapabilities` and submitted with
`SubmitTask.extension`: `asset.import`, `scene.edit`, `physics.step`,
`world.observe`, `scene.inspect`, `scene.capture`, `scene.export`, `scene.close`.
Use asset IDs and relative file names from `UploadAsset` for `asset.import.source`.
Subsequent operations take the returned `session_id`. Scene edits precede settling;
capture accepts up to 32 authored camera paths, 64–2048 square pixels, 1–1024 samples
per pixel and optional primary-surface annotations.

Scene state, measured velocities, jobs and artifacts are retained on disk. Each
physics step uses CUDA tensors. Every camera in a capture reads the same settled
stage. The result uses the existing gateway artifact handles and downloads.
Closing a session releases its use while retaining files; it does not delete them.

The renderer supports the registered office, mug and bin assets' explicit PBR
inputs, UV seams, face-varying normals, metallic/roughness textures and lamp emission.
Other MDL programs fail explicitly. Rendering uses the A100 CUDA adapter rather than
native RTX or Replicator. Primary-surface labels describe geometric intersections;
they do not simulate optical sensor noise or transmissive semantic labels.

The scene worker's loopback HTTP endpoint is an internal trusted transport. Public
asset resolution, request IDs, durable job ownership, authentication and artifact
authorization remain in the gRPC gateway. A worker restart retains settled scenes;
an interrupted accepted job is never submitted again automatically.
