# Error mapping in PostgresLinkStore, tested with a fake pool so no database is
# needed (the integration tests in test_postgres_store.py need PGHOST).
#
# Contract: anything that means "the database is not usable right now" becomes
# StoreUnavailable (HTTP 503). A bug in our own SQL or data must NOT be hidden
# behind a 503, so those errors keep propagating.
from contextlib import contextmanager

import psycopg
import pytest
from psycopg.errors import DataError, InsufficientPrivilege
from psycopg_pool import PoolClosed, PoolTimeout

from app.links import StoreUnavailable
from app.postgres import PostgresLinkStore

UNAVAILABLE_ERRORS = [
    psycopg.OperationalError("server closed the connection"),
    PoolTimeout("couldn't get a connection"),
    PoolClosed("the pool is closed"),
    psycopg.InterfaceError("the connection is closed"),
]


class _Conn:
    """Connection whose execute() raises `error` (or succeeds when it is None)."""

    def __init__(self, error: Exception | None) -> None:
        self._error = error

    def execute(self, *_args, **_kwargs):
        if self._error is not None:
            raise self._error
        return self  # supports .fetchone() in the schema check

    def fetchone(self):
        return (1,)

    def cursor(self, **_kwargs):
        return self

    @contextmanager
    def transaction(self):
        yield


class FakePool:
    """Raises `on_connect` when a connection is requested, else hands out a
    connection whose statements raise `on_execute`."""

    def __init__(self, on_connect=None, on_execute=None) -> None:
        self._on_connect = on_connect
        self._on_execute = on_execute

    @contextmanager
    def connection(self, timeout=None):
        if self._on_connect is not None:
            raise self._on_connect
        yield _Conn(self._on_execute)


def store_failing_on_connect(error: Exception) -> PostgresLinkStore:
    s = PostgresLinkStore(FakePool(on_connect=error))
    s._schema_ready = True  # isolate the request path from the schema step
    return s


@pytest.mark.parametrize("error", UNAVAILABLE_ERRORS, ids=lambda e: type(e).__name__)
def test_get_maps_unavailable_errors(error):
    with pytest.raises(StoreUnavailable):
        store_failing_on_connect(error).get("Abc1234")


@pytest.mark.parametrize("error", UNAVAILABLE_ERRORS, ids=lambda e: type(e).__name__)
def test_create_maps_unavailable_errors(error):
    with pytest.raises(StoreUnavailable):
        store_failing_on_connect(error).create("Abc1234", "https://example.com")


@pytest.mark.parametrize("error", UNAVAILABLE_ERRORS, ids=lambda e: type(e).__name__)
def test_ensure_schema_maps_unavailable_errors(error):
    with pytest.raises(StoreUnavailable):
        PostgresLinkStore(FakePool(on_connect=error)).ensure_schema()


def test_a_failed_schema_step_is_unavailable_not_a_crash():
    # e.g. the app role lacks CREATE on the schema. The request must get a 503,
    # and the schema must stay "not ready" so the next request retries it.
    s = PostgresLinkStore(FakePool(on_execute=InsufficientPrivilege("permission denied")))
    with pytest.raises(StoreUnavailable):
        s.get("Abc1234")
    assert s._schema_ready is False


def test_ready_is_false_when_the_schema_step_fails():
    # The lifespan calls ready() at startup: it must return False, not raise,
    # or the pod would crash instead of starting and answering 503.
    s = PostgresLinkStore(FakePool(on_execute=InsufficientPrivilege("permission denied")))
    assert s.ready() is False


def test_ready_is_false_when_the_pool_is_closed():
    s = store_failing_on_connect(PoolClosed("the pool is closed"))
    assert s.ready() is False


def test_our_own_bugs_are_not_hidden_behind_a_503():
    # DataError means we sent bad data: that is a defect to see as a 500.
    s = PostgresLinkStore(FakePool(on_connect=DataError("bad value")))
    s._schema_ready = True
    with pytest.raises(DataError):
        s.get("Abc1234")
