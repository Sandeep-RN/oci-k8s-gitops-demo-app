# GET /api/status (demo #230, ADR 0026): reads a cached snapshot, never Prometheus.
import json
import re
import time
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from app import status
from app.main import create_app
from app.status import StatusService
from tests.helpers import AUTH, TOKENS
from tests.status_helpers import Clock, FakePrometheus

TOP_LEVEL = ["as_of", "stale", "slo", "traffic", "node", "cluster", "delivery"]


def make(fake=None, clock=None, **app_kwargs):
    fake = fake if fake is not None else FakePrometheus()
    clock = clock if clock is not None else Clock()
    registry = CollectorRegistry()
    service = StatusService(fake, registry=registry, monotonic=clock, wall=lambda: 1_795_000_100.0)
    app = create_app(status_service=service, registry=registry, **app_kwargs)
    return TestClient(app, follow_redirects=False), service, fake, clock, registry


def test_the_route_does_not_exist_when_the_feature_is_off():
    client = TestClient(create_app())
    r = client.get("/api/status")
    assert r.status_code == 404
    assert "/api/status" not in client.get("/api/openapi.json").text


def test_200_available_false_until_a_refresh_has_succeeded():
    # Review of PR #42 (L2): a 5xx here would count in Traefik's server-error
    # alert for every visitor during startup or a Prometheus outage at boot.
    client, service, fake, _, _ = make()
    r = client.get("/api/status")
    assert r.status_code == 200
    assert r.json() == {"available": False}
    assert r.headers["cache-control"] == "no-store"
    assert fake.calls == []  # and the request did not go to Prometheus


def test_available_false_stays_when_every_refresh_failed():
    client, service, fake, _, _ = make()
    fake.fail_all()
    service.refresh_once()
    r = client.get("/api/status")
    assert (r.status_code, r.json()) == (200, {"available": False})
    assert r.headers["cache-control"] == "no-store"


def test_a_snapshot_has_no_available_key_and_replaces_the_placeholder():
    client, service, _, _, _ = make()
    assert client.get("/api/status").json() == {"available": False}
    service.refresh_once()
    body = client.get("/api/status").json()
    assert "available" not in body
    assert body["slo"]["target"] == 99.5


def test_200_with_the_snapshot_and_a_short_public_cache():
    client, service, _, _, _ = make()
    service.refresh_once()
    r = client.get("/api/status")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    assert r.headers["cache-control"] == "public, max-age=15"
    body = r.json()
    assert list(body) == TOP_LEVEL
    assert body["stale"] is False
    assert body["slo"]["target"] == 99.5
    assert body["slo"]["window_days"] == 7
    assert body["delivery"]["commit_short"] == body["delivery"]["commit"][:7]


def test_the_response_carries_no_labels_ips_or_names():
    client, service, _, _, _ = make()
    service.refresh_once()
    text = client.get("/api/status").text
    for leaked in ("backend-6d9f", "10.0.0.9", "node-1", "internal.invalid", "secret-app-name"):
        assert leaked not in text
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)  # no IPv4 address anywhere
    # Only these leaf names exist: a new key must be a deliberate change.
    keys = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                keys.add(k)
                walk(v)

    walk(json.loads(text))
    assert keys == {
        *TOP_LEVEL,
        "target",
        "window_days",
        "error_budget_remaining",
        "availability_7d",
        "latency_good_7d",
        "requests_per_s_5m",
        "cpu_ratio",
        "mem_ratio",
        "net_tx_bytes_per_s",
        "pods_running",
        "commit",
        "commit_short",
        "running_since",
        "argocd",
        "apps_total",
        "synced",
        "healthy",
    }


def test_requests_never_call_prometheus():
    client, service, fake, _, _ = make()
    service.refresh_once()
    calls = len(fake.calls)
    for _ in range(25):
        assert client.get("/api/status").status_code == 200
    assert len(fake.calls) == calls


def test_request_data_never_reaches_a_query():
    client, service, fake, _, _ = make()
    service.refresh_once()
    hostile = "up%7Bx%3D%22y%22%7D&q=1&query=drop&promql=x&namespace=kube-system"
    r = client.get(f"/api/status?query={hostile}", headers={"X-Query": "evil", "Host": "evil"})
    assert r.status_code == 200
    assert set(fake.calls) == set(status.QUERIES)  # still only the allowlist
    assert len(fake.calls) == len(status.QUERIES)  # and nothing new was sent


def test_the_handler_declares_no_parameters_at_all():
    _, service, _, _, _ = make()
    app = create_app(status_service=service)
    (route,) = [r for r in app.routes if getattr(r, "path", None) == "/api/status"]
    dependant = route.dependant
    assert not dependant.query_params
    assert not dependant.path_params
    assert not dependant.header_params
    assert not dependant.cookie_params
    assert not dependant.body_params
    assert route.methods == {"GET"}  # HEAD is not added either


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_other_methods_are_refused(method):
    client, service, _, _, _ = make()
    service.refresh_once()
    assert getattr(client, method)("/api/status").status_code == 405


def test_the_snapshot_turns_stale_in_the_response():
    client, service, fake, clock, _ = make()
    service.refresh_once()
    fake.fail_all()
    clock.now += 91
    body = client.get("/api/status").json()
    assert body["stale"] is True
    assert body["slo"]["target"] == 99.5


def test_a_failed_refresh_leaves_the_served_snapshot_alone():
    client, service, fake, _, _ = make()
    service.refresh_once()
    before = client.get("/api/status").json()
    fake.fail_all()
    service.refresh_once()
    assert client.get("/api/status").json() == before


# ---- precedence over /{code} and the other routes ---------------------------------


def test_the_status_route_wins_over_the_code_route_and_codes_still_work():
    client, service, _, _, _ = make(tokens=TOKENS)
    service.refresh_once()
    assert client.get("/api/status").status_code == 200
    code = client.post("/api/links", json={"url": "https://example.com"}, headers=AUTH).json()[
        "code"
    ]
    assert client.get(f"/{code}").status_code == 302
    assert client.get("/abcdefg").status_code == 404
    # Not a code (two segments), so it can never collide with a link.
    assert not re.fullmatch(r"[0-9A-Za-z]{7}", "api/status")


def test_the_status_route_is_counted_under_its_own_route_not_the_slo_route():
    client, service, _, _, registry = make()
    service.refresh_once()
    client.get("/api/status")

    def n(route):
        labels = {"method": "GET", "route": route, "status": "200"}
        return registry.get_sample_value("http_request_duration_seconds_count", labels) or 0

    assert n("/api/status") == 1
    assert n("/{code}") == 0  # the redirect SLO never sees it


# ---- isolation from health and readiness (ADR 0016) -----------------------------


def test_health_and_readiness_ignore_a_prometheus_that_raises():
    fake = FakePrometheus()
    for q in list(fake.answers):
        fake.answers[q] = RuntimeError("prometheus exploded")
    registry = CollectorRegistry()
    service = StatusService(fake, registry=registry, refresh_seconds=0.05)
    app = create_app(status_service=service, registry=registry, readiness_checks=[lambda: True])
    with TestClient(app) as client:  # the lifespan starts the refresher
        deadline = time.monotonic() + 3
        while len(fake.calls) < len(status.QUERIES) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fake.calls, "the refresher never ran"
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").json() == {"status": "ready"}
        assert client.get("/api/status").json() == {"available": False}


def test_the_health_routes_do_not_mention_status_or_touch_the_query():
    client, service, fake, _, _ = make()
    client.get("/healthz")
    client.get("/readyz")
    assert fake.calls == []
    assert client.get("/readyz").json() == {"status": "ready"}


# ---- lifespan -----------------------------------------------------------------------


def test_the_lifespan_starts_the_refresher_and_stops_it_at_shutdown():
    client, service, fake, _, _ = make()
    assert not service.running  # no TestClient context yet: nothing runs
    with client:
        assert service.running
        deadline = time.monotonic() + 3
        while service.read() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.get("/api/status").status_code == 200
    assert not service.running


def test_a_caller_lifespan_still_runs_around_the_refresher():
    events = []

    @asynccontextmanager
    async def mine(_):
        events.append("start")
        yield
        events.append("stop")

    service = StatusService(FakePrometheus(), registry=CollectorRegistry())
    app = create_app(status_service=service, lifespan=mine)
    with TestClient(app):
        assert events == ["start"]
        assert service.running
    assert events == ["start", "stop"]
    assert not service.running


def test_nothing_runs_when_the_feature_is_off():
    with TestClient(create_app()) as client:
        assert client.get("/api/status").status_code == 404


# ---- metrics --------------------------------------------------------------------------


def test_the_status_metrics_live_on_the_apps_registry_with_no_labels():
    client, service, fake, _, registry = make()
    names = {m.name for m in registry.collect()}
    assert {"status_refresh_failures", "status_snapshot_age_seconds"} <= names
    for metric in registry.collect():
        if metric.name.startswith("status_"):
            assert all(not s.labels for s in metric.samples), metric.name
    fake.fail_all()
    service.refresh_once()
    assert registry.get_sample_value("status_refresh_failures_total") == 1
