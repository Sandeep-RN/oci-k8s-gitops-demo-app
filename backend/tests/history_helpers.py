"""Fakes and builders for the history tests (demo #235, ADR 0027).

Every name, address and SHA below is made up. The labels with pod, node and
address text are there on purpose: the page must never let them through.
"""

import io
import json
import threading
from collections.abc import Callable, Mapping, Sequence

from app import history, status
from app.status import QueryError, RangeSeries

# A fixed "now", aligned to every step in use (300, 900 and 1800 s).
NOW = 1_795_000_000 - (1_795_000_000 % 3600)
SHA_A = "0123456789abcdef0123456789abcdef01234567"
SHA_B = "fedcba9876543210fedcba9876543210fedcba98"
SHA_C = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaabbbbbbbb"


def image(sha: str) -> str:
    return f"registry.example.invalid/team/app/backend:{sha}"


def spec(key: str) -> tuple[str, str, int, int]:
    (found,) = [q for q in history.RANGE_QUERIES if q[0] == key]
    return found


def grid(key: str, now: float = NOW) -> tuple[float, float, float]:
    """(start, end, step) as the service will compute them for `key`."""
    _, _, window, step = spec(key)
    end = (now // step) * step
    return end - window, end, step


def points_for(key: str, values: Sequence[float | None], now: float = NOW):
    """A list of (ts, value) over the key's grid; None leaves a gap."""
    start, _, step = grid(key, now)
    return [(start + i * step, v) for i, v in enumerate(values) if v is not None]


def full_series(key: str, fn: Callable[[int], float | None] = lambda i: 0.5, now: float = NOW):
    """One RangeSeries covering the whole window of `key`."""
    start, end, step = grid(key, now)
    n = int((end - start) // step) + 1
    pts = tuple((start + i * step, fn(i)) for i in range(n) if fn(i) is not None)
    return RangeSeries(labels={"instance": "10.0.0.9:9100"}, points=pts)


def deploy_series(now: float = NOW) -> list[RangeSeries]:
    """Three backend images over the 7-day window, two pods each time (made up)."""
    start, end, step = grid("deploys", now)
    n = int((end - start) // step) + 1
    third = n // 3

    def one(sha: str, pod: str, a: int, b: int) -> RangeSeries:
        return RangeSeries(
            labels={"image": image(sha), "pod": pod, "node": "node-1.internal.invalid"},
            points=tuple((start + i * step, 1.0) for i in range(a, b)),
        )

    return [
        one(SHA_A, "backend-aaa-pod1", 0, third + 2),
        one(SHA_A, "backend-aaa-pod2", 0, third + 2),
        one(SHA_B, "backend-bbb-pod1", third, 2 * third + 2),
        one(SHA_B, "backend-bbb-pod2", third, 2 * third + 2),
        one(SHA_C, "backend-ccc-pod1", 2 * third, n),
        one(SHA_C, "backend-ccc-pod2", 2 * third, n),
    ]


def default_range_answers(now: float = NOW) -> dict[str, list[RangeSeries]]:
    shapes = {
        "budget": lambda i: 0.98 - i * 1e-5,
        "errors": lambda i: 0.0012,
        "latency": lambda i: 0.9987,
        "rps": lambda i: 0.03,
        "cpu": lambda i: 0.1 + (i % 12) * 0.01,
        "mem": lambda i: 0.35 + i * 1e-4,
        "net": lambda i: 4000.0 + i,
        "pods": lambda i: 41.0,
    }
    answers = {spec(k)[1]: [full_series(k, fn, now)] for k, fn in shapes.items()}
    answers[spec("deploys")[1]] = deploy_series(now)
    return answers


class FakeRange:
    """A query_range callable with canned answers keyed by PromQL.

    An Exception value is raised. `calls` records (promql, start, end, step)."""

    def __init__(self, answers: Mapping[str, object] | None = None) -> None:
        self.answers: dict[str, object] = dict(
            answers if answers is not None else default_range_answers()
        )
        self.calls: list[tuple[str, float, float, float]] = []
        self._lock = threading.Lock()

    def __call__(self, promql: str, start: float, end: float, step: float):
        with self._lock:
            self.calls.append((promql, start, end, step))
        answer = self.answers[promql]
        if isinstance(answer, BaseException):
            raise answer
        return list(answer)  # type: ignore[arg-type]

    def fail(self, key: str) -> None:
        self.answers[spec(key)[1]] = QueryError("prometheus down")

    def fail_all(self) -> None:
        for q in list(self.answers):
            self.answers[q] = QueryError("prometheus down")


# ---- raw HTTP bodies for the client tests -------------------------------------------


def matrix(*series, status_="success", result_type="matrix") -> bytes:
    return json.dumps(
        {"status": status_, "data": {"resultType": result_type, "result": list(series)}}
    ).encode()


def mseries(start: float, step: float, values: Sequence[object], **labels) -> dict:
    """A matrix series; a None value is left out (a gap). Values are written as text."""
    return {
        "metric": labels,
        "values": [[start + i * step, v if isinstance(v, str) else repr(v)]
                   for i, v in enumerate(values) if v is not None],
    }  # fmt: skip


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeOpener:
    def __init__(self, body=b"", error=None, status_code=200):
        self.body = body
        self.error = error
        self.status_code = status_code
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        response = FakeResponse(self.body)
        response.status = self.status_code
        return response


assert status.MAX_RESPONSE_BYTES == 64 * 1024  # the instant-query cap must not move
