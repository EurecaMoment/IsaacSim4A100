"""Serve the versioned simulator contract on a bounded asynchronous gRPC server."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection

from a100sim.rpc.backend import SimulatorBackend
from a100sim.rpc.config import Settings
from a100sim.rpc.errors import ServiceError
from a100sim.rpc.jobs import JobManager

LOGGER = logging.getLogger(__name__)


@asynccontextmanager
async def handle_errors(context: grpc.aio.ServicerContext) -> AsyncIterator[None]:
    """Translate expected boundary errors without leaking backend addresses or paths."""
    try:
        yield
    except ServiceError as error:
        await context.abort(error.code, str(error))
    except (ValueError, TypeError, KeyError) as error:
        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))
    except OSError:
        LOGGER.exception("RPC filesystem operation failed")
        await context.abort(grpc.StatusCode.INTERNAL, "File operation failed; inspect gateway logs")
    except Exception:
        LOGGER.exception("Unexpected RPC failure")
        await context.abort(grpc.StatusCode.INTERNAL, "Gateway operation failed; inspect gateway logs")


class TokenInterceptor(grpc.aio.ServerInterceptor):
    """Apply one operator-supplied bearer credential to all RPCs, including reflection."""

    def __init__(self, token: str) -> None:
        self.expected = ("Bearer " + token).encode()

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler | None]],
        details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler | None:
        """Reject unauthorized calls while preserving each method's streaming shape."""
        handler = await continuation(details)
        supplied = dict(details.invocation_metadata).get("authorization", "").encode()
        if handler is None or hmac.compare_digest(supplied, self.expected):
            return handler

        async def deny(_request: Any, context: grpc.aio.ServicerContext) -> None:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "A valid gateway bearer credential is required")

        async def deny_stream(request: Any, context: grpc.aio.ServicerContext) -> AsyncIterator[None]:
            await deny(request, context)
            if False:
                yield None

        key = (
            ("stream" if handler.request_streaming else "unary")
            + "_"
            + ("stream" if handler.response_streaming else "unary")
        )
        return handler._replace(**{key: deny_stream if handler.response_streaming else deny})




def build_server(
    manager: JobManager, listen: str, *, token: str = "", certificate: bytes = b"", private_key: bytes = b""
) -> tuple[grpc.aio.Server, int]:
    """Bind a server with reflection, standard health and bounded message/concurrency limits."""
    if bool(certificate) != bool(private_key):
        raise ValueError("TLS requires both certificate and private key")
    if not listen.startswith(("127.0.0.1:", "localhost:", "[::1]:")) and not (token and certificate):
        raise ValueError("A non-loopback listener requires TLS and A100SIM_GRPC_TOKEN")
    server = grpc.aio.server(
        interceptors=[TokenInterceptor(token)] if token else [],
        maximum_concurrent_rpcs=256,
        options=[
            ("grpc.max_receive_message_length", 4 * 1024 * 1024),
            ("grpc.max_send_message_length", 4 * 1024 * 1024),
        ],
    )
    from a100sim.rpc.unified import SERVICE, UnifiedService, rpc as shared_rpc

    shared_rpc.simulation_pb2_grpc.add_SimulationServiceServicer_to_server(UnifiedService(manager, handle_errors), server)
    checker = health.HealthServicer()
    checker.set("", health_pb2.HealthCheckResponse.SERVING)
    checker.set(SERVICE, health_pb2.HealthCheckResponse.SERVING)
    health_pb2_grpc.add_HealthServicer_to_server(checker, server)
    reflection.enable_server_reflection((SERVICE, health.SERVICE_NAME, reflection.SERVICE_NAME), server)
    port = (
        server.add_secure_port(listen, grpc.ssl_server_credentials(((private_key, certificate),)))
        if certificate
        else (server.add_insecure_port(listen))
    )
    if not port:
        raise RuntimeError("Could not bind the configured gRPC address")
    return server, port


async def serve(settings: Settings) -> None:
    """Run until interrupted, keeping SDK dependencies outside the simulator process."""
    backend_type = SimulatorBackend
    if os.environ.get("A100SIM_SCENE_URL"):
        from a100sim.rpc.scenes import SceneBackend
        backend_type = SceneBackend
    manager = JobManager(settings, backend_type(settings))
    certificate = (
        Path(os.environ["A100SIM_GRPC_TLS_CERT"]).read_bytes() if os.environ.get("A100SIM_GRPC_TLS_CERT") else b""
    )
    private_key = (
        Path(os.environ["A100SIM_GRPC_TLS_KEY"]).read_bytes() if os.environ.get("A100SIM_GRPC_TLS_KEY") else b""
    )
    server, _ = build_server(
        manager, settings.listen, token=settings.token, certificate=certificate, private_key=private_key
    )
    await manager.start()
    try:
        await server.start()
        LOGGER.info("Simulator gRPC gateway listening on %s", settings.listen)
        stopped = asyncio.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stopped.set())
        await stopped.wait()
    finally:
        await server.stop(5)
        await manager.close()


def main() -> None:
    """Launch the gateway using environment configuration and an optional bind override."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen")
    args = parser.parse_args()
    if args.listen:
        os.environ["A100SIM_GRPC_LISTEN"] = args.listen
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(serve(Settings.from_environment()))


if __name__ == "__main__":
    main()
