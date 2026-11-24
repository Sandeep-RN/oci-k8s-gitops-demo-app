"""History charts for GET /status (demo issue #235, ADR 0027).

A sibling of status.py with the same bar. A background thread asks Prometheus a
FIXED list of range queries, turns the answers into numbers on a regular time
grid, renders the page once, and keeps that in memory. The request path only
returns a string: it never calls Prometheus, so a slow or dead Prometheus can only
make the charts stale. It cannot slow, fail or restart the redirect service
(ADR 0016: this is not part of /healthz or /readyz).

Safety rules that the tests pin down:
- Every query, window and step is a constant in RANGE_QUERIES. No request data,
  and no variable, ever reaches a query.
- Prometheus has no authentication, so its answers are parsed as hostile: size and
  point caps, strict numbers, labels never copied to the output. The only label
  that is read is the backend image tag, and only after a 40-hex match, as 7
  characters.
- The feature is off unless STATUS_HISTORY=on AND STATUS_PROMETHEUS_URL is valid.
"""

import logging
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from prometheus_client import CollectorRegistry

from app import history_page, status
from app.history_model import Deploy, DeploysData, HistorySnapshot, SeriesData
from app.metrics import make_history_metrics
from app.status import PrometheusQuery, QueryError, RangeSeries

__all__ = ["Deploy", "DeploysData", "HistorySnapshot", "SeriesData"]  # re-exported models

log = logging.getLogger("app")

DAY = 86400

# ---- the allowlist (ADR 0027) ---------------------------------------------------------
# The memory chart wraps the strip's query in avg(): after a node-exporter pod is
# replaced Prometheus holds two series for a day, and a single-line chart refuses two
# (a merge would invent a number), so the chart would read "no data" for 24 h.
Q_NODE_MEMORY_RANGE = f"avg({status.Q_NODE_MEMORY})"

# (key, PromQL, window seconds, step seconds). The ONLY range queries this app ever
# sends, all constants, and no ad hoc range selector is longer than [5m]: the 7-day
# charts read RECORDING RULES (an ad hoc rate(...[7d]) over 7 days would be costly).
# The PromQL is shared with the instant queries of /api/status where it is the same.
RANGE_QUERIES: tuple[tuple[str, str, int, int], ...] = (
    ("budget", status.Q_ERROR_BUDGET, 7 * DAY, 1800),
    ("errors", status.Q_ERROR_RATIO, 7 * DAY, 1800),  # the chart shows 1 - value
    ("latency", status.Q_LATENCY_GOOD, 7 * DAY, 1800),
    ("rps", status.Q_REQUEST_RATE, DAY, 300),
    ("cpu", status.Q_NODE_CPU, DAY, 300),
    ("mem", Q_NODE_MEMORY_RANGE, DAY, 300),
    ("net", status.Q_NODE_NET_TX, DAY, 300),
    ("pods", status.Q_PODS_RUNNING, DAY, 300),
    # One series per backend pod; the image label's tag is the commit. 7 days at
    # 15 min: about 16 series and 744 points when measured.
    ("deploys", status.Q_BACKEND_IMAGE, 7 * DAY, 900),
)

# What each charted value can mean; anything outside is clamped, as /api/status does.
_BOUNDS: dict[str, tuple[float, float]] = {
    "budget": (0.0, 1.0),
    "errors": (0.0, 1.0),
    "latency": (0.0, 1.0),
    "rps": (0.0, 1e9),
    "cpu": (0.0, 1.0),
    "mem": (0.0, 1.0),
    "net": (0.0, 1e15),
    "pods": (0.0, 1e6),
}

RangeQuery = Callable[[str, float, float, float], list[RangeSeries]]

# ---- limits ---------------------------------------------------------------------------
DEFAULT_REFRESH_SECONDS = 300.0
MIN_REFRESH_SECONDS = 60.0  # below this the env value is refused: 9 queries per refresh
MAX_REFRESH_SECONDS = 900.0
# 9 queries one after another, each cut off at about 2.5 s (the client's own bound).
HISTORY_REFRESH_TIMEOUT = 25.0
MAX_DEPLOY_MARKS = 32  # the newest; a chart with more ticks is unreadable anyway
GRID_TOLERANCE = 1.0  # seconds; a point further than this from a step is not ours
# |value| below this is drawn as 0: subnormals (5e-324) are noise, and they break
# arithmetic further down (review M1).
NOISE_FLOOR = 1e-12


# ---- the snapshot ---------------------------------------------------------------------
@dataclass(frozen=True)
class _Failure:
    """What is kept of a failed query: a fixed message or an exception TYPE NAME,
    never the exception object (see the worker)."""

    message: str | None
    type_name: str = "QueryError"

    @property
    def text(self) -> str:
        return self.message if self.message is not None else self.type_name


class _Unusable(Exception):
    """An answer that cannot become a chart (not a failure of the whole refresh)."""


def _grid(start: float, end: float, step: float) -> int:
    return int(round((end - start) / step)) + 1


def _series_data(
    key: str, answer: Any, start: float, end: float, step: float, as_of: float
) -> SeriesData:
    if not isinstance(answer, list) or not all(isinstance(s, RangeSeries) for s in answer):
        raise _Unusable("not a list of series")
    if len(answer) > 1:
        # A merge would invent a number. A gap is honest.
        raise _Unusable("more than one series for a single line")
    low, high = _BOUNDS[key]
    values: list[float | None] = [None] * _grid(start, end, step)
    for s in answer:
        for ts, value in s.points:
            if not (math.isfinite(ts) and math.isfinite(value)):
                continue
            index = int(round((ts - start) / step))
            if 0 <= index < len(values) and abs(ts - (start + index * step)) <= GRID_TOLERANCE:
                value = 0.0 if abs(value) < NOISE_FLOOR else value
                values[index] = max(low, min(high, value))
    return SeriesData(start, end, step, tuple(values), as_of)


def _deploys_data(answer: Any, start: float, end: float, as_of: float) -> DeploysData:
    """Markers: every time a commit tag shows up that was absent at the sample before.

    A rollback A -> B -> A is two markers (B, then A again): first-seen-ever would
    miss the way back (review L4). A tag that is already there at the first sample was
    running before the data begins, so it is not a deploy and gets no marker. Only a
    full lowercase git SHA in the image's tag counts; anything else is ignored."""
    if not isinstance(answer, list) or not all(isinstance(s, RangeSeries) for s in answer):
        raise _Unusable("not a list of series")
    present: dict[float, set[str]] = {}  # sample time -> tags running then
    for s in answer:
        labels = s.labels if isinstance(s.labels, Mapping) else {}
        tag = status._tag(labels.get("image"))  # noqa: SLF001  (the 40-hex check lives there)
        if tag is None:
            continue
        for ts, _ in s.points:
            if math.isfinite(ts) and start <= ts <= end:
                present.setdefault(ts, set()).add(tag)
    marks: list[Deploy] = []
    previous: set[str] | None = None  # None at the first sample: all of those are "before"
    for ts in sorted(present):
        if previous is not None:
            # Sorted tags: two tags new at the same sample get a stable order.
            marks.extend(Deploy(ts=ts, short=tag[:7]) for tag in sorted(present[ts] - previous))
        previous = present[ts]
    return DeploysData(start, end, tuple(marks[-MAX_DEPLOY_MARKS:]), as_of)


@dataclass(frozen=True)
class _Current:
    snapshot: HistorySnapshot
    fresh_html: str
    stale_html: str
    at: float  # monotonic time of the success


# ---- the refresher --------------------------------------------------------------------
class HistoryService:
    """Holds the snapshot and its rendered page and refreshes them. `read()` and
    `page()` are O(1) and never block."""

    def __init__(
        self,
        query_range: RangeQuery,
        *,
        registry: CollectorRegistry | None = None,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
        stale_after: float | None = None,
        refresh_timeout: float = HISTORY_REFRESH_TIMEOUT,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._query_range = query_range
        self.refresh_seconds = refresh_seconds
        # Three missed refreshes: after that the page says its data is old.
        self.stale_after = stale_after if stale_after is not None else 3 * refresh_seconds
        self._refresh_timeout = refresh_timeout
        self._monotonic = monotonic
        self._wall = wall
        # Replaced whole: readers never see a half-written snapshot and need no lock.
        self._current: _Current | None = None
        self._flight = threading.Lock()  # single-flight: refreshes never overlap
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # The worker of the latest refresh. A new refresh does not start while it is
        # alive, so a Prometheus that never answers strands ONE thread, not one per
        # refresh (the rule of the review of PR #42, M1).
        self._workers: list[threading.Thread] = []
        self._failures, age = make_history_metrics(
            registry if registry is not None else CollectorRegistry()
        )
        age.set_function(self._age)

    def _age(self) -> float:
        # -1 means "never succeeded"; alerts on age > N then stay quiet at startup.
        current = self._current
        return -1.0 if current is None else self._monotonic() - current.at

    def read(self) -> HistorySnapshot | None:
        current = self._current
        return None if current is None else current.snapshot

    def page(self) -> str | None:
        """The rendered page, or None before the first successful refresh."""
        current = self._current
        if current is None:
            return None
        stale = (self._monotonic() - current.at) > self.stale_after
        return current.stale_html if stale else current.fresh_html

    def refresh_once(self) -> bool:
        """One refresh. True if the snapshot was replaced. Never raises."""
        if not self._flight.acquire(blocking=False):
            return False
        try:
            return self._refresh()
        except Exception:
            log.exception("history refresh failed unexpectedly")
            self._failures.inc()
            return False
        finally:
            self._flight.release()

    def _refresh(self) -> bool:
        if any(t.is_alive() for t in self._workers):
            log.warning("history refresh skipped: a query of the previous one is still running")
            self._failures.inc()
            return False
        wall = self._wall()
        jobs = []
        for key, promql, window, step in RANGE_QUERIES:
            end = (wall // step) * step  # a step boundary: stable timestamps, cache friendly
            jobs.append((key, promql, end - window, end, step))
        outcomes: dict[str, Any] = {}
        cutoff = threading.Event()  # set at the deadline: the worker starts no new query

        def work() -> None:
            for key, promql, start, end, step in jobs:
                if cutoff.is_set():
                    return
                try:
                    outcomes[key] = self._query_range(promql, start, end, step)
                except QueryError as exc:
                    # Never keep the exception itself: its traceback keeps the frame
                    # of the parser, which holds the parsed body. Nine of those at
                    # once were 28 MiB. A QueryError message is a fixed string.
                    outcomes[key] = _Failure(str(exc))
                except Exception as exc:
                    outcomes[key] = _Failure(None, type(exc).__name__)

        # One daemon thread (not a ThreadPoolExecutor: its threads are joined at
        # interpreter exit, so a stuck query would stop the pod exiting on SIGTERM).
        # A late finisher writes into THIS refresh's dict, which nothing reads after.
        worker = threading.Thread(target=work, name="history-query-0", daemon=True)
        self._workers = [worker]
        worker.start()
        deadline = time.monotonic() + self._refresh_timeout
        while worker.is_alive() and not self._stop.is_set() and time.monotonic() < deadline:
            worker.join(0.05)
        cutoff.set()
        if self._stop.is_set():
            return False  # shutting down: nothing is replaced and nothing is counted

        previous = self._current.snapshot.charts if self._current is not None else {}
        charts: dict[str, SeriesData | DeploysData | None] = {}
        failed = 0
        for key, _, start, end, step in jobs:
            outcome = outcomes.get(key)
            try:
                if key not in outcomes:  # still running, or never started
                    raise _Unusable("no answer in time")
                if isinstance(outcome, _Failure):
                    log.warning("history query %s failed: %s", key, outcome.text)
                    raise _Unusable("query failed")
                if key == "deploys":
                    charts[key] = _deploys_data(outcome, start, end, wall)
                else:
                    charts[key] = _series_data(key, outcome, start, end, step, wall)
            except _Unusable as exc:
                failed += 1
                log.debug("history chart %s unusable: %s", key, exc)
                charts[key] = previous.get(key)  # the last good one, or None
        if failed == len(jobs):
            self._failures.inc()
            return False  # nothing answered: keep the last good snapshot
        snapshot = HistorySnapshot(as_of=wall, charts=charts)
        chart_errors: list[str] = []
        try:
            # Only this render is counted: the stale copy would fail the same charts.
            fresh = history_page.render_page(
                snapshot, stale=False, refresh_seconds=self.refresh_seconds, errors=chart_errors
            )
            stale = history_page.render_page(
                snapshot, stale=True, refresh_seconds=self.refresh_seconds
            )
        except Exception:
            # A rendering bug must not take the page down: the last good page stays.
            log.exception("history page could not be rendered")
            self._failures.inc()
            return False
        if failed or chart_errors:
            self._failures.inc()  # once per refresh, whatever the number of bad charts
        self._current = _Current(snapshot, fresh, stale, self._monotonic())
        return True

    # -- thread --
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(self._stop,), name="history-refresher", daemon=True
        )
        self._thread.start()

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.refresh_once()
            stop.wait(self.refresh_seconds)

    def stop(self) -> None:
        # The refresh loop looks at this event every 50 ms, so a query that is stuck
        # does not make shutdown wait for the refresh deadline.
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)
        self._thread = None


def history_from_env(
    environ: Mapping[str, str], registry: CollectorRegistry
) -> HistoryService | None:
    """The service if STATUS_HISTORY is `on` and STATUS_PROMETHEUS_URL is valid,
    else None (feature off, `/status` is a 404).

    A bad value is logged and turns the feature off rather than stopping the app: a
    cosmetic feature must never stop the app that serves the redirects."""
    switch = environ.get("STATUS_HISTORY", "").strip().lower()
    if switch != "on":
        if switch:
            log.warning("STATUS_HISTORY %r is not 'on'; history charts are off", switch)
        return None
    url = environ.get("STATUS_PROMETHEUS_URL", "").strip()
    if not url:
        log.warning("STATUS_HISTORY is on but STATUS_PROMETHEUS_URL is not set; charts are off")
        return None
    interval = DEFAULT_REFRESH_SECONDS
    raw = environ.get("STATUS_HISTORY_REFRESH_SECONDS", "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        # isfinite first: nan and inf must never reach the range check as numbers.
        if math.isfinite(value) and MIN_REFRESH_SECONDS <= value <= MAX_REFRESH_SECONDS:
            interval = value
        else:
            log.warning(
                "STATUS_HISTORY_REFRESH_SECONDS %r is not a number from %d to %d; using %d",
                raw,
                MIN_REFRESH_SECONDS,
                MAX_REFRESH_SECONDS,
                DEFAULT_REFRESH_SECONDS,
            )
    try:
        client = PrometheusQuery(url)
    except ValueError as exc:
        log.error("STATUS_PROMETHEUS_URL is not usable (%s); /status is off", exc)
        return None
    log.info("history charts on: refreshing every %g s", interval)
    return HistoryService(client.query_range, registry=registry, refresh_seconds=interval)
