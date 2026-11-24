# GET /status and the landing strip's link to it (demo #235, ADR 0027).
import re
import time
from contextlib import asynccontextmanager

from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from app import history, history_page, links
from app.history import HistoryService
from app.main import create_app
from app.status import StatusService
from tests.helpers import AUTH, TOKENS
from tests.history_helpers import NOW, FakeRange
from tests.status_helpers import Clock, FakePrometheus

WALL = NOW + 123.0
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


def make(fake=None, clock=None, status=False, **app_kwargs):
    fake = fake if fake is not None else FakeRange()
    clock = clock if clock is not None else Clock()
    registry = CollectorRegistry()
    service = HistoryService(fake, registry=registry, monotonic=clock, wall=lambda: WALL)
    kwargs = dict(app_kwargs)
    if status:
        kwargs["status_service"] = StatusService(FakePrometheus(), registry=registry)
    app = create_app(history_service=service, registry=registry, **kwargs)
    return TestClient(app, follow_redirects=False), service, fake, clock, registry


def test_the_route_does_not_exist_when_history_is_off():
    client = TestClient(create_app())
    r = client.get("/status")
    assert r.status_code == 404
    assert "text/html" not in r.headers["content-type"]
    assert "/status" not in client.get("/api/openapi.json").text


def test_a_status_service_alone_does_not_turn_the_page_on():
    app = create_app(status_service=StatusService(FakePrometheus(), registry=CollectorRegistry()))
    assert TestClient(app).get("/status").status_code == 404


def test_200_warming_up_until_a_refresh_has_succeeded_never_a_5xx():
    # A public 5xx feeds the Traefik error alert (the lesson of ADR 0026's amendment).
    client, service, fake, _, _ = make()
    r = client.get("/status")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-security-policy"] == CSP
    assert "warming up" in r.text and "try again in a minute" in r.text
    assert "<svg" not in r.text
    assert fake.calls == []  # and the request did not go to Prometheus


def test_warming_up_stays_while_every_refresh_fails():
    fake = FakeRange()
    fake.fail_all()
    client, service, _, _, _ = make(fake)
    service.refresh_once()
    r = client.get("/status")
    assert r.status_code == 200 and "warming up" in r.text
    assert r.headers["cache-control"] == "no-store"


def test_the_page_after_a_refresh_is_cacheable_and_has_the_charts():
    client, service, _, _, _ = make()
    service.refresh_once()
    r = client.get("/status")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert r.headers["cache-control"] == "public, max-age=60"
    assert r.text.count("<svg") == 8
    assert "warming up" not in r.text


def test_the_csp_header_is_exact_and_has_no_script_src():
    client, service, _, _, _ = make()
    service.refresh_once()
    for r in (client.get("/status"),):
        csp = r.headers["content-security-policy"]
        assert csp == CSP == history_page.CSP
        assert "script-src" not in csp and "unsafe-eval" not in csp
        assert "connect-src" not in csp
    assert "<script" not in r.text.lower()


def test_head_is_answered_without_a_body_and_other_methods_are_refused():
    client, service, _, _, _ = make()
    service.refresh_once()
    head = client.head("/status")
    assert head.status_code == 200 and head.content == b""
    for method in ("post", "put", "delete", "patch"):
        assert getattr(client, method)("/status").status_code == 405


def test_requests_never_call_prometheus_and_request_data_never_reaches_a_query():
    client, service, fake, _, _ = make()
    service.refresh_once()
    calls = len(fake.calls)
    hostile = "up%7Bx%3D%22y%22%7D&q=1&query=drop&start=0&end=1&step=1&window=999999"
    for _ in range(10):
        r = client.get(f"/status?query={hostile}", headers={"X-Query": "evil", "Host": "evil"})
        assert r.status_code == 200
    assert len(fake.calls) == calls
    assert {c[0] for c in fake.calls} == {q for _, q, _, _ in history.RANGE_QUERIES}


def test_the_handler_declares_no_parameters_at_all():
    _, service, _, _, _ = make()
    app = create_app(history_service=service)
    (route,) = [r for r in app.routes if getattr(r, "path", None) == "/status"]
    dependant = route.dependant
    assert not dependant.query_params and not dependant.path_params
    assert not dependant.header_params and not dependant.cookie_params
    assert not dependant.body_params
    assert route.methods == {"GET", "HEAD"}


def test_the_page_carries_no_label_address_or_pod_text():
    client, service, _, _, _ = make()
    service.refresh_once()
    text = client.get("/status").text
    for leaked in ("backend-aaa", "10.0.0.9", "node-1", "internal.invalid", "registry.example"):
        assert leaked not in text
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)


def test_the_page_turns_stale_with_the_clock():
    client, service, _, clock, _ = make()
    service.refresh_once()
    assert "history-stale" not in client.get("/status").text
    clock.now += service.stale_after + 1
    assert "history-stale" in client.get("/status").text


# ---- precedence over /{code} ----------------------------------------------------------


def test_status_can_never_be_a_short_code():
    assert not links.is_valid_code("status")  # 6 characters; codes are exactly 7
    assert len("status") != links.CODE_LENGTH


def test_the_status_route_wins_over_the_code_route_and_codes_still_work():
    client, service, _, _, _ = make(tokens=TOKENS)
    service.refresh_once()
    assert client.get("/status").status_code == 200
    code = client.post("/api/links", json={"url": "https://example.com"}, headers=AUTH).json()[
        "code"
    ]
    assert client.get(f"/{code}").status_code == 302
    assert client.get("/abcdefg").status_code == 404
    assert client.get("/statu").status_code == 404 and client.get("/status/").status_code in (
        307,
        404,
    )


def test_the_status_route_is_registered_before_the_code_route():
    _, service, _, _, _ = make()
    app = create_app(history_service=service)
    paths = [getattr(r, "path", None) for r in app.routes]
    assert paths.index("/status") < paths.index("/{code}")


def test_the_page_is_counted_under_its_own_route_not_the_slo_route():
    client, service, _, _, registry = make()
    service.refresh_once()
    client.get("/status")

    def n(route):
        labels = {"method": "GET", "route": route, "status": "200"}
        return registry.get_sample_value("http_request_duration_seconds_count", labels) or 0

    assert n("/status") == 1
    assert n("/{code}") == 0  # the redirect SLO never sees it


# ---- isolation from health and readiness (ADR 0016) --------------------------------


def test_health_and_readiness_ignore_a_prometheus_that_raises():
    fake = FakeRange()
    for q in list(fake.answers):
        fake.answers[q] = RuntimeError("prometheus exploded")
    registry = CollectorRegistry()
    service = HistoryService(fake, registry=registry, refresh_seconds=60)
    app = create_app(history_service=service, registry=registry, readiness_checks=[lambda: True])
    with TestClient(app) as client:  # the lifespan starts the refresher
        deadline = time.monotonic() + 3
        while len(fake.calls) < len(history.RANGE_QUERIES) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fake.calls, "the refresher never ran"
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").json() == {"status": "ready"}
        r = client.get("/status")
        assert r.status_code == 200 and "warming up" in r.text


def test_health_routes_do_not_touch_the_history_query():
    client, service, fake, _, _ = make()
    client.get("/healthz")
    client.get("/readyz")
    assert fake.calls == []


# ---- lifespan -----------------------------------------------------------------------


def test_the_lifespan_starts_and_stops_both_services():
    registry = CollectorRegistry()
    history_service = HistoryService(FakeRange(), registry=registry, refresh_seconds=60)
    status_service = StatusService(FakePrometheus(), registry=registry)
    app = create_app(
        history_service=history_service, status_service=status_service, registry=registry
    )
    assert not history_service.running and not status_service.running
    with TestClient(app) as client:
        assert history_service.running and status_service.running
        deadline = time.monotonic() + 3
        while history_service.read() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "<svg" in client.get("/status").text
    assert not history_service.running and not status_service.running


def test_a_caller_lifespan_still_runs_around_the_history_refresher():
    events = []

    @asynccontextmanager
    async def mine(_):
        events.append("start")
        yield
        events.append("stop")

    service = HistoryService(FakeRange(), registry=CollectorRegistry(), refresh_seconds=60)
    app = create_app(history_service=service, lifespan=mine)
    with TestClient(app):
        assert events == ["start"] and service.running
    assert events == ["start", "stop"] and not service.running


def test_history_alone_still_starts_without_a_status_service():
    service = HistoryService(FakeRange(), registry=CollectorRegistry(), refresh_seconds=60)
    with TestClient(create_app(history_service=service)):
        assert service.running


def test_nothing_runs_when_both_are_off():
    with TestClient(create_app()) as client:
        assert client.get("/status").status_code == 404


# ---- the /api/status flag and the landing strip -------------------------------------


def test_the_history_flag_in_the_payload_follows_the_feature():
    registry = CollectorRegistry()
    status_service = StatusService(
        FakePrometheus(), registry=registry, wall=lambda: 1_795_000_100.0
    )
    status_service.refresh_once()
    on = TestClient(
        create_app(
            status_service=status_service,
            history_service=HistoryService(FakeRange(), registry=CollectorRegistry()),
        )
    )
    off = TestClient(create_app(status_service=status_service))
    assert on.get("/api/status").json()["history"] is True
    assert off.get("/api/status").json()["history"] is False


def test_the_landing_strip_has_a_hidden_history_link_without_an_address():
    html = TestClient(create_app()).get("/").text
    link = re.search(r"<a[^>]*\bid=\"t-history\"[^>]*>", html).group(0)
    assert "hidden" in link and "href" not in link  # no link at all without the script


def test_the_landing_script_shows_the_link_only_when_history_is_exactly_true():
    html = TestClient(create_app()).get("/").text
    script = html.split("<script>")[1].split("</script>")[0]
    assert "history === true" in script
    assert "'/status'" in script and "'History charts'" in script
    assert "innerHTML" not in script
    urls = re.findall(r"https?://[^\s'\"]+", script)
    assert urls == ["https://github.com/Sandeep-RN/oci-k8s-gitops-demo-app/commit/"]
    assert html.lower().count("<script") == 1


def test_the_nav_of_the_landing_page_does_not_link_to_the_page_unconditionally():
    # Off means 404: a static link would point at it.
    html = TestClient(create_app()).get("/").text
    assert 'href="/status"' not in html
