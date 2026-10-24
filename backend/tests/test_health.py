from fastapi.testclient import TestClient

from app.links import InMemoryLinkStore
from app.main import create_app


def test_healthz_is_ok_without_dependencies():
    # Liveness must not depend on anything external, or a DB blip would make
    # Kubernetes restart healthy pods.
    client = TestClient(create_app(readiness_checks=[lambda: False]))
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_ok_when_all_checks_pass():
    client = TestClient(create_app(readiness_checks=[lambda: True, lambda: True]))
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ready"}


def test_readyz_ok_with_no_checks_configured():
    assert TestClient(create_app()).get("/readyz").status_code == 200


def test_readyz_503_when_a_check_fails():
    client = TestClient(create_app(readiness_checks=[lambda: True, lambda: False]))
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"status": "not ready"}


def test_readyz_ignores_the_link_store():
    # Store reachability is shared by every replica, so it is not a readiness
    # signal (ADR 0016). Only checks passed in explicitly count.
    class DownStore(InMemoryLinkStore):
        def ready(self) -> bool:
            raise RuntimeError("db down")

    r = TestClient(create_app(store=DownStore())).get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ready"}


def test_readyz_503_when_a_check_raises():
    def boom() -> bool:
        raise RuntimeError("db down")

    r = TestClient(create_app(readiness_checks=[boom])).get("/readyz")
    assert r.status_code == 503
