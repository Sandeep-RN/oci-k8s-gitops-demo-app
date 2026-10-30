# A database that is restarting often accepts TCP connections but never answers
# the handshake. These tests use such a "black hole" so that requests have to
# fail fast on their own budget, not because the connection was refused.
import socket
import time

import pytest
from fastapi.testclient import TestClient

from app.links import StoreUnavailable
from app.main import create_app
from app.postgres import POOL_TIMEOUT_DEFAULT, READY_TIMEOUT, PostgresLinkStore, make_pool
from tests.helpers import AUTH, TOKENS


@pytest.fixture
def blackhole():
    """Listens, never accepts or replies: connects succeed at TCP level and hang."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(64)
    yield s.getsockname()[1]
    s.close()


@pytest.fixture
def hung_store(blackhole):
    pool = make_pool(conninfo=f"host=127.0.0.1 port={blackhole} user=u dbname=d")
    pool.open(wait=False)
    yield PostgresLinkStore(pool)
    pool.close(timeout=1)


def test_readiness_gives_up_within_its_own_budget(hung_store):
    start = time.monotonic()
    assert hung_store.ready() is False
    assert time.monotonic() - start < READY_TIMEOUT + 0.5


def test_readyz_stays_ready_while_the_database_hangs(hung_store):
    # ADR 0016: every replica shares the database, so failing readiness on it
    # would empty the Service and turn fast 503s into refused connections.
    client = TestClient(create_app(store=hung_store))
    start = time.monotonic()
    r = client.get("/readyz")
    assert r.status_code == 200
    assert time.monotonic() - start < 0.5


def test_api_calls_return_503_within_the_pool_timeout(hung_store):
    client = TestClient(create_app(store=hung_store, tokens=TOKENS), follow_redirects=False)
    for call in (
        lambda: client.get("/AAAAAAA"),
        lambda: client.post("/api/links", json={"url": "https://example.com"}, headers=AUTH),
    ):
        start = time.monotonic()
        assert call().status_code == 503
        assert time.monotonic() - start < POOL_TIMEOUT_DEFAULT + 1.0


def test_pool_timeout_default_is_short():
    # Anything above a few seconds makes users and probes wait on an outage.
    assert POOL_TIMEOUT_DEFAULT <= 2.0
    assert READY_TIMEOUT < POOL_TIMEOUT_DEFAULT


def test_pool_timeout_is_configurable(monkeypatch, blackhole):
    monkeypatch.setenv("PG_POOL_TIMEOUT", "0.5")
    pool = make_pool(conninfo=f"host=127.0.0.1 port={blackhole} user=u dbname=d")
    pool.open(wait=False)
    try:
        # A request path uses the pool timeout (readiness has its own budget).
        start = time.monotonic()
        with pytest.raises(StoreUnavailable):
            PostgresLinkStore(pool).get("AAAAAAA")
        elapsed = time.monotonic() - start
        assert 0.4 < elapsed < 1.0  # ~0.5 s, well under the 2 s default
    finally:
        pool.close(timeout=1)


def test_invalid_pool_timeout_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("PG_POOL_TIMEOUT", "not-a-number")
    pool = make_pool(conninfo="host=127.0.0.1 port=1")
    assert pool.timeout == POOL_TIMEOUT_DEFAULT


# --- A call that never returns (e.g. a pooled connection to a frozen server) ---
# The driver can block with no timeout of its own, so the API puts a hard limit
# around every store call and answers 503 on schedule regardless.
from app.links import InMemoryLinkStore, TimeBoundedStore  # noqa: E402


class StuckStore(InMemoryLinkStore):
    def __init__(self, delay: float):
        super().__init__()
        self.delay = delay

    def get(self, code):
        time.sleep(self.delay)
        return super().get(code)

    def create(self, code, url, created_by=None):
        time.sleep(self.delay)
        return super().create(code, url, created_by)

    def ready(self):
        time.sleep(self.delay)
        return True


def test_time_bounded_store_raises_instead_of_hanging():
    store = TimeBoundedStore(StuckStore(delay=5), timeout=0.3, ready_timeout=0.2)
    start = time.monotonic()
    with pytest.raises(StoreUnavailable):
        store.get("AAAAAAA")
    with pytest.raises(StoreUnavailable):
        store.create("AAAAAAA", "https://a.example")
    assert store.ready() is False
    assert time.monotonic() - start < 1.5


def test_time_bounded_store_passes_through_fast_calls():
    store = TimeBoundedStore(InMemoryLinkStore(), timeout=1, ready_timeout=1)
    link = store.create("AAAAAAA", "https://a.example")
    assert store.get("AAAAAAA") == link
    assert store.get("BBBBBBB") is None
    assert store.ready() is True


def test_stuck_calls_cannot_exhaust_the_caller():
    # Every worker stuck: new calls must still fail fast instead of queueing.
    store = TimeBoundedStore(StuckStore(delay=3), timeout=0.2, ready_timeout=0.2, workers=2)
    start = time.monotonic()
    for _ in range(6):
        with pytest.raises(StoreUnavailable):
            store.get("AAAAAAA")
    assert time.monotonic() - start < 2.0


def test_api_returns_503_on_schedule_when_the_store_hangs():
    store = TimeBoundedStore(StuckStore(delay=5), timeout=0.3, ready_timeout=0.2)
    client = TestClient(create_app(store=store), follow_redirects=False)
    start = time.monotonic()
    assert client.get("/AAAAAAA").status_code == 503
    assert client.get("/readyz").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert time.monotonic() - start < 1.5
