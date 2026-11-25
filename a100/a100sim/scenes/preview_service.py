"""Local HTTP service for versioned scene previews."""

import argparse
import json
import math
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class StageChanged(Exception):
    pass


class PreviewServer(ThreadingHTTPServer):
    request_queue_size = 64
    daemon_threads = True

    def __init__(self, address, engine, sessions, capacity, cache_size):
        self.engine = engine
        self.sessions = sessions
        self.capacity = capacity
        self.slots = threading.BoundedSemaphore(capacity)
        self.lock = threading.Lock()
        self.cache = OrderedDict()
        self.cache_size = cache_size
        self.images = OrderedDict()
        self.inflight = {}
        self.image_bytes = 0
        self.image_budget = 128 * 1024 * 1024
        self.active = 0
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nRetry-After: 1\r\nContent-Length: 0\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        with self.lock:
            self.active += 1
        try:
            super().process_request(request, address)
        except BaseException:
            self.release_slot()
            raise

    def release_slot(self):
        with self.lock:
            self.active -= 1
        self.slots.release()

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.release_slot()

    def prepare(self, path, size, version):
        key = path, size, version
        with self.lock:
            pending = self.cache.get(key)
            if pending is not None:
                self.cache.move_to_end(key)
                owner = False
            else:
                owner = True
                pending = self.cache[key] = Future()
        if owner:
            try:
                scene = self.engine.prepare(path, size=size)
                if stage_version(path) != version:
                    raise StageChanged("Stage changed during preparation. Retry the request.")
                pending.set_result(scene)
                with self.lock:
                    for old_key in list(self.cache):
                        if len(self.cache) <= self.cache_size:
                            break
                        if self.cache[old_key].done():
                            self.cache.pop(old_key)
            except BaseException as exc:
                pending.set_exception(exc)
                with self.lock:
                    self.cache.pop(key, None)
                raise
        return pending.result(), owner

    def frame(self, scene, path, version, camera, yaw, size, spp):
        key = (path, version, camera, yaw, size, spp)
        with self.lock:
            png = self.images.get(key)
            if png is not None:
                self.images.move_to_end(key)
                return png, "hit"
            pending = self.inflight.get(key)
            owner = pending is None
            if owner:
                pending = self.inflight[key] = Future()
        if not owner:
            return pending.result(), "joined"
        try:
            png = self.engine.render(scene, camera, size=size, spp=spp, yaw_degrees=yaw)
            with self.lock:
                self.images[key] = png
                self.image_bytes += len(png)
                while self.image_bytes > self.image_budget:
                    _, evicted = self.images.popitem(last=False)
                    self.image_bytes -= len(evicted)
            pending.set_result(png)
            return png, "miss"
        except BaseException as exc:
            pending.set_exception(exc)
            raise
        finally:
            with self.lock:
                self.inflight.pop(key, None)


def view_key(view, cameras):
    if not isinstance(view, dict) or not isinstance(view.get("camera"), str):
        raise ValueError("Each view requires an authored camera path")
    camera = view["camera"]
    if camera not in cameras:
        raise ValueError("Unknown authored camera: " + camera)
    yaw = view.get("yaw_degrees", 0)
    if type(yaw) not in (int, float) or not math.isfinite(yaw) or abs(yaw) > 180:
        raise ValueError("yaw_degrees must be a finite angle between -180 and 180")
    return camera, float(yaw)


def stage_version(path):
    info = path.stat()
    return f"{info.st_dev:x}-{info.st_ino:x}-{info.st_size:x}-{info.st_mtime_ns:x}"


class Handler(BaseHTTPRequestHandler):
    server_version = "IsaacScenePreview/1"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def respond(self, status, content, mime="application/json", headers=None):
        if mime == "application/json":
            content = json.dumps(content, ensure_ascii=True).encode("ascii")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, str(value))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path != "/health":
            return self.respond(404, {"error": "Unknown endpoint"})
        with self.server.lock:
            active = self.server.active - 1
            prepared = sum(f.done() for f in self.server.cache.values())
        self.respond(200, {"status": "ok", "active": active, "capacity": self.server.capacity, "prepared": prepared})

    def do_POST(self):
        if self.path not in {"/v1/prepare", "/v1/preview"}:
            return self.respond(404, {"error": "Unknown endpoint"})
        started = time.perf_counter()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 2 or length > 4096:
                raise ValueError("JSON body must contain 2 to 4096 bytes")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            session = payload.get("session_id")
            if not isinstance(session, str) or not re.fullmatch(r"[a-f0-9]{32}", session):
                raise ValueError("Invalid session_id")
            camera = payload.get("camera")
            if self.path == "/v1/preview" and (not isinstance(camera, str) or not camera.startswith("/")):
                raise ValueError("Expected an authored absolute camera prim path")
            size = payload.get("size", 1024)
            spp = payload.get("spp", 4)
            if type(size) is not int or not 64 <= size <= 1024:
                raise ValueError("size must be an integer between 64 and 1024")
            if type(spp) is not int or not 1 <= spp <= 4:
                raise ValueError("spp must be an integer between 1 and 4")
            stage = self.server.sessions / session / "stage.usdc"
            version = stage_version(stage)
            if payload.get("version", version) != version:
                raise StageChanged("Requested stage version has changed")
            scene, built = self.server.prepare(stage, size, version)
            ready = time.perf_counter()
            if self.path == "/v1/prepare":
                views = payload.get("views")
                if views is None:
                    views = [{"camera": path} for path in scene.cameras]
                if not isinstance(views, list) or not 1 <= len(views) <= 64:
                    raise ValueError("prepare expects 1 to 64 views")
                keys = list(dict.fromkeys(view_key(v, scene.cameras) for v in views))
                modes = [self.server.frame(scene, stage, version, cam, yaw, size, spp)[1] for cam, yaw in keys]
                if stage_version(stage) != version:
                    raise StageChanged("Stage changed while preparing views")
                return self.respond(
                    200,
                    {
                        "version": version,
                        "built": built,
                        "views": len(keys),
                        "rendered": modes.count("miss"),
                        "seconds": time.perf_counter() - started,
                    },
                )
            camera, yaw = view_key(payload, scene.cameras)
            png, cache_status = self.server.frame(scene, stage, version, camera, yaw, size, spp)
            if stage_version(stage) != version:
                raise StageChanged("Stage changed while rendering. Retry the request.")
        except (ValueError, KeyError, TypeError) as exc:
            return self.respond(400, {"error": str(exc)})
        except FileNotFoundError:
            return self.respond(404, {"error": "Scene stage missing"})
        except StageChanged as exc:
            return self.respond(409, {"error": str(exc)})
        except Exception as exc:
            self.log_error("Preview failed: %r", exc)
            return self.respond(500, {"error": str(exc)})
        self.respond(
            200,
            png,
            "image/png",
            {
                "X-Scene-Version": version,
                "X-Preview-Cache": cache_status,
                "X-Preparation-Ms": round((ready - started) * 1000, 2),
                "X-Render-Ms": round((time.perf_counter() - ready) * 1000, 2),
            },
        )


def main():
    import atexit

    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", required=True, type=Path)
    parser.add_argument("--port", type=int, default=18325)
    parser.add_argument("--capacity", type=int, default=32)
    parser.add_argument("--cache-size", type=int, default=8)
    args = parser.parse_args()
    from a100sim.app import create_app

    application = create_app()
    atexit.register(application.close)
    import drjit as dr
    from a100sim.scenes.preview_renderer import PreviewEngine

    dr.set_thread_count(16)
    server = PreviewServer(("127.0.0.1", args.port), PreviewEngine(), args.sessions, args.capacity, args.cache_size)
    with server:
        server.serve_forever()


if __name__ == "__main__":
    main()
