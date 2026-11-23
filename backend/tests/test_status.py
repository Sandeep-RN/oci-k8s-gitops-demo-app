# The status snapshot builder and its background refresher (demo #230, ADR 0026).
# No network: Prometheus is a fake callable.
import json
import math
import threading
import time

import pytest
from prometheus_client import CollectorRegistry

from app import status
from app.status import Sample, StatusService
from tests.status_helpers import SHA, Clock, FakePrometheus, default_answers

# The exact allowlist (ADR 0026): adding a query means changing this test on purpose.
EXPECTED_QUERIES = {
    "slo:redirect_error_budget:remaining_ratio7d",
    "slo:redirect_errors:ratio_rate7d",
    "slo:redirect_latency_good:ratio_rate7d",
    "slo:redirect_requests:rate5m",
    '1-avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))',
    "1-node_memory_MemAvailable_bytes/node_memory_MemTotal_bytes",
    'sum(rate(node_network_transmit_bytes_total{device!~"lo|veth.*|cni.*|flannel.*"}[5m]))',
    'count(kube_pod_status_phase{phase="Running"}==1)',
    'kube_pod_container_info{namespace="link-shortener",container="backend"}',
    'max(kube_pod_start_time{namespace="link-shortener",pod=~"backend-.*"})',
    "argocd_app_info",
}

# Which output fields each query feeds: a failed query nulls exactly these.
FIELDS_BY_QUERY = {
    status.Q_ERROR_BUDGET: [("slo", "error_budget_remaining")],
    status.Q_ERROR_RATIO: [("slo", "availability_7d")],
    status.Q_LATENCY_GOOD: [("slo", "latency_good_7d")],
    status.Q_REQUEST_RATE: [("traffic", "requests_per_s_5m")],
    status.Q_NODE_CPU: [("node", "cpu_ratio")],
    status.Q_NODE_MEMORY: [("node", "mem_ratio")],
    status.Q_NODE_NET_TX: [("node", "net_tx_bytes_per_s")],
    status.Q_PODS_RUNNING: [("cluster", "pods_running")],
    status.Q_BACKEND_IMAGE: [("delivery", "commit"), ("delivery", "commit_short")],
    status.Q_BACKEND_START: [("delivery", "running_since")],
    status.Q_ARGOCD_APPS: [("delivery", "argocd")],
}


def make_service(fake=None, clock=None, **kwargs):
    fake = fake if fake is not None else FakePrometheus()
    clock = clock if clock is not None else Clock()
    kwargs.setdefault("registry", CollectorRegistry())
    kwargs.setdefault("wall", lambda: 1_795_000_100.0)
    return StatusService(fake, monotonic=clock, **kwargs), fake, clock


def get(payload, path):
    for key in path:
        payload = payload[key]
    return payload


# ---- the allowlist --------------------------------------------------------------


def test_the_allowlist_is_exactly_this_set_of_constants():
    assert set(status.QUERIES) == EXPECTED_QUERIES
    assert len(status.QUERIES) == len(EXPECTED_QUERIES)  # no duplicates
    assert isinstance(status.QUERIES, tuple)
    assert all(isinstance(q, str) for q in status.QUERIES)


def test_a_refresh_sends_exactly_the_allowlist_and_nothing_else():
    service, fake, _ = make_service()
    assert service.refresh_once() is True
    assert sorted(fake.calls) == sorted(status.QUERIES)


def test_the_node_network_query_does_not_expose_receive():
    # Receive was image pulls, not visitor traffic (ADR 0026).
    assert not any("receive" in q for q in status.QUERIES)


# ---- the payload ----------------------------------------------------------------


def test_the_payload_has_the_agreed_shape_and_rounded_values():
    service, _, _ = make_service()
    service.refresh_once()
    p = service.read()
    assert list(p) == ["as_of", "stale", "slo", "traffic", "node", "cluster", "delivery"]
    assert p["as_of"] == "2026-11-18T11:08:20Z"
    assert p["stale"] is False
    assert p["slo"] == {
        "target": 99.5,
        "window_days": 7,
        "error_budget_remaining": 0.9877,
        "availability_7d": 0.99877,  # 1 - errors, never the raw error ratio
        "latency_good_7d": 0.99877,
    }
    assert p["traffic"] == {"requests_per_s_5m": 0.033}
    assert p["node"] == {"cpu_ratio": 0.182, "mem_ratio": 0.375, "net_tx_bytes_per_s": 4713}
    assert p["cluster"] == {"pods_running": 41}
    assert p["delivery"] == {
        "commit": SHA,
        "commit_short": SHA[:7],
        "running_since": "2026-11-18T11:06:40Z",
        "argocd": {"apps_total": 3, "synced": 2, "healthy": 2},
    }


def test_the_payload_is_json_serialisable_and_leaks_no_labels():
    service, _, _ = make_service()
    service.refresh_once()
    text = json.dumps(service.read())
    for leaked in ("backend-6d9f", "10.0.0.9", "node-1", "internal.invalid", "secret-app-name"):
        assert leaked not in text
    assert "registry.example" not in text  # the image repository stays out; only the SHA shows


def test_an_empty_argocd_answer_gives_null_not_an_error():
    # The Argo CD series do not exist until a ServiceMonitor scrapes them.
    fake = FakePrometheus()
    fake.answers[status.Q_ARGOCD_APPS] = []
    service, _, _ = make_service(fake)
    assert service.refresh_once() is True
    assert service.read()["delivery"]["argocd"] is None


@pytest.mark.parametrize("query", list(FIELDS_BY_QUERY))
@pytest.mark.parametrize("failure", ["raises", "empty"])
def test_a_failed_or_empty_query_nulls_only_its_own_fields(query, failure):
    fake = FakePrometheus()
    fake.answers[query] = status.QueryError("boom") if failure == "raises" else []
    service, _, _ = make_service(fake)
    assert service.refresh_once() is True  # partial results are fine
    p = service.read()
    nulled = set(FIELDS_BY_QUERY[query])
    for fields in FIELDS_BY_QUERY.values():
        for path in fields:
            if path in nulled:
                assert get(p, path) is None, path
            else:
                assert get(p, path) is not None, path


def test_a_failed_query_is_counted_as_a_failed_refresh_but_the_snapshot_still_updates():
    registry = CollectorRegistry()
    fake = FakePrometheus()
    fake.answers[status.Q_NODE_CPU] = status.QueryError("boom")
    service, _, _ = make_service(fake, registry=registry)
    service.refresh_once()
    assert registry.get_sample_value("status_refresh_failures_total") == 1
    assert service.read() is not None


@pytest.mark.parametrize(
    "bad", [math.nan, math.inf, -math.inf, "0.5", None, True, [1], {"a": 1}], ids=repr
)
def test_non_finite_or_non_numeric_values_become_null(bad):
    fake = FakePrometheus()
    for q in (status.Q_NODE_CPU, status.Q_ERROR_BUDGET, status.Q_PODS_RUNNING):
        fake.answers[q] = [Sample({}, bad)]
    service, _, _ = make_service(fake)
    service.refresh_once()
    p = service.read()
    assert p["node"]["cpu_ratio"] is None
    assert p["slo"]["error_budget_remaining"] is None
    assert p["cluster"]["pods_running"] is None
    assert p["slo"]["latency_good_7d"] is not None  # untouched fields survive


def test_ratios_are_clamped_and_counts_are_never_negative():
    fake = FakePrometheus()
    fake.answers[status.Q_ERROR_BUDGET] = [Sample({}, -3.0)]  # budget overspent
    fake.answers[status.Q_NODE_CPU] = [Sample({}, 7.5)]
    fake.answers[status.Q_ERROR_RATIO] = [Sample({}, 4.0)]  # nonsense: availability would be < 0
    fake.answers[status.Q_REQUEST_RATE] = [Sample({}, -1.0)]
    fake.answers[status.Q_PODS_RUNNING] = [Sample({}, -2.0)]
    service, _, _ = make_service(fake)
    service.refresh_once()
    p = service.read()
    assert p["slo"]["error_budget_remaining"] == 0.0
    assert p["node"]["cpu_ratio"] == 1.0
    assert p["slo"]["availability_7d"] == 0.0
    assert p["traffic"]["requests_per_s_5m"] == 0.0
    assert p["cluster"]["pods_running"] == 0


@pytest.mark.parametrize(
    "image, expected",
    [
        (f"repo.example.invalid/a/b:{SHA}", SHA),
        (f"repo.example.invalid:5000/a/b:{SHA}", SHA),  # a registry port is not a tag
        (f"repo.example.invalid/a/b:{SHA}@sha256:{'a' * 64}", SHA),  # a digest is dropped
        ("repo.example.invalid/a/b:latest", None),
        ("repo.example.invalid/a/b", None),
        ("repo.example.invalid:5000/a/b", None),
        (f"repo.example.invalid/a/b:{SHA.upper()}", None),  # lowercase hex only
        (f"repo.example.invalid/a/b:{SHA}0", None),
        ("repo.example.invalid/a/b:0123456", None),  # short SHAs are not accepted
        (f"repo.example.invalid/a/b:{SHA[:-1]}\n", None),
        (f"repo.example.invalid/a/b:{SHA}<script>", None),
        ("", None),
    ],
)
def test_only_a_full_lowercase_sha_tag_is_accepted_as_the_running_commit(image, expected):
    fake = FakePrometheus()
    fake.answers[status.Q_BACKEND_IMAGE] = [Sample({"image": image}, 1.0)]
    service, _, _ = make_service(fake)
    service.refresh_once()
    delivery = service.read()["delivery"]
    assert delivery["commit"] == expected
    assert delivery["commit_short"] == (expected[:7] if expected else None)


def test_a_rollout_with_two_different_tags_gives_no_commit_rather_than_a_guess():
    other = "f" * 40
    fake = FakePrometheus()
    fake.answers[status.Q_BACKEND_IMAGE] = [
        Sample({"image": f"r.invalid/b:{SHA}"}, 1.0),
        Sample({"image": f"r.invalid/b:{other}"}, 1.0),
    ]
    service, _, _ = make_service(fake)
    service.refresh_once()
    assert service.read()["delivery"]["commit"] is None


def test_the_image_label_missing_or_not_text_gives_no_commit():
    fake = FakePrometheus()
    fake.answers[status.Q_BACKEND_IMAGE] = [Sample({}, 1.0), Sample({"image": 5}, 1.0)]
    service, _, _ = make_service(fake)
    service.refresh_once()
    assert service.read()["delivery"]["commit"] is None


@pytest.mark.parametrize("bad", [0.0, -5.0, 1e18, 1e300])
def test_an_implausible_start_time_becomes_null(bad):
    fake = FakePrometheus()
    fake.answers[status.Q_BACKEND_START] = [Sample({}, bad)]
    service, _, _ = make_service(fake)
    service.refresh_once()
    assert service.read()["delivery"]["running_since"] is None


def test_argocd_counts_only_and_a_sample_without_labels_still_counts_as_an_app():
    fake = FakePrometheus()
    fake.answers[status.Q_ARGOCD_APPS] = [
        Sample({"sync_status": "Synced", "health_status": "Healthy"}, 1),
        Sample({}, 1),
        Sample({"sync_status": 7, "health_status": ["x"]}, 1),
    ]
    service, _, _ = make_service(fake)
    service.refresh_once()
    assert service.read()["delivery"]["argocd"] == {"apps_total": 3, "synced": 1, "healthy": 1}


# ---- failure behaviour ----------------------------------------------------------


def test_nothing_is_readable_until_a_refresh_has_succeeded():
    service, _, _ = make_service()
    assert service.read() is None


def test_when_every_query_fails_nothing_is_readable_and_the_refresh_reports_failure():
    registry = CollectorRegistry()
    fake = FakePrometheus()
    fake.fail_all()
    service, _, _ = make_service(fake, registry=registry)
    assert service.refresh_once() is False
    assert service.read() is None
    assert registry.get_sample_value("status_refresh_failures_total") == 1


def test_a_failed_refresh_keeps_the_last_good_snapshot():
    service, fake, clock = make_service()
    service.refresh_once()
    before = service.read()
    fake.fail_all()
    clock.now += 30
    assert service.refresh_once() is False
    after = service.read()
    assert after["as_of"] == before["as_of"]
    assert after["slo"] == before["slo"]
    assert after["stale"] is False  # 30 s old: not stale yet


def test_the_snapshot_turns_stale_after_90_seconds_without_a_success():
    service, fake, clock = make_service()
    service.refresh_once()
    fake.fail_all()
    clock.now += 90
    assert service.read()["stale"] is False  # exactly 90 s is still fresh
    clock.now += 0.5
    assert service.read()["stale"] is True
    assert service.read()["slo"]["target"] == 99.5  # still the last good numbers


def test_a_later_success_clears_stale():
    service, fake, clock = make_service()
    service.refresh_once()
    saved = dict(fake.answers)
    fake.fail_all()
    clock.now += 200
    assert service.read()["stale"] is True
    fake.answers.update(saved)
    service.refresh_once()
    assert service.read()["stale"] is False


def test_the_snapshot_age_gauge_follows_the_clock_and_is_minus_one_before_any_success():
    registry = CollectorRegistry()
    service, _, clock = make_service(registry=registry)
    assert registry.get_sample_value("status_snapshot_age_seconds") == -1
    service.refresh_once()
    clock.now += 42
    assert registry.get_sample_value("status_snapshot_age_seconds") == 42


def test_a_refresh_never_raises_even_for_a_surprising_exception():
    fake = FakePrometheus()
    for q in list(fake.answers):
        fake.answers[q] = RuntimeError("not a QueryError")
    service, _, _ = make_service(fake)
    assert service.refresh_once() is False


def test_the_result_of_a_query_must_be_a_list_of_samples():
    fake = FakePrometheus()
    original = fake.__call__

    class Weird(FakePrometheus):
        def __call__(self, promql):
            if promql == status.Q_NODE_CPU:
                return "not a list"  # type: ignore[return-value]
            if promql == status.Q_NODE_MEMORY:
                return [object()]  # type: ignore[list-item]
            return original(promql)

    weird = Weird()
    service, _, _ = make_service(weird)
    assert service.refresh_once() is True
    p = service.read()
    assert p["node"]["cpu_ratio"] is None
    assert p["node"]["mem_ratio"] is None
    assert p["node"]["net_tx_bytes_per_s"] is not None


# ---- single flight and the time cap ---------------------------------------------


def test_refreshes_never_overlap():
    release = threading.Event()
    entered = threading.Event()

    class Slow(FakePrometheus):
        def __call__(self, promql):
            entered.set()
            release.wait(5)
            return super().__call__(promql)

    service, _, _ = make_service(Slow())
    first = threading.Thread(target=service.refresh_once)
    first.start()
    assert entered.wait(2)
    assert service.refresh_once() is False  # skipped: one is already running
    release.set()
    first.join(5)
    assert service.read() is not None


def test_a_hung_query_cannot_hold_a_refresh_past_its_cap_and_others_still_land():
    release = threading.Event()

    class Hung(FakePrometheus):
        def __call__(self, promql):
            if promql == status.Q_NODE_CPU:
                release.wait(10)
            return super().__call__(promql)

    service, _, _ = make_service(Hung(), refresh_timeout=0.4)
    started = time.monotonic()
    ok = service.refresh_once()
    elapsed = time.monotonic() - started
    release.set()
    assert elapsed < 2.0
    assert ok is True
    p = service.read()
    assert p["node"]["cpu_ratio"] is None
    assert p["node"]["mem_ratio"] is not None


def test_a_refresh_after_a_hung_one_can_start():
    release = threading.Event()
    hang = {"on": True}

    class Hung(FakePrometheus):
        def __call__(self, promql):
            if hang["on"] and promql == status.Q_NODE_CPU:
                release.wait(10)
            return super().__call__(promql)

    service, _, _ = make_service(Hung(), refresh_timeout=0.3)
    service.refresh_once()
    hang["on"] = False
    assert service.refresh_once() is True
    release.set()
    assert service.read()["node"]["cpu_ratio"] == 0.182


# ---- the background thread ------------------------------------------------------


def wait_for(predicate, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_the_refresher_thread_refreshes_repeatedly_and_stops_cleanly():
    service, fake, _ = make_service(refresh_seconds=0.05)
    service.start()
    try:
        assert wait_for(lambda: len(fake.calls) >= 2 * len(status.QUERIES))
    finally:
        service.stop()
    assert not service.running
    settled = len(fake.calls)
    time.sleep(0.2)
    assert len(fake.calls) == settled  # nothing runs after stop


def test_the_thread_is_a_daemon_and_start_is_idempotent():
    service, _, _ = make_service(refresh_seconds=60)
    service.start()
    service.start()
    try:
        threads = [t for t in threading.enumerate() if t.name == "status-refresher"]
        assert len(threads) == 1
        assert threads[0].daemon is True
    finally:
        service.stop()


def test_stop_returns_quickly_even_between_refreshes():
    service, _, _ = make_service(refresh_seconds=3600)
    service.start()
    assert wait_for(lambda: service.read() is not None)
    started = time.monotonic()
    service.stop()
    assert time.monotonic() - started < 2.0


def test_the_thread_survives_a_prometheus_that_is_down():
    fake = FakePrometheus()
    fake.fail_all()
    service, _, _ = make_service(fake, refresh_seconds=0.05)
    service.start()
    try:
        assert wait_for(lambda: len(fake.calls) >= 2 * len(status.QUERIES))
        assert service.running
    finally:
        service.stop()


# ---- configuration from the environment -----------------------------------------


def test_unset_or_blank_url_means_the_feature_is_off():
    assert status.service_from_env({}, CollectorRegistry()) is None
    assert status.service_from_env({"STATUS_PROMETHEUS_URL": "  "}, CollectorRegistry()) is None


def test_a_valid_url_builds_a_service_with_defaults():
    service = status.service_from_env(
        {"STATUS_PROMETHEUS_URL": "http://prometheus.example.invalid:9090"}, CollectorRegistry()
    )
    assert isinstance(service, StatusService)
    assert service.refresh_seconds == 30


def test_an_invalid_url_turns_the_feature_off_and_logs_without_stopping_the_app(caplog):
    # A cosmetic feature must not stop the app that serves the redirects.
    service = status.service_from_env({"STATUS_PROMETHEUS_URL": "ftp://x"}, CollectorRegistry())
    assert service is None
    assert "STATUS_PROMETHEUS_URL" in caplog.text


@pytest.mark.parametrize(
    "value, expected", [("45", 45), ("5", 5), ("0", 30), ("-3", 30), ("abc", 30), ("1", 30)]
)
def test_the_refresh_interval_comes_from_the_env_with_a_floor(value, expected):
    # Below 5 s would hammer Prometheus: the value is refused and the default used.
    service = status.service_from_env(
        {"STATUS_PROMETHEUS_URL": "http://p.invalid:9090", "STATUS_REFRESH_SECONDS": value},
        CollectorRegistry(),
    )
    assert service.refresh_seconds == expected


def test_default_answers_cover_the_whole_allowlist():
    # Guards the fake itself: a new query without a canned answer fails here.
    assert set(default_answers()) == set(status.QUERIES)
