"""Short links: the domain type, code generation and the store interface."""

import re
import secrets
import string
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

CODE_LENGTH = 7
CODE_ALPHABET = string.ascii_letters + string.digits  # base62
CODE_PATTERN = rf"^[0-9A-Za-z]{{{CODE_LENGTH}}}$"
# 62^7 ≈ 3.5e12 codes, so collisions are rare but still handled (unique key + retry).
MAX_CREATE_ATTEMPTS = 5

# Top-level paths a generated code must never equal, because those routes are
# matched before the redirect route.
RESERVED_CODES = frozenset({"healthz", "readyz"})


@dataclass(frozen=True)
class Link:
    code: str
    url: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Name of the link-creation token that made it (ADR 0020). None for links
    # created before the token gate existed.
    created_by: str | None = None


class CodeCollision(Exception):
    """The code is already taken."""


class StoreUnavailable(Exception):
    """The backing store cannot be reached; the API maps this to 503."""


class LinkStore(Protocol):
    def create(self, code: str, url: str, created_by: str | None = None) -> Link: ...

    def get(self, code: str) -> Link | None: ...

    def ready(self) -> bool: ...


_CODE_RE = re.compile(CODE_PATTERN)


def is_valid_code(code: str) -> bool:
    return bool(_CODE_RE.match(code))


def _random_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def generate_code() -> str:
    while True:
        code = _random_code()
        if code not in RESERVED_CODES:
            return code


class InMemoryLinkStore:
    """For tests and local runs without a database. Not shared across replicas."""

    def __init__(self) -> None:
        self._links: dict[str, Link] = {}
        self._lock = threading.Lock()

    def create(self, code: str, url: str, created_by: str | None = None) -> Link:
        with self._lock:
            if code in self._links:
                raise CodeCollision(code)
            link = Link(code=code, url=url, created_by=created_by)
            self._links[code] = link
            return link

    def get(self, code: str) -> Link | None:
        return self._links.get(code)

    def ready(self) -> bool:
        return True


class TimeBoundedStore:
    """Puts a hard time limit around every call to another store.

    A database driver can block with no timeout of its own, for example when a
    pooled connection points at a server that froze or vanished. Rather than
    trust every code path in the driver, each call runs on a worker thread and
    the caller gives up after `timeout`, answering with StoreUnavailable (a 503).
    Stuck workers are bounded: once all are busy, new calls fail immediately
    instead of queueing behind them.
    """

    def __init__(
        self,
        inner: LinkStore,
        timeout: float,
        ready_timeout: float,
        workers: int = 16,
    ) -> None:
        self._inner = inner
        self._timeout = timeout
        self._ready_timeout = ready_timeout
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="store")
        # One slot per worker; a slot is held until its call really finishes, so
        # abandoned (stuck) calls keep counting against the limit.
        self._slots = threading.BoundedSemaphore(workers)

    def _run(self, fn, *args, timeout: float):
        if not self._slots.acquire(blocking=False):
            raise StoreUnavailable("all store workers are busy or stuck")
        future: Future = self._executor.submit(fn, *args)
        future.add_done_callback(lambda _f: self._slots.release())
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            future.cancel()  # only helps if it never started
            raise StoreUnavailable(f"store call exceeded {timeout:g}s") from None

    def create(self, code: str, url: str, created_by: str | None = None) -> Link:
        return self._run(self._inner.create, code, url, created_by, timeout=self._timeout)

    def get(self, code: str) -> Link | None:
        return self._run(self._inner.get, code, timeout=self._timeout)

    def ready(self) -> bool:
        try:
            return bool(self._run(self._inner.ready, timeout=self._ready_timeout))
        except StoreUnavailable:
            return False
