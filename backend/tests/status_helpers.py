"""A fake Prometheus for the status tests (demo #230). No network, no real data.

The default values mirror the shape of the real answers; every name, address and
SHA below is made up. The pod and instance labels are there on purpose: the
endpoint must never let them through.
"""

import threading
from collections.abc import Mapping

from app import status
from app.status import Sample

SHA = "0123456789abcdef0123456789abcdef01234567"
IMAGE = f"registry.example.invalid/team/app/backend:{SHA}"


def _one(value: float, **labels: str) -> list[Sample]:
    return [Sample(labels=labels, value=value)]


def default_answers() -> dict[str, list[Sample]]:
    return {
        status.Q_ERROR_BUDGET: _one(0.987654321),
        status.Q_ERROR_RATIO: _one(0.001234567891),
        status.Q_LATENCY_GOOD: _one(0.9987654321),
        status.Q_REQUEST_RATE: _one(0.0333333333, instance="10.0.0.9:9000"),
        status.Q_NODE_CPU: _one(0.18234567),
        status.Q_NODE_MEMORY: _one(0.37456789),
        status.Q_NODE_NET_TX: _one(4712.6789),
        status.Q_PODS_RUNNING: _one(41.0),
        status.Q_BACKEND_IMAGE: [
            Sample(
                labels={
                    "image": IMAGE,
                    "pod": "backend-6d9f7c5b8-abcde",
                    "node": "node-1.internal.invalid",
                    "namespace": "link-shortener",
                    "container": "backend",
                },
                value=1.0,
            ),
            Sample(
                labels={"image": IMAGE, "pod": "backend-6d9f7c5b8-fghij", "container": "backend"},
                value=1.0,
            ),
        ],
        status.Q_BACKEND_START: _one(1_795_000_000.0, pod="backend-6d9f7c5b8-abcde"),
        # The Argo CD series do not exist yet in the real cluster; present here
        # so the happy path is tested. Empty is tested separately.
        status.Q_ARGOCD_APPS: [
            Sample(
                {"name": "secret-app-name", "sync_status": "Synced", "health_status": "Healthy"}, 1
            ),
            Sample({"name": "other", "sync_status": "Synced", "health_status": "Progressing"}, 1),
            Sample({"name": "third", "sync_status": "OutOfSync", "health_status": "Healthy"}, 1),
        ],
    }


class FakePrometheus:
    """A query callable with canned answers. An Exception value is raised."""

    def __init__(self, answers: Mapping[str, object] | None = None) -> None:
        self.answers: dict[str, object] = dict(
            answers if answers is not None else default_answers()
        )
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, promql: str) -> list[Sample]:
        with self._lock:
            self.calls.append(promql)
        answer = self.answers[promql]
        if isinstance(answer, BaseException):
            raise answer
        return list(answer)  # type: ignore[arg-type]

    def fail_all(self) -> None:
        for q in list(self.answers):
            self.answers[q] = status.QueryError("prometheus down")


class Clock:
    """A settable monotonic clock."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now
