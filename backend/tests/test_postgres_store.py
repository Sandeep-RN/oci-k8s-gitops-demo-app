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


def test_schema_is_created_on_first_use(store):
    # A pod that started while Postgres was down never ran ensure_schema; its
    # first request must create the table instead of failing (ADR 0016).
    with store._pool.connection() as conn:
        conn.execute("DROP TABLE links")
    fresh = PostgresLinkStore(store._pool)
    assert fresh.get("Abc1234") is None
    assert fresh.create("Abc1234", "https://example.com/y").code == "Abc1234"


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


# --- created_by (ADR 0020): which link-creation token made the link ---

_OLD_SCHEMA = """
CREATE TABLE links (
    code       text        PRIMARY KEY,
    url        text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
)
"""


def test_created_by_roundtrip(store):
    assert store.create("Abc1234", "https://a.example", created_by="k6").created_by == "k6"
    assert store.get("Abc1234").created_by == "k6"
    assert store.create("Def5678", "https://b.example").created_by is None


def test_created_by_on_a_fresh_table(store):
    with store._pool.connection() as conn:
        conn.execute("DROP TABLE links")
    fresh = PostgresLinkStore(store._pool)
    assert fresh.create("Abc1234", "https://a.example", created_by="owner").created_by == "owner"
    assert fresh.get("Abc1234").created_by == "owner"


@pytest.fixture
def restore_schema(store):
    # The old-schema test leaves a table without the CHECK constraints; drop it
    # so the next test's store recreates the current schema.
    yield
    with store._pool.connection() as conn:
        conn.execute("DROP TABLE IF EXISTS links")


def test_created_by_is_added_to_an_existing_table(store, restore_schema):
    # A database from before the token gate: links exist, the column does not.
    with store._pool.connection() as conn:
        conn.execute("DROP TABLE links")
        conn.execute(_OLD_SCHEMA)
        conn.execute("INSERT INTO links (code, url) VALUES ('Old1234', 'https://old.example')")
    upgraded = PostgresLinkStore(store._pool)
    old = upgraded.get("Old1234")
    assert old.url == "https://old.example"
    assert old.created_by is None
    assert upgraded.create("New1234", "https://new.example", created_by="k6").created_by == "k6"
    # Running the schema step again on the upgraded table changes nothing.
    PostgresLinkStore(store._pool).ensure_schema()
    assert upgraded.get("New1234").created_by == "k6"
