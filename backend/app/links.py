"""Short links: the domain type, code generation and the store interface."""

import re
import secrets
import string
import threading
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


class CodeCollision(Exception):
    """The code is already taken."""


class StoreUnavailable(Exception):
    """The backing store cannot be reached; the API maps this to 503."""


class LinkStore(Protocol):
    def create(self, code: str, url: str) -> Link: ...

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

    def create(self, code: str, url: str) -> Link:
        with self._lock:
            if code in self._links:
                raise CodeCollision(code)
            link = Link(code=code, url=url)
            self._links[code] = link
            return link

    def get(self, code: str) -> Link | None:
        return self._links.get(code)

    def ready(self) -> bool:
        return True
