import logging
from collections.abc import Callable, Sequence

from fastapi import FastAPI, Response, status

log = logging.getLogger("app")

# A readiness check returns True when its dependency is usable. The Postgres
# check is added with the link endpoints; none are configured yet.
ReadinessCheck = Callable[[], bool]


def create_app(readiness_checks: Sequence[ReadinessCheck] = ()) -> FastAPI:
    app = FastAPI(title="link-shortener")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        # Liveness: the process is up. Deliberately checks no dependency, so a
        # database outage cannot make Kubernetes restart healthy pods.
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(response: Response) -> dict[str, str]:
        # Readiness: take the pod out of the Service while a dependency is down.
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

    return app


app = create_app()
