"""Live platform data for the landing page (demo issue #230, ADR 0026).

GET /api/status serves ONE in-memory snapshot of rounded aggregates. A
background thread refreshes it from Prometheus; the request path never calls
Prometheus, so a slow or dead Prometheus can only make the numbers stale. It
cannot slow, fail or restart the redirect service (ADR 0016: this is not part of
/healthz or /readyz).

Safety rules that the tests pin down:
- Every PromQL string is a constant in QUERIES. No request data reaches a query.
- Prometheus has no authentication, so its answers are parsed as hostile: size
  cap, strict JSON, strict numbers, labels never copied to the output.
- The feature is off unless STATUS_PROMETHEUS_URL is set.
"""

import functools
import json
import logging
import math
import queue
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from typing import Any

from prometheus_client import CollectorRegistry

from app.metrics import make_status_metrics

log = logging.getLogger("app")

# ---- the allowlist (ADR 0026) ----------------------------------------------------
# The ONLY PromQL this app ever sends. Do not build a query from a variable.
Q_ERROR_BUDGET = "slo:redirect_error_budget:remaining_ratio7d"
Q_ERROR_RATIO = "slo:redirect_errors:ratio_rate7d"
Q_LATENCY_GOOD = "slo:redirect_latency_good:ratio_rate7d"
Q_REQUEST_RATE = "slo:redirect_requests:rate5m"
Q_NODE_CPU = '1-avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))'
# "By MemAvailable": `kubectl top` uses another measure and shows a higher number.
Q_NODE_MEMORY = "1-node_memory_MemAvailable_bytes/node_memory_MemTotal_bytes"
# Transmit only, all traffic. Receive is deliberately absent: on 2026-11-23 it
# was almost entirely image pulls, not visitors.
Q_NODE_NET_TX = (
    'sum(rate(node_network_transmit_bytes_total{device!~"lo|veth.*|cni.*|flannel.*"}[5m]))'
)
Q_PODS_RUNNING = 'count(kube_pod_status_phase{phase="Running"}==1)'
# pod=~"backend-.*" like Q_BACKEND_START: another "backend" container in the
# namespace (a sidecar job, a debug pod) must not become "the running commit".
Q_BACKEND_IMAGE = (
    'kube_pod_container_info{namespace="link-shortener",container="backend",pod=~"backend-.*"}'
)
Q_BACKEND_START = 'max(kube_pod_start_time{namespace="link-shortener",pod=~"backend-.*"})'
# These series do not exist until a gitops ServiceMonitor scrapes Argo CD. An
# empty answer is normal and gives `argocd: null`.
Q_ARGOCD_APPS = "argocd_app_info"

QUERIES: tuple[str, ...] = (
    Q_ERROR_BUDGET,
    Q_ERROR_RATIO,
    Q_LATENCY_GOOD,
    Q_REQUEST_RATE,
    Q_NODE_CPU,
    Q_NODE_MEMORY,
    Q_NODE_NET_TX,
    Q_PODS_RUNNING,
    Q_BACKEND_IMAGE,
    Q_BACKEND_START,
    Q_ARGOCD_APPS,
)

# ---- limits -----------------------------------------------------------------------
SLO_TARGET_PERCENT = 99.5  # ADR 0008; the recording rules do not carry the target
SLO_WINDOW_DAYS = 7
QUERY_TIMEOUT = 2.0  # seconds, per query
REFRESH_TIMEOUT = 5.0  # seconds, for a whole refresh
# The watchdog closes the socket this long after QUERY_TIMEOUT, as a backstop for
# what the read loop cannot see: a server that dribbles its HEADERS.
WATCHDOG_GRACE = 0.5
# Real answers are a few KB. Memory per refresh is roughly this x QUERY_WORKERS x
# ~20 (a hostile body of `{}` costs ~20x its size once parsed): 64 KiB x 3 keeps
# the worst case in single-digit MiB (it was 126 MiB at 1 MiB x 6).
MAX_RESPONSE_BYTES = 64 * 1024
MAX_SERIES = 1000  # one answer; the biggest real one has a handful
MAX_LABEL_VALUE = 512
MAX_LABELS = 32
QUERY_WORKERS = 3  # 11 queries; normal answers take milliseconds
DEFAULT_REFRESH_SECONDS = 30.0
MIN_REFRESH_SECONDS = 5.0  # below this the env value is refused: it would hammer Prometheus
# Above this the page would call a healthy snapshot stale most of the time.
MAX_REFRESH_SECONDS = 300.0
STALE_AFTER_SECONDS = 90.0  # the floor; the real threshold is 3 refresh intervals
# Answer before the first successful refresh. 200, not 5xx: public clients would
# otherwise trip the Traefik server-error alert during startup or an outage at boot.
NOT_YET = {"available": False}

_NUMBER = re.compile(r"[+-]?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40}", re.ASCII)
_YEAR_2100 = 4_102_444_800


class QueryError(Exception):
    """Prometheus could not be asked, or its answer was unusable."""


@dataclass(frozen=True)
class Sample:
    """One series of an instant vector. Labels are used to read, never to publish."""

    labels: Mapping[str, Any]
    value: Any


Query = Callable[[str], list[Sample]]


# ---- the Prometheus client --------------------------------------------------------
class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        # A redirect would let a changed Prometheus URL send us elsewhere.
        return None


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-standard JSON constant {name}")


class _Watch:
    """Lets the watchdog thread cut off ONE request from outside.

    The socket timeout bounds each single receive, so a server that sends one
    byte every 1.5 s keeps urllib inside getresponse() for as long as it likes.
    The only way to interrupt a blocked receive from another thread is to shut
    the socket down. shutdown() (not close()) on purpose: the owning thread still
    closes the descriptor, so it can never be reused under the blocked call (and
    close() would not even act while the response's makefile() holds a reference).
    On Linux, the pod, a blocked receive returns at once. On Windows it returns at
    the next byte, so the bound there is the watchdog plus one socket timeout.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self.expired = False

    def attach(self, sock: socket.socket) -> None:
        with self._lock:
            self._sock = sock
            if self.expired:  # the deadline passed while we were connecting
                self._abort()

    def expire(self) -> None:
        with self._lock:
            self.expired = True
            if self._sock is not None:
                self._abort()

    def _abort(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)  # type: ignore[union-attr]
        except OSError:
            pass  # already closed or reset: nothing left to interrupt


class _WatchedConnection:
    """Mixin: report the connected socket to the request's _Watch."""

    def __init__(self, *args: Any, watch: _Watch | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[call-arg]
        self._watch = watch

    def connect(self) -> None:
        super().connect()  # type: ignore[misc]
        # For https this is the TLS socket: the handshake itself is bounded only
        # by the socket timeout (the in-cluster Prometheus is plain http).
        if self._watch is not None:
            self._watch.attach(self.sock)  # type: ignore[attr-defined]


class _WatchedHTTPConnection(_WatchedConnection, HTTPConnection):
    pass


class _WatchedHTTPSConnection(_WatchedConnection, HTTPSConnection):
    pass


class _WatchedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> Any:
        factory = functools.partial(_WatchedHTTPConnection, watch=getattr(req, "watch", None))
        return self.do_open(factory, req)


class _WatchedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: urllib.request.Request) -> Any:
        factory = functools.partial(_WatchedHTTPSConnection, watch=getattr(req, "watch", None))
        return self.do_open(factory, req, context=self._context)


class PrometheusQuery:
    """The default `query` callable: stdlib urllib, no new dependency."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = QUERY_TIMEOUT,
        opener: Any = None,
    ) -> None:
        self._endpoint = _endpoint(base_url)
        self._timeout = timeout
        # No proxies from the environment (HTTP_PROXY would divert the call),
        # no redirects, and connections the watchdog can cut off.
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirects(),
            _WatchedHTTPHandler(),
            _WatchedHTTPSHandler(),
        )

    def __call__(self, promql: str) -> list[Sample]:
        url = f"{self._endpoint}?{urllib.parse.urlencode({'query': promql})}"
        request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
        try:
            body = self._fetch(request)
        except QueryError:
            raise
        except (OSError, ValueError, HTTPException) as exc:
            # URLError, HTTPError, timeouts and resets are all OSError.
            raise QueryError(f"prometheus request failed: {type(exc).__name__}") from exc
        return _parse_vector(body)

    def _fetch(self, request: urllib.request.Request) -> bytes:
        # Two bounds on the whole exchange, because the socket timeout only bounds
        # one receive and a hostile or broken server can stay under it forever:
        # 1. the body loop below, which checks a deadline between receives;
        # 2. a watchdog that shuts the socket down shortly after, which is the
        #    only thing that can stop a server dribbling the HEADERS (urllib is
        #    inside getresponse() and never returns to our loop).
        deadline = time.monotonic() + self._timeout
        watch = _Watch()
        request.watch = watch  # type: ignore[attr-defined]  # read by the Watched* handlers
        watchdog = threading.Timer(self._timeout + WATCHDOG_GRACE, watch.expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                if getattr(response, "status", 200) != 200:
                    raise QueryError(f"prometheus answered HTTP {response.status}")
                chunks: list[bytes] = []
                total = 0
                while True:
                    if time.monotonic() > deadline:
                        raise QueryError("prometheus response too slow")
                    # read1: AT MOST ONE receive per call. read(8192) loops until
                    # 8192 bytes arrived, so a dribbling server held it (and the
                    # deadline check above) for minutes.
                    chunk = response.read1(8192)
                    if not chunk:
                        if watch.expired:  # shutdown looks like a clean EOF
                            raise QueryError("prometheus response too slow")
                        return b"".join(chunks)
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise QueryError("prometheus response too large")
                    chunks.append(chunk)
        except (OSError, HTTPException, ValueError) as exc:
            if watch.expired:  # the shutdown surfaces as a reset or a bad status line
                raise QueryError("prometheus response too slow") from exc
            raise
        finally:
            watchdog.cancel()


def _endpoint(base_url: str) -> str:
    """Validate STATUS_PROMETHEUS_URL and return the instant-query endpoint."""
    text = base_url.strip() if isinstance(base_url, str) else ""
    # Whitespace and ASCII control characters (NUL, ESC, DEL...) have no place in
    # a URL and would end up in logs and request lines.
    if not text or re.search(r"[\x00-\x20\x7f]", text):
        raise ValueError("a URL like http://host:9090 is required")
    parts = urllib.parse.urlsplit(text)
    try:
        port = parts.port  # raises ValueError for a bad port
    except ValueError as exc:
        raise ValueError("the port is not a number") from exc
    if port == 0:
        raise ValueError("port 0 is not a listening port")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("the URL must be http(s)://host[:port]")
    if parts.username is not None or parts.password is not None:
        raise ValueError("the URL must not contain credentials")
    if parts.query or parts.fragment:
        raise ValueError("the URL must not contain a query or fragment")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}/api/v1/query"


def _parse_vector(body: bytes) -> list[Sample]:
    try:
        doc = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise QueryError("prometheus answer is not valid JSON") from exc
    if not isinstance(doc, dict) or doc.get("status") != "success":
        raise QueryError("prometheus answer is not a success")
    data = doc.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "vector":
        raise QueryError("prometheus answer is not an instant vector")
    result = data.get("result")
    if not isinstance(result, list):
        raise QueryError("prometheus answer has no result list")
    if len(result) > MAX_SERIES:
        raise QueryError("prometheus answer has too many series")
    samples = []
    for item in result:
        sample = _parse_sample(item)
        if sample is not None:
            samples.append(sample)
    return samples


def _parse_sample(item: Any) -> Sample | None:
    """One series, or None when it is malformed or has no usable number (NaN is
    a normal answer for 0/0 and means "no value")."""
    if not isinstance(item, dict):
        return None
    metric, pair = item.get("metric"), item.get("value")
    if not isinstance(metric, dict) or not isinstance(pair, list) or len(pair) != 2:
        return None
    text = pair[1]
    if not isinstance(text, str) or len(text) > 64 or not _NUMBER.fullmatch(text):
        return None
    value = float(text)
    if not math.isfinite(value):
        return None
    labels = {
        k: v[:MAX_LABEL_VALUE]
        for k, v in list(metric.items())[:MAX_LABELS]
        if isinstance(k, str) and isinstance(v, str) and len(k) <= 128
    }
    return Sample(labels=labels, value=value)


# ---- the snapshot ------------------------------------------------------------------
def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _scalar(samples: Any) -> float | None:
    """The first usable number of a result, or None."""
    if not isinstance(samples, list):
        return None
    for sample in samples:
        if isinstance(sample, Sample) and _is_number(sample.value):
            return float(sample.value)
    return None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _ratio(value: float | None, digits: int) -> float | None:
    return None if value is None else round(_clamp(value, 0.0, 1.0), digits)


def _non_negative(value: float | None, high: float, digits: int | None = None) -> Any:
    if value is None:
        return None
    clamped = _clamp(value, 0.0, high)
    return round(clamped, digits) if digits is not None else int(round(clamped))


def _tag(image: Any) -> str | None:
    """The tag of an image reference ("host:port/repo/name:tag@sha256:..."), if it
    is a full lowercase git SHA. Anything else is not trusted as a commit."""
    if not isinstance(image, str) or len(image) > 1024:
        return None
    last = image.split("@", 1)[0].rsplit("/", 1)[-1]  # a registry port is before the "/"
    if ":" not in last:
        return None
    tag = last.rsplit(":", 1)[1]
    return tag if _COMMIT.fullmatch(tag) else None


def _commit(samples: Any) -> str | None:
    if not isinstance(samples, list):
        return None
    tags = {
        _tag(s.labels.get("image")) if isinstance(s.labels, Mapping) else None
        for s in samples
        if isinstance(s, Sample)
    }
    # Two replicas on different images (a rollout) or any odd tag: no claim.
    return tags.pop() if len(tags) == 1 else None


def _running_since(samples: Any) -> str | None:
    value = _scalar(samples)
    if value is None or not 0 < value < _YEAR_2100:
        return None
    return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _argocd(samples: Any) -> dict[str, int] | None:
    """Counts only. App names, repos and namespaces never leave this function."""
    if not isinstance(samples, list):
        return None
    apps = [s for s in samples if isinstance(s, Sample)]
    if not apps:
        return None

    def count(label: str, want: str) -> int:
        return sum(1 for s in apps if isinstance(s.labels, Mapping) and s.labels.get(label) == want)

    return {
        "apps_total": len(apps),
        "synced": count("sync_status", "Synced"),
        "healthy": count("health_status", "Healthy"),
    }


def build_payload(results: Mapping[str, Any], as_of: datetime) -> dict[str, Any]:
    """Rounded aggregates from raw query results. A missing, failed or empty
    result nulls its own fields and nothing else."""
    errors = _scalar(results.get(Q_ERROR_RATIO))
    commit = _commit(results.get(Q_BACKEND_IMAGE))
    return {
        "as_of": as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stale": False,  # set per read
        "slo": {
            "target": SLO_TARGET_PERCENT,
            "window_days": SLO_WINDOW_DAYS,
            "error_budget_remaining": _ratio(_scalar(results.get(Q_ERROR_BUDGET)), 4),
            "availability_7d": _ratio(None if errors is None else 1.0 - errors, 5),
            "latency_good_7d": _ratio(_scalar(results.get(Q_LATENCY_GOOD)), 5),
        },
        "traffic": {
            "requests_per_s_5m": _non_negative(_scalar(results.get(Q_REQUEST_RATE)), 1e9, 3),
        },
        "node": {
            "cpu_ratio": _ratio(_scalar(results.get(Q_NODE_CPU)), 3),
            "mem_ratio": _ratio(_scalar(results.get(Q_NODE_MEMORY)), 3),
            "net_tx_bytes_per_s": _non_negative(_scalar(results.get(Q_NODE_NET_TX)), 1e15),
        },
        "cluster": {"pods_running": _non_negative(_scalar(results.get(Q_PODS_RUNNING)), 1e6)},
        "delivery": {
            "commit": commit,
            "commit_short": commit[:7] if commit else None,
            "running_since": _running_since(results.get(Q_BACKEND_START)),
            "argocd": _argocd(results.get(Q_ARGOCD_APPS)),
        },
    }


# ---- the refresher -----------------------------------------------------------------
class StatusService:
    """Holds the snapshot and refreshes it. `read()` is O(1) and never blocks."""

    def __init__(
        self,
        query: Query,
        *,
        registry: CollectorRegistry | None = None,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
        stale_after: float | None = None,
        refresh_timeout: float = REFRESH_TIMEOUT,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._query = query
        self.refresh_seconds = refresh_seconds
        # Three missed refreshes, and never under 90 s: a fixed 90 s would call a
        # healthy snapshot stale most of the time at a 300 s interval.
        self._stale_after = (
            stale_after
            if stale_after is not None
            else max(STALE_AFTER_SECONDS, 3 * refresh_seconds)
        )
        self._refresh_timeout = refresh_timeout
        self._monotonic = monotonic
        self._wall = wall
        # (payload, monotonic time of the success). Replaced whole: readers never
        # see a half-written snapshot, and need no lock.
        self._snapshot: tuple[dict[str, Any], float] | None = None
        self._flight = threading.Lock()  # single-flight: refreshes never overlap
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Workers of the latest refresh. A new refresh does not start while any of
        # them is still alive, so a Prometheus that never answers can strand ONE
        # batch of threads, not one per refresh.
        self._workers: list[threading.Thread] = []
        self._failures, age = make_status_metrics(
            registry if registry is not None else CollectorRegistry()
        )
        age.set_function(self._age)

    def _age(self) -> float:
        # -1 means "never succeeded"; alerts on age > N then stay quiet at startup.
        snapshot = self._snapshot
        return -1.0 if snapshot is None else self._monotonic() - snapshot[1]

    def read(self) -> dict[str, Any] | None:
        snapshot = self._snapshot
        if snapshot is None:
            return None
        payload, at = snapshot
        return {**payload, "stale": (self._monotonic() - at) > self._stale_after}

    def refresh_once(self) -> bool:
        """One refresh. True if the snapshot was replaced. Never raises."""
        if not self._flight.acquire(blocking=False):
            return False
        try:
            return self._refresh()
        except Exception:
            log.exception("status refresh failed unexpectedly")
            self._failures.inc()
            return False
        finally:
            self._flight.release()

    def _refresh(self) -> bool:
        if any(t.is_alive() for t in self._workers):
            # Review of PR #42 (M1): starting more workers on top of stuck ones is
            # how threads pile up. Skip, and count it: this IS a failed refresh.
            log.warning("status refresh skipped: queries of the previous one are still running")
            self._failures.inc()
            return False
        # Fresh daemon workers per refresh (not a ThreadPoolExecutor: its threads
        # are joined at interpreter exit, so a stuck query would stop the pod from
        # exiting on SIGTERM). A late finisher writes into THIS refresh's dict,
        # which nothing reads after the deadline.
        jobs: queue.SimpleQueue[str] = queue.SimpleQueue()
        for q in QUERIES:
            jobs.put(q)
        outcomes: dict[str, Any] = {}
        cutoff = threading.Event()  # set at the deadline: workers take no new job

        def work() -> None:
            while not cutoff.is_set():
                try:
                    q = jobs.get_nowait()
                except queue.Empty:
                    return
                try:
                    outcomes[q] = self._query(q)
                except Exception as exc:
                    outcomes[q] = exc

        self._workers = [
            threading.Thread(target=work, name=f"status-query-{i}", daemon=True)
            for i in range(QUERY_WORKERS)
        ]
        for t in self._workers:
            t.start()
        deadline = time.monotonic() + self._refresh_timeout
        for t in self._workers:
            t.join(max(0.0, deadline - time.monotonic()))
        cutoff.set()
        results: dict[str, Any] = {}
        failed = 0
        for q in QUERIES:
            if q not in outcomes:  # still running, or never started: counts as failed
                failed += 1
                continue
            value = outcomes[q]
            if isinstance(value, QueryError):
                # Every QueryError message is a fixed string (no label or body text).
                log.warning("status query failed: %s", value)
                failed += 1
            elif isinstance(value, Exception):
                log.warning("status query failed: %s", type(value).__name__)
                failed += 1
            elif isinstance(value, list):
                results[q] = value
            else:
                failed += 1
        if failed:
            self._failures.inc()
        if failed == len(QUERIES):
            return False  # nothing answered: keep the last good snapshot
        payload = build_payload(results, datetime.fromtimestamp(self._wall(), UTC))
        self._snapshot = (payload, self._monotonic())
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
            target=self._run, args=(self._stop,), name="status-refresher", daemon=True
        )
        self._thread.start()

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.refresh_once()
            stop.wait(self.refresh_seconds)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._refresh_timeout + 1.0)
        self._thread = None


def service_from_env(
    environ: Mapping[str, str], registry: CollectorRegistry
) -> StatusService | None:
    """The service if STATUS_PROMETHEUS_URL is set and valid, else None (feature off).

    A bad value is logged and turns the feature off rather than stopping the
    app: a cosmetic feature must never stop the app that serves the redirects.
    """
    url = environ.get("STATUS_PROMETHEUS_URL", "").strip()
    if not url:
        return None
    interval = DEFAULT_REFRESH_SECONDS
    raw = environ.get("STATUS_REFRESH_SECONDS", "").strip()
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
                "STATUS_REFRESH_SECONDS %r is not a number from %d to %d; using %d",
                raw,
                MIN_REFRESH_SECONDS,
                MAX_REFRESH_SECONDS,
                DEFAULT_REFRESH_SECONDS,
            )
    try:
        query = PrometheusQuery(url)
    except ValueError as exc:
        log.error("STATUS_PROMETHEUS_URL is not usable (%s); /api/status is off", exc)
        return None
    log.info("live status on: refreshing every %g s", interval)
    return StatusService(query, registry=registry, refresh_seconds=interval)
