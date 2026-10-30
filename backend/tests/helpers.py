"""Shared test values. Link creation needs a token (ADR 0020); tests that are
about something else use this one. Fake value, never a real token."""

from collections.abc import Mapping

TEST_TOKEN = "test-token-0123456789abcdef0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


class StaticTokens:
    def __init__(self, tokens: Mapping[str, str]) -> None:
        self._tokens = dict(tokens)

    def read(self) -> Mapping[str, str]:
        return self._tokens


TOKENS = StaticTokens({"test": TEST_TOKEN})
