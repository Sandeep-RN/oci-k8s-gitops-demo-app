"""Request metrics for the redirect SLO (ADR 0008).

One histogram, labelled by method, route template and status code. The SLO
queries in the gitops repo use these names; renaming them breaks the alerts.

Metrics are served on their own port (start_metrics_server), never on the app
port: the app port is behind the public load balancer.
"""

import time
from collections.abc import Callable

from prometheus_client import REGISTRY, CollectorRegistry, Histogram, start_http_server
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# 0.2 is the latency SLO threshold. 2.0 and 2.5 bracket the pool timeout and the
# TimeBoundedStore backstop, so fail-fast 503s land in their own buckets.
REQUEST_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0, 2.5, 5.0)

# Requests that match no route share one label, so random scanner paths
# cannot create unbounded time series. Unknown methods are folded the same way.
UNMATCHED_ROUTE = "<unmatched>"
KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


def make_request_histogram(registry: CollectorRegistry) -> Histogram:
    return Histogram(
        "http_request_duration_seconds",
        "HTTP request duration, by method, route template and status code.",
        ["method", "route", "status"],
        buckets=REQUEST_DURATION_BUCKETS,
        registry=registry,
    )


class RequestMetricsMiddleware:
    """Pure ASGI middleware, so it sees the final status even for unhandled errors."""

    def __init__(self, app: ASGIApp, histogram: Histogram) -> None:
        self.app = app
        self.histogram = histogram

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        # Stays 500 if the app raises before sending a response; the outer
        # ServerErrorMiddleware then answers 500, which is what we record.
        status = 500

        async def send_and_capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_capture)
        finally:
            # The router stores the matched route in the shared scope dict.
            route = scope.get("route")
            method = scope["method"] if scope["method"] in KNOWN_METHODS else "OTHER"
            self.histogram.labels(
                method=method,
                route=getattr(route, "path", UNMATCHED_ROUTE),
                status=str(status),
            ).observe(time.perf_counter() - start)


def start_metrics_server(
    port: int, registry: CollectorRegistry = REGISTRY, addr: str = "0.0.0.0"
) -> tuple[int, Callable[[], None]]:
    """Serve /metrics in a background thread. Returns the bound port and a stop function."""
    server, thread = start_http_server(port, addr=addr, registry=registry)

    def stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    return server.server_port, stop
