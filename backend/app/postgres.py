"""PostgreSQL-backed link store.

Connection settings come from the standard libpq environment variables
(PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE), so Kubernetes can inject the
password from a Secret without any app-specific config format.
"""

import logging

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.rows import class_row
from psycopg_pool import ConnectionPool, PoolTimeout

from app.links import CODE_LENGTH, CodeCollision, Link, StoreUnavailable

log = logging.getLogger("app.postgres")

# Arbitrary constant key for pg_advisory_xact_lock, so concurrent replicas
# serialise schema creation instead of racing on the system catalogs.
_SCHEMA_LOCK_KEY = 74_521_001

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS links (
    code       text        PRIMARY KEY CHECK (code ~ '^[0-9A-Za-z]{{{CODE_LENGTH}}}$'),
    url        text        NOT NULL CHECK (length(url) <= 2048),
    created_at timestamptz NOT NULL DEFAULT now()
)
"""

# Errors that mean "the database is not usable right now" rather than a bug.
_UNAVAILABLE = (psycopg.OperationalError, PoolTimeout)


def make_pool(conninfo: str = "", timeout: float = 5.0) -> ConnectionPool:
    """Create a closed pool. Empty conninfo means "use the PG* environment"."""
    return ConnectionPool(
        conninfo,
        min_size=1,
        max_size=5,
        timeout=timeout,  # wait for a free connection at most this long
        open=False,
        # Validate a connection before handing it out, so a restarted Postgres
        # does not leave dead sockets in the pool.
        check=ConnectionPool.check_connection,
        kwargs={"connect_timeout": 3, "application_name": "link-shortener"},
    )


class PostgresLinkStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        self._schema_ready = False

    def ensure_schema(self) -> None:
        try:
            with self._pool.connection() as conn, conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_KEY,))
                conn.execute(_SCHEMA)
        except _UNAVAILABLE as e:
            raise StoreUnavailable(str(e)) from e
        self._schema_ready = True

    def ready(self) -> bool:
        # Readiness creates the schema on first success, so a pod that started
        # while Postgres was down becomes ready once it is reachable.
        try:
            if not self._schema_ready:
                self.ensure_schema()
            with self._pool.connection() as conn:
                conn.execute("SELECT 1")
            return True
        except (StoreUnavailable, *_UNAVAILABLE) as e:
            # One line, no traceback: the probe runs every few seconds during
            # an outage and a stack trace per probe would flood the logs.
            log.warning("postgres not ready: %s", e)
            return False

    def create(self, code: str, url: str) -> Link:
        try:
            with self._pool.connection() as conn:
                cur = conn.cursor(row_factory=class_row(Link))
                return cur.execute(
                    "INSERT INTO links (code, url) VALUES (%s, %s)"
                    " RETURNING code, url, created_at",
                    (code, url),
                ).fetchone()
        except UniqueViolation as e:
            # The pool's connection context rolls back the failed transaction.
            raise CodeCollision(code) from e
        except _UNAVAILABLE as e:
            raise StoreUnavailable(str(e)) from e

    def get(self, code: str) -> Link | None:
        try:
            with self._pool.connection() as conn:
                cur = conn.cursor(row_factory=class_row(Link))
                return cur.execute(
                    "SELECT code, url, created_at FROM links WHERE code = %s", (code,)
                ).fetchone()
        except _UNAVAILABLE as e:
            raise StoreUnavailable(str(e)) from e
