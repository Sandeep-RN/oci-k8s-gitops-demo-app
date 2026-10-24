# The SLO queries (ADR 0008) depend on these exact metric names and labels.
# Changing them breaks the recording rules and alerts in the gitops repo.
import urllib.request

import pytest
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from app.links import InMemoryLinkStore, StoreUnavailable
from app.main import create_app
from app.metrics import REQUEST_DURATION_BUCKETS, start_metrics_server

METRIC = "http_request_duration_seconds"


@pytest.fixture
def registry():
    # A fresh registry per test keeps counts independent of other tests.
    return CollectorRegistry()


def count(registry, route, status, method="GET"):
    labels = {"method": method, "route": route, "status": status}
    return registry.get_sample_value(f"{METRIC}_count", labels) or 0


def test_redirect_is_recorded_under_the_route_template(registry):
    client = TestClient(create_app(registry=registry))
    code = client.post("/api/links", json={"url": "https://example.com"}).json()["code"]

    client.get(f"/{code}", follow_redirects=False)

    # The template, not the code itself: one series per route, not per link.
    assert count(registry, "/{code}", "302") == 1
    assert count(registry, "/api/links", "201", method="POST") == 1
    routes = {s.labels.get("route") for m in registry.collect() for s in m.samples}
    assert f"/{code}" not in routes


def test_unknown_code_is_a_404_not_an_error(registry):
    client = TestClient(create_app(registry=registry))
    client.get("/zzzzzzz")
    assert count(registry, "/{code}", "404") == 1


def test_unmatched_paths_share_one_label(registry):
    # Scanners send random paths; each must not create a new time series.
    client = TestClient(create_app(registry=registry))
    client.get("/wp-admin/setup.php")
    client.get("/a/b/c")
    assert count(registry, "<unmatched>", "404") == 2


def test_storage_outage_is_recorded_as_503(registry):
    class DownStore(InMemoryLinkStore):
        def get(self, code):
            raise StoreUnavailable("db down")

    client = TestClient(create_app(store=DownStore(), registry=registry))
    client.get("/abcdefg")
    assert count(registry, "/{code}", "503") == 1


def test_unhandled_exception_is_recorded_as_500(registry):
    class BrokenStore(InMemoryLinkStore):
        def get(self, code):
            raise RuntimeError("bug")

    app = create_app(store=BrokenStore(), registry=registry)
    r = TestClient(app, raise_server_exceptions=False).get("/abcdefg")
    assert r.status_code == 500
    assert count(registry, "/{code}", "500") == 1


def test_histogram_has_the_slo_latency_threshold_bucket(registry):
    # The latency SLI is "95% of redirects under 200 ms": it needs le="0.2".
    assert 0.2 in REQUEST_DURATION_BUCKETS
    TestClient(create_app(registry=registry)).get("/healthz")
    labels = {"method": "GET", "route": "/healthz", "status": "200", "le": "0.2"}
    assert registry.get_sample_value(f"{METRIC}_bucket", labels) == 1


def test_metrics_server_serves_the_registry(registry):
    TestClient(create_app(registry=registry)).get("/healthz")
    port, stop = start_metrics_server(0, registry=registry, addr="127.0.0.1")
    try:
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read()
    finally:
        stop()
    assert f'{METRIC}_count{{method="GET",route="/healthz",status="200"}} 1.0' in body.decode()


def test_metrics_are_not_served_on_the_app_port(registry):
    # The app port sits behind the public load balancer; metrics stay internal.
    r = TestClient(create_app(registry=registry)).get("/metrics")
    assert r.status_code == 404
