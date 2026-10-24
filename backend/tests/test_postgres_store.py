# Integration tests against a real PostgreSQL. They run when PGHOST is set
# (CI provides a service container; locally: see README "Run locally") and are
# skipped otherwise, so the fast unit suite needs no database.
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("PGHOST"), reason="PGHOST not set")

from app.links import CodeCollision, StoreUnavailable  # noqa: E402
from app.postgres import PostgresLinkStore, make_pool  # noqa: E402


@pytest.fixture
def store():
    pool = make_pool()
    pool.open(wait=True, timeout=10)
    s = PostgresLinkStore(pool)
    s.ensure_schema()
    with pool.connection() as conn:
        conn.execute("TRUNCATE links")
    yield s
    pool.close()


def test_roundtrip(store):
    link = store.create("Abc1234", "https://example.com/x")
    assert link.code == "Abc1234"
    got = store.get("Abc1234")
    assert got is not None
    assert got.url == "https://example.com/x"
    assert got.created_at is not None


def test_missing_code_returns_none(store):
    assert store.get("zzzzzzz") is None


def test_duplicate_code_raises_collision(store):
    store.create("Abc1234", "https://a.example")
    with pytest.raises(CodeCollision):
        store.create("Abc1234", "https://b.example")
    # The failed insert must not poison the pool's connection.
    assert store.get("Abc1234").url == "https://a.example"


def test_ensure_schema_is_idempotent_and_safe_concurrently(store):
    # Two replicas starting at once both run ensure_schema; the advisory lock
    # must make that safe.
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(lambda _: store.ensure_schema(), range(8)))
    assert store.ready() is True


def test_ready_is_false_and_errors_map_when_db_unreachable():
    pool = make_pool(conninfo="host=127.0.0.1 port=1 connect_timeout=1", timeout=2)
    pool.open(wait=False)
    s = PostgresLinkStore(pool)
    try:
        assert s.ready() is False
        with pytest.raises(StoreUnavailable):
            s.get("Abc1234")
    finally:
        pool.close()
