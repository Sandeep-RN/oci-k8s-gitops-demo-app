"""Link-creation tokens (ADR 0020).

Tokens live in a directory, one file per token: the file name is the token's
name, the file content is its value. A Kubernetes Secret mounted as a volume
produces exactly this layout, so adding or revoking one token is adding or
removing one key of one SealedSecret, without touching the others.

Token values are never logged or returned; only names are.
"""

import hashlib
import hmac
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

log = logging.getLogger("app.auth")

# Names are logged and stored with each link, so keep them to a plain set.
# Kubernetes Secret keys allow the same characters.
# Used with fullmatch: "$" would also accept a trailing newline (log injection).
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
# Shorter values are skipped: a guessable token would defeat the gate.
# `openssl rand -hex 32` gives 64 characters.
MIN_TOKEN_LENGTH = 32


class TokenSource(Protocol):
    def read(self) -> Mapping[str, str]:
        """Current tokens, name -> value."""
        ...


class NoTokens:
    def read(self) -> Mapping[str, str]:
        return {}


class DirectoryTokens:
    """Reads the directory on every call.

    Link creation is rare, so a few small file reads per create cost nothing,
    and a revoked token stops working as soon as the kubelet refreshes the
    mounted Secret (about a minute), with no pod restart. At high create rates,
    cache this with a short TTL.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        # The directory is read on every create; a bad file is reported once,
        # not on each request. A race between threads at worst logs it twice.
        self._reported: set[str] = set()

    def _warn_once(self, key: str, msg: str, *args: object) -> None:
        if key not in self._reported:
            self._reported.add(key)
            log.warning(msg, *args)

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> Mapping[str, str]:
        tokens: dict[str, str] = {}
        try:
            entries = sorted(self._path.iterdir())
        except OSError:
            # Missing or unreadable directory: no tokens, so every create is a
            # 401. Logged once at startup (main._log_tokens), not per request.
            return tokens
        for entry in entries:
            name = entry.name
            # A Secret volume also holds "..data" and timestamped dot-dirs.
            if name.startswith(".") or not entry.is_file():
                continue
            if not _NAME_RE.fullmatch(name):
                self._warn_once(f"name:{name}", "ignoring link token with an invalid name")
                continue
            try:
                value = entry.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                self._warn_once(f"read:{name}", "ignoring unreadable link token %s", name)
                continue
            if len(value) < MIN_TOKEN_LENGTH:
                self._warn_once(
                    f"short:{name}",
                    "ignoring link token %s: shorter than %d characters",
                    name,
                    MIN_TOKEN_LENGTH,
                )
                continue
            tokens[name] = value
        return tokens


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def match_token(presented: str, tokens: Mapping[str, str]) -> str | None:
    """Return the name of the token equal to `presented`, or None.

    Constant-time: digests have a fixed length, so compare_digest leaks neither
    the tokens' lengths nor how many leading characters matched, and every
    token is compared even after a match.
    """
    presented_digest = _digest(presented)
    matched: str | None = None
    for name, value in tokens.items():
        if hmac.compare_digest(presented_digest, _digest(value)) and matched is None:
            matched = name
    return matched
