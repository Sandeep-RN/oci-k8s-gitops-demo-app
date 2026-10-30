"""PostgreSQL-backed link store.

Connection settings come from the standard libpq environment variables
(PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE), so Kubernetes can inject the
password from a Secret without any app-specific config format.
"""

import logging
import os

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
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text
)
"""

# Tables created before the token gate (ADR 0020) lack created_by. It is added
# only when missing: ADD COLUMN takes an ACCESS EXCLUSIVE lock even with
# IF NOT EXISTS, and this runs on every pod start. Nullable with no default, so
# adding it is a catalog change, not a table rewrite. At scale, schema changes
# belong in a migration tool run once per release, not in the app.
_HAS_CREATED_BY = """
SELECT 1 FROM information_schema.columns
WHERE table_schema = current_schema() AND table_name = 'links' AND column_name = 'created_by'
"""
_ADD_CREATED_BY = "ALTER TABLE links ADD COLUMN created_by text"

_COLUMNS = "code, url, created_at, created_by"

# How long a request waits for a free connection before failing with a 503.
# Short on purpose: during an outage users and probes should get a quick error,
# not a hung request. Override with PG_POOL_TIMEOUT (seconds).
POOL_TIMEOUT_DEFAULT = 2.0
# Readiness gets a tighter budget than requests, so it always answers inside
# the kubelet probe's own timeout (set explicitly in the Deployment).
READY_TIMEOUT = 1.0

# Errors that mean "the database is not usable right now" rather than a bug.
_UNAVAILABLE = (psycopg.OperationalError, PoolTimeout)


def _pool_timeout() -> float:
    raw = os.environ.get("PG_POOL_TIMEOUT")
    if raw is None:
        return POOL_TIMEOUT_DEFAULT
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError(raw)
        return value
    except ValueError:
        log.warning("ignoring invalid PG_POOL_TIMEOUT=%r", raw)
        return POOL_TIMEOUT_DEFAULT


def make_pool(conninfo: str = "", timeout: float | None = None) -> ConnectionPool:
    """Create a closed pool. Empty conninfo means "use the PG* environment"."""
    timeout = _pool_timeout() if timeout is None else timeout
    return ConnectionPool(
        conninfo,
        min_size=1,
        max_size=5,
        timeout=timeout,  # wait for a free connection at most this long
        open=False,
        # Validate a connection before handing it out, so a restarted Postgres
        # does not leave dead sockets in the pool.
        check=ConnectionPool.check_connection,
        # A restarting Postgres can accept the TCP connection and then stay silent;
        # without this a connection attempt can block a pool worker for a long time.
        kwargs={
            "connect_timeout": 2,
            "application_name": "link-shortener",
            # If the server's pod disappears, packets are silently dropped and a
            # socket can wait for minutes. These make the kernel give up in a few
            # seconds, so threads stuck on a dead connection are released.
            "tcp_user_timeout": 5000,  # ms of unacknowledged data
            "keepalives": 1,
            "keepalives_idle": 5,
            "keepalives_interval": 2,
            "keepalives_count": 3,
        },
    )


class PostgresLinkStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        self._schema_ready = False

    def ensure_schema(self, timeout: float | None = None) -> None:
        try:
            with self._pool.connection(timeout=timeout) as conn, conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_KEY,))
                conn.execute(_SCHEMA)
                if conn.execute(_HAS_CREATED_BY).fetchone() is None:
                    log.info("adding links.created_by")
                    conn.execute(_ADD_CREATED_BY)
        except _UNAVAILABLE as e:
            raise StoreUnavailable(str(e)) from e
        self._schema_ready = True

    def _ensure_schema_once(self) -> None:
        # A pod that started while Postgres was down has no schema yet; its
        # first request that reaches the database creates it. Cheap afterwards.
        if not self._schema_ready:
            self.ensure_schema()

    def ready(self) -> bool:
        # "Is the database reachable right now?" Used at startup (and creates
        # the schema on success). Not a readiness probe: the database is shared
        # by every replica, so /readyz must not depend on it (ADR 0016).
        try:
            if not self._schema_ready:
                self.ensure_schema(timeout=READY_TIMEOUT)
            with self._pool.connection(timeout=READY_TIMEOUT) as conn:
                conn.execute("SELECT 1")
            return True
        except (StoreUnavailable, *_UNAVAILABLE) as e:
            # One line, no traceback: the probe runs every few seconds during
            # an outage and a stack trace per probe would flood the logs.
            log.warning("postgres not ready: %s", e)
            return False

    def create(self, code: str, url: str, created_by: str | None = None) -> Link:
        try:
            self._ensure_schema_once()
            with self._pool.connection() as conn:
                cur = conn.cursor(row_factory=class_row(Link))
                return cur.execute(
                    "INSERT INTO links (code, url, created_by) VALUES (%s, %s, %s)"
                    f" RETURNING {_COLUMNS}",
                    (code, url, created_by),
                ).fetchone()
        except UniqueViolation as e:
            # The pool's connection context rolls back the failed transaction.
            raise CodeCollision(code) from e
        except _UNAVAILABLE as e:
            raise StoreUnavailable(str(e)) from e

    def get(self, code: str) -> Link | None:
        try:
            self._ensure_schema_once()
            with self._pool.connection() as conn:
                cur = conn.cursor(row_factory=class_row(Link))
                return cur.execute(
                    f"SELECT {_COLUMNS} FROM links WHERE code = %s", (code,)
                ).fetchone()
        except _UNAVAILABLE as e:
            raise StoreUnavailable(str(e)) from e
