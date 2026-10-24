import logging
import os
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from prometheus_client import REGISTRY, CollectorRegistry
from pydantic import AfterValidator, AnyHttpUrl, BaseModel

from app import links
from app.links import (
    CODE_PATTERN,
    CodeCollision,
    InMemoryLinkStore,
    LinkStore,
    StoreUnavailable,
    TimeBoundedStore,
)
from app.metrics import RequestMetricsMiddleware, make_request_histogram, start_metrics_server

log = logging.getLogger("app")

# A readiness check returns True when its dependency is usable.
ReadinessCheck = Callable[[], bool]

MAX_URL_LENGTH = 2048
METRICS_PORT_DEFAULT = 9000


def _max_length(url: AnyHttpUrl) -> AnyHttpUrl:
    if len(str(url)) > MAX_URL_LENGTH:
        raise ValueError(f"URL longer than {MAX_URL_LENGTH} characters")
    return url


class LinkIn(BaseModel):
    # AnyHttpUrl accepts only http and https, which rules out javascript: and
    # other schemes that would turn a redirect into an attack.
    url: Annotated[AnyHttpUrl, AfterValidator(_max_length)]


class LinkOut(BaseModel):
    code: str
    url: str
    created_at: datetime


Code = Annotated[str, Path(pattern=CODE_PATTERN)]


def create_app(
    readiness_checks: Sequence[ReadinessCheck] = (),
    store: LinkStore | None = None,
    lifespan: Callable[[FastAPI], AsyncIterator[None]] | None = None,
    registry: CollectorRegistry | None = None,
) -> FastAPI:
    store = store if store is not None else InMemoryLinkStore()
    app = FastAPI(title="link-shortener", lifespan=lifespan)
    # A private registry by default: metrics can only be registered once per
    # registry, and tests build many apps. Production passes the global one.
    histogram = make_request_histogram(registry if registry is not None else CollectorRegistry())
    app.add_middleware(RequestMetricsMiddleware, histogram=histogram)

    @app.exception_handler(StoreUnavailable)
    async def store_unavailable(_: Request, exc: StoreUnavailable) -> JSONResponse:
        log.warning("store unavailable: %s", exc)
        return JSONResponse({"detail": "storage unavailable"}, status_code=503)

    # Health routes are registered before the redirect route: "healthz" and
    # "readyz" would otherwise also match a 7-character code path.
    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        # Liveness: the process is up. Deliberately checks no dependency, so a
        # database outage cannot make Kubernetes restart healthy pods.
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(response: Response) -> dict[str, str]:
        # Readiness: should THIS pod get traffic? Only pod-local checks count.
        # The database is shared by every replica, so failing readiness on it
        # would take all of them out of the Service at once: clients would get
        # refused connections instead of fast 503s, and the SLI would not see
        # them (ADR 0016, measured in issue #53).
        for check in readiness_checks:
            try:
                ok = check()
            except Exception:
                log.exception("readiness check raised")
                ok = False
            if not ok:
                response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
                return {"status": "not ready"}
        return {"status": "ready"}

    @app.post("/api/links", status_code=status.HTTP_201_CREATED, response_model=LinkOut)
    def create_link(body: LinkIn, response: Response) -> LinkOut:
        url = str(body.url)
        for _ in range(links.MAX_CREATE_ATTEMPTS):
            try:
                link = store.create(links.generate_code(), url)
            except CodeCollision:
                continue
            response.headers["Location"] = f"/api/links/{link.code}"
            return LinkOut(code=link.code, url=link.url, created_at=link.created_at)
        raise RuntimeError(f"no free code after {links.MAX_CREATE_ATTEMPTS} attempts")

    @app.get("/api/links/{code}", response_model=LinkOut)
    def get_link(code: Code) -> LinkOut:
        link = store.get(code)
        if link is None:
            raise HTTPException(status_code=404, detail="not found")
        return LinkOut(code=link.code, url=link.url, created_at=link.created_at)

    @app.get("/{code}")
    def redirect(code: str) -> RedirectResponse:
        # Validated here, not with Path(pattern=...), so a malformed code is a
        # plain 404 rather than a 422 that describes the code format.
        if not links.is_valid_code(code):
            raise HTTPException(status_code=404, detail="not found")
        link = store.get(code)
        if link is None:
            raise HTTPException(status_code=404, detail="not found")
        # 302 and no-store: every visit reaches the server (the redirect SLO,
        # ADR 0008, measures it) and the target can change later.
        return RedirectResponse(
            link.url,
            status_code=status.HTTP_302_FOUND,
            headers={"Cache-Control": "no-store"},
        )

    return app


def create_production_app() -> FastAPI:
    # Imported here so tests that never touch Postgres do not need psycopg.
    from app.postgres import READY_TIMEOUT, PostgresLinkStore, make_pool

    pool = make_pool()
    pg_store = PostgresLinkStore(pool)
    # Backstop for driver calls that can block with no timeout of their own.
    # The pool's own timeout usually fires first with a clean error.
    store = TimeBoundedStore(pg_store, timeout=pool.timeout, ready_timeout=READY_TIMEOUT + 0.5)
    metrics_port = int(os.environ.get("METRICS_PORT", METRICS_PORT_DEFAULT))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Started here, not at import, so importing the module binds no port.
        _, stop_metrics = start_metrics_server(metrics_port, registry=REGISTRY)
        # wait=False: the process starts and becomes ready even if Postgres is
        # down; requests answer 503 until it is back. This first check only
        # gives the schema a head start; the first request creates it otherwise.
        pool.open(wait=False)
        if not store.ready():
            log.warning("postgres not reachable at startup; requests will return 503 until it is")
        yield
        pool.close()
        stop_metrics()

    # Pod-local readiness: the pool is open. It closes during shutdown, which
    # takes the pod out of the Service before it stops serving.
    # The global registry also carries process metrics (memory, CPU, open fds).
    return create_app(
        readiness_checks=[lambda: not pool.closed],
        store=store,
        lifespan=lifespan,
        registry=REGISTRY,
    )


app = create_production_app()
