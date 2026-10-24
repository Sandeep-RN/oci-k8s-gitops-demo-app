import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
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

log = logging.getLogger("app")

# A readiness check returns True when its dependency is usable.
ReadinessCheck = Callable[[], bool]

MAX_URL_LENGTH = 2048


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
) -> FastAPI:
    store = store if store is not None else InMemoryLinkStore()
    app = FastAPI(title="link-shortener", lifespan=lifespan)

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
        # Readiness: take the pod out of the Service while a dependency is down.
        for check in [*readiness_checks, store.ready]:
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

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # wait=False: the process starts (and passes liveness) even if Postgres
        # is down; readiness reports it and creates the schema once reachable.
        pool.open(wait=False)
        if not store.ready():
            log.warning("postgres not reachable at startup; will retry via /readyz")
        yield
        pool.close()

    return create_app(store=store, lifespan=lifespan)


app = create_production_app()
