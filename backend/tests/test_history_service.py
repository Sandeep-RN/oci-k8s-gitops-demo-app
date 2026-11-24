# The history snapshot and its refresher (demo #235, ADR 0027). No network except
# where a test says "real socket": Prometheus is a fake callable.
import re
import threading
import time

import pytest
from prometheus_client import CollectorRegistry

from app import history, history_page, status
from app.history import HistoryService
from app.status import QueryError, RangeSeries, StatusService
from tests.history_helpers import (
    NOW,
    SHA_A,
    SHA_B,
    SHA_C,
    FakeRange,
    default_range_answers,
    deploy_series,
    full_series,
    grid,
    image,
    spec,
)
from tests.loopback import DripServer
from tests.status_helpers import Clock, FakePrometheus

WALL = NOW + 123.0  # a few minutes after a step boundary: the grid must round DOWN

# The exact allowlist (ADR 0027): adding a query means changing this test on purpose.
EXPECTED = (
    ("budget", "slo:redirect_error_budget:remaining_ratio7d", 7 * 86400, 1800),
    ("errors", "slo:redirect_errors:ratio_rate7d", 7 * 86400, 1800),
    ("latency", "slo:redirect_latency_good:ratio_rate7d", 7 * 86400, 1800),
    ("rps", "slo:redirect_requests:rate5m", 86400, 300),
    ("cpu", '1-avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))', 86400, 300),
    # aggregated: two node-exporter series (a pod replaced) must not blank the chart (review M2)
    ("mem", "avg(1-node_memory_MemAvailable_bytes/node_memory_MemTotal_bytes)", 86400, 300),
    (
        "net",
        'sum(rate(node_network_transmit_bytes_total{device!~"lo|veth.*|cni.*|flannel.*"}[5m]))',
        86400,
        300,
    ),
    ("pods", 'count(kube_pod_status_phase{phase="Running"}==1)', 86400, 300),
    (
        "deploys",
        'kube_pod_container_info{namespace="link-shortener",container="backend",pod=~"backend-.*"}',
        7 * 86400,
        900,
    ),
)


def make(fake=None, clock=None, **kwargs):
    fake = fake if fake is not None else FakeRange()
    clock = clock if clock is not None else Clock()
    kwargs.setdefault("registry", CollectorRegistry())
    kwargs.setdefault("wall", lambda: WALL)
    return HistoryService(fake, monotonic=clock, **kwargs), fake, clock


# ---- the allowlist --------------------------------------------------------------------


def test_the_range_allowlist_is_exactly_this_set_of_constants():
    assert tuple(history.RANGE_QUERIES) == EXPECTED
    assert [k for k, *_ in history.RANGE_QUERIES] == [k for k, *_ in EXPECTED]


def test_every_entry_is_plain_text_and_whole_numbers_and_keys_are_unique():
    keys = [k for k, *_ in history.RANGE_QUERIES]
    assert len(set(keys)) == len(keys)
    for key, promql, window, step in history.RANGE_QUERIES:
        assert isinstance(key, str) and isinstance(promql, str)
        assert type(window) is int and type(step) is int
        assert 0 < step < window <= 7 * 86400
        assert window % step == 0
        assert window // step + 1 <= 700  # far from Prometheus's 11,000 limit


def test_no_query_has_an_ad_hoc_range_selector_longer_than_one_hour():
    # ADR 0027: a long `rate(...[7d])` over a 7-day range is expensive. The 7-day
    # charts read RECORDING RULES; the longest ad hoc range used is [5m].
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    longest = 0
    for _, promql, _, _ in history.RANGE_QUERIES:
        assert not re.search(r"\[[^\]]*:[^\]]*\]", promql), "no subqueries"
        for n, unit in re.findall(r"\[(\d+)([smhdw])\]", promql):
            longest = max(longest, int(n) * seconds[unit])
        assert not re.search(r"\[[^\]]*\]", re.sub(r"\[(\d+)([smhdw])\]", "", promql))
    assert longest == 300


def test_the_allowlist_shares_its_promql_with_the_instant_queries_where_it_can():
    shared = {q for _, q, _, _ in history.RANGE_QUERIES} & set(status.QUERIES)
    # All nine: the memory query is aggregated for both (one constant, review M2).
    assert len(shared) == 9
    assert history.Q_NODE_MEMORY_RANGE == status.Q_NODE_MEMORY


def test_every_single_line_chart_query_is_aggregated_or_a_recording_rule():
    # Two series (a replaced exporter pod, a second node) must never be possible for a
    # single-line chart: that chart would read "no data" until the old series aged out.
    for key, promql, _, _ in history.RANGE_QUERIES:
        if key == "deploys":
            continue  # one series per pod on purpose
        assert re.match(r"^(1-)?(avg|sum|max|min|count)\(", promql) or re.fullmatch(
            r"slo:[a-z0-9_:]+", promql
        ), f"{key}: {promql} is not aggregated"


def test_a_refresh_sends_exactly_the_allowlist_with_windows_from_the_server_clock():
    service, fake, _ = make()
    assert service.refresh_once() is True
    assert [c[0] for c in fake.calls] == [q for _, q, _, _ in history.RANGE_QUERIES]  # in order
    for (_key, promql, window, step), (sent, start, end, sent_step) in zip(
        history.RANGE_QUERIES, fake.calls, strict=True
    ):
        assert sent == promql and sent_step == step
        assert end == (WALL // step) * step  # rounded down to a step boundary
        assert end - start == window
        assert end <= WALL


def test_queries_run_one_after_another_on_one_thread():
    seen = {"now": 0, "max": 0, "threads": set()}
    lock = threading.Lock()
    inner = FakeRange()

    def slow(promql, start, end, step):
        with lock:
            seen["now"] += 1
            seen["max"] = max(seen["max"], seen["now"])
            seen["threads"].add(threading.current_thread().name)
        time.sleep(0.01)
        try:
            return inner(promql, start, end, step)
        finally:
            with lock:
                seen["now"] -= 1

    service, _, _ = make(slow)
    assert service.refresh_once() is True
    assert seen["max"] == 1
    assert len(seen["threads"]) == 1
    assert next(iter(seen["threads"])).startswith("history-query")


def test_the_worker_is_a_daemon_thread():
    # A stuck query must never stop the pod from exiting on SIGTERM.
    flags = []
    inner = FakeRange()

    def spy(promql, start, end, step):
        flags.append(threading.current_thread().daemon)
        return inner(promql, start, end, step)

    service, _, _ = make(spy)
    service.refresh_once()
    assert flags and all(flags)


# ---- the snapshot ---------------------------------------------------------------------


def test_nothing_is_readable_until_a_refresh_has_succeeded():
    service, fake, _ = make()
    assert service.read() is None
    assert service.page() is None
    assert fake.calls == []


def test_series_land_on_the_step_grid_and_gaps_are_none_never_zero():
    answers = default_range_answers(WALL)
    start, end, step = grid("cpu", WALL)
    answers[spec("cpu")[1]] = [
        RangeSeries({}, ((start, 0.5), (start + step, 0.25), (start + 5 * step, 0.75)))
    ]
    service, _, _ = make(FakeRange(answers))
    service.refresh_once()
    cpu = service.read().charts["cpu"]
    assert (cpu.start, cpu.end, cpu.step) == (start, end, step)
    assert len(cpu.values) == int((end - start) // step) + 1
    assert cpu.values[:6] == (0.5, 0.25, None, None, None, 0.75)
    assert all(v is None for v in cpu.values[6:])  # and never 0.0
    assert 0.0 not in cpu.values


def test_points_off_the_grid_or_outside_it_are_ignored():
    answers = default_range_answers(WALL)
    start, end, step = grid("pods", WALL)
    answers[spec("pods")[1]] = [
        RangeSeries({}, ((start + 7, 1.0), (start - step, 2.0), (end + step, 3.0), (end, 4.0)))
    ]
    service, _, _ = make(FakeRange(answers))
    service.refresh_once()
    values = service.read().charts["pods"].values
    assert [v for v in values if v is not None] == [4.0]


@pytest.mark.parametrize(
    "key, raw, expected",
    [
        ("budget", 1.7, 1.0),
        ("budget", -3.0, 0.0),
        ("errors", 2.0, 1.0),
        ("latency", -1.0, 0.0),
        ("cpu", 5.0, 1.0),
        ("mem", -0.2, 0.0),
        ("rps", -4.0, 0.0),
        ("net", -1.0, 0.0),
        ("pods", -2.0, 0.0),
        ("pods", 1e12, 1e6),
        # subnormals and noise are zero, not a number that breaks arithmetic (review M1)
        ("rps", 5e-324, 0.0),
        ("net", 1e-320, 0.0),
        ("rps", -5e-324, 0.0),
    ],
)
def test_values_are_clamped_to_what_the_metric_can_mean(key, raw, expected):
    answers = default_range_answers(WALL)
    start, _, _ = grid(key, WALL)
    answers[spec(key)[1]] = [RangeSeries({}, ((start, raw),))]
    service, _, _ = make(FakeRange(answers))
    service.refresh_once()
    assert service.read().charts[key].values[0] == expected


def test_an_empty_answer_is_a_chart_with_no_values_not_a_failure():
    answers = default_range_answers(WALL)
    answers[spec("rps")[1]] = []
    service, _, _ = make(FakeRange(answers), registry=(registry := CollectorRegistry()))
    service.refresh_once()
    rps = service.read().charts["rps"]
    assert rps is not None and all(v is None for v in rps.values)
    assert registry.get_sample_value("status_history_refresh_failures_total") == 0


def test_more_than_one_series_for_a_single_line_chart_fails_that_chart():
    # A merge would invent a number; a gap is honest.
    answers = default_range_answers(WALL)
    answers[spec("mem")[1]] = [full_series("mem", now=WALL), full_series("mem", now=WALL)]
    service, _, _ = make(FakeRange(answers), registry=(registry := CollectorRegistry()))
    service.refresh_once()
    assert service.read().charts["mem"] is None
    assert service.read().charts["cpu"] is not None
    assert registry.get_sample_value("status_history_refresh_failures_total") == 1


def test_one_failed_chart_leaves_the_others_alone_and_is_counted_once():
    fake = FakeRange()
    fake.fail("cpu")
    service, _, _ = make(fake, registry=(registry := CollectorRegistry()))
    assert service.refresh_once() is True
    charts = service.read().charts
    assert charts["cpu"] is None
    assert all(charts[k] is not None for k, *_ in EXPECTED if k != "cpu")
    assert registry.get_sample_value("status_history_refresh_failures_total") == 1


@pytest.mark.parametrize("failure", [QueryError("down"), RuntimeError("boom"), ValueError("x")])
def test_any_exception_from_a_query_only_fails_that_chart(failure):
    fake = FakeRange()
    fake.answers[spec("net")[1]] = failure
    service, _, _ = make(fake)
    service.refresh_once()
    assert service.read().charts["net"] is None
    assert service.read().charts["pods"] is not None


@pytest.mark.parametrize("junk", [None, "text", 7, [1, 2], [object()], {"a": 1}])
def test_an_answer_that_is_not_a_list_of_series_fails_that_chart(junk):
    fake = FakeRange()
    fake.answers[spec("pods")[1]] = junk  # FakeRange does list(junk): some raise, some are junk
    service, _, _ = make(fake)
    service.refresh_once()
    assert service.read().charts["pods"] is None
    assert service.read().charts["cpu"] is not None


def test_when_every_query_fails_nothing_is_readable_and_it_is_counted():
    fake = FakeRange()
    fake.fail_all()
    service, _, _ = make(fake, registry=(registry := CollectorRegistry()))
    assert service.refresh_once() is False
    assert service.read() is None and service.page() is None
    assert registry.get_sample_value("status_history_refresh_failures_total") == 1


def test_a_failed_chart_keeps_its_last_good_data_with_its_own_age():
    service, fake, clock = make()
    times = iter([WALL, WALL + 300.0])
    service._wall = lambda: next(times)  # noqa: SLF001
    service.refresh_once()
    first = service.read().charts["cpu"]
    fake.fail("cpu")
    service.refresh_once()
    again = service.read()
    assert again.charts["cpu"] is first  # the last good one, untouched
    assert again.charts["cpu"].as_of == WALL
    assert again.as_of == WALL + 300.0  # the snapshot as a whole is newer
    assert again.charts["pods"].as_of == WALL + 300.0


def test_a_refresh_where_everything_fails_keeps_the_whole_last_snapshot():
    service, fake, _ = make()
    service.refresh_once()
    before = service.read()
    fake.fail_all()
    assert service.refresh_once() is False
    assert service.read() is before


def test_the_snapshot_is_replaced_whole_never_half_written():
    gate, entered = threading.Event(), threading.Event()
    inner = FakeRange()

    def blocking(promql, start, end, step):
        if promql == spec("pods")[1]:
            entered.set()
            gate.wait(5)
        return inner(promql, start, end, step)

    service, _, _ = make(blocking)
    done = threading.Thread(target=service.refresh_once, daemon=True)
    done.start()
    assert entered.wait(3)
    assert service.read() is None  # four charts are done, but nothing is visible yet
    gate.set()
    done.join(5)
    assert service.read() is not None and service.read().charts["budget"] is not None


# ---- deploy markers -------------------------------------------------------------------


def deploys_of(service):
    return service.read().charts["deploys"].marks


def test_a_marker_is_the_first_time_each_tag_appears_shown_as_7_characters():
    service, _, _ = make(FakeRange(default_range_answers(WALL)))
    service.refresh_once()
    marks = deploys_of(service)
    start, end, step = grid("deploys", WALL)
    n = int((end - start) // step) + 1
    third = n // 3
    # SHA_A was already running when the data begins: that is not a deploy.
    assert [m.short for m in marks] == [SHA_B[:7], SHA_C[:7]]
    assert [m.ts for m in marks] == [start + third * step, start + 2 * third * step]
    assert all(len(m.short) == 7 for m in marks)


def test_two_pods_on_the_same_tag_make_one_marker():
    service, _, _ = make()
    service.refresh_once()
    assert len({m.short for m in deploys_of(service)}) == len(deploys_of(service))


@pytest.mark.parametrize(
    "bad",
    [
        "registry.example.invalid/app/backend:latest",
        "registry.example.invalid/app/backend:" + "A" * 40,  # upper case
        "registry.example.invalid/app/backend:" + "a" * 39,
        "registry.example.invalid/app/backend:" + "a" * 41,
        "registry.example.invalid/app/backend:" + "g" * 40,
        "registry.example.invalid/app/backend:<script>alert(1)</script>",
        "registry.example.invalid/app/backend:" + "a" * 40 + "\n",
        "registry.example.invalid/app/backend:" + "a" * 40 + "<b>",
        "backend",
        "",
        None,
        5,
    ],
)
def test_a_tag_that_is_not_exactly_40_lowercase_hex_never_becomes_a_marker(bad):
    answers = default_range_answers(WALL)
    start, end, step = grid("deploys", WALL)
    good = deploy_series(WALL)
    evil = RangeSeries(labels={"image": bad}, points=((start + 40 * step, 1.0),))
    answers[spec("deploys")[1]] = [*good, evil]
    service, _, _ = make(FakeRange(answers))
    service.refresh_once()
    shorts = [m.short for m in deploys_of(service)]
    assert shorts == [SHA_B[:7], SHA_C[:7]]
    assert all(re.fullmatch(r"[0-9a-f]{7}", s) for s in shorts)


def test_a_series_without_an_image_label_is_ignored():
    answers = default_range_answers(WALL)
    start, _, step = grid("deploys", WALL)
    answers[spec("deploys")[1]] = [
        *deploy_series(WALL),
        RangeSeries(labels={}, points=((start + 50 * step, 1.0),)),
    ]
    service, _, _ = make(FakeRange(answers))
    service.refresh_once()
    assert len(deploys_of(service)) == 2


def test_an_empty_deploy_answer_is_no_markers_not_a_failure():
    answers = default_range_answers(WALL)
    answers[spec("deploys")[1]] = []
    service, _, _ = make(FakeRange(answers), registry=(registry := CollectorRegistry()))
    service.refresh_once()
    assert deploys_of(service) == ()
    assert registry.get_sample_value("status_history_refresh_failures_total") == 0


def test_the_markers_are_capped_and_the_newest_are_kept():
    answers = default_range_answers(WALL)
    start, _, step = grid("deploys", WALL)

    # Tags that differ in their FIRST 7 characters: the old test's tags shared them all,
    # so it could not tell which markers were kept (review L1).
    def tag(i):
        return f"{i:07x}" + "0" * 33

    many = [
        RangeSeries(labels={"image": image(tag(i))}, points=((start + (i + 1) * step, 1.0),))
        for i in range(1, 80)
    ]
    answers[spec("deploys")[1]] = many
    service, _, _ = make(FakeRange(answers))
    service.refresh_once()
    marks = deploys_of(service)
    assert len(marks) == history.MAX_DEPLOY_MARKS
    # i=1 is the tag at the first sample (not a deploy); the newest 32 are i=48..79.
    assert [m.short for m in marks] == [f"{i:07x}" for i in range(48, 80)]
    assert [m.ts for m in marks] == [start + (i + 1) * step for i in range(48, 80)]


def test_a_rollback_gets_a_marker_a_to_b_to_a_is_two_transitions():
    # Review L4: first-seen-ever gave A -> B -> A one marker (B) and none for the way back.
    start, _, step = grid("deploys", WALL)

    def run(sha, a, b):
        return RangeSeries(
            labels={"image": image(sha)}, points=tuple((start + i * step, 1.0) for i in range(a, b))
        )

    answers = default_range_answers(WALL)
    answers[spec("deploys")[1]] = [run(SHA_A, 0, 10), run(SHA_B, 10, 20), run(SHA_A, 20, 30)]
    service, _, _ = make(FakeRange(answers))
    service.refresh_once()
    marks = deploys_of(service)
    # three appearances minus the one at the first sample
    assert [(m.short, m.ts) for m in marks] == [
        (SHA_B[:7], start + 10 * step),
        (SHA_A[:7], start + 20 * step),
    ]


def test_a_rolling_update_and_a_missed_scrape_make_no_extra_markers():
    start, _, step = grid("deploys", WALL)

    def pod(sha, idx):
        return RangeSeries(
            labels={"image": image(sha)}, points=tuple((start + i * step, 1.0) for i in idx)
        )

    answers = default_range_answers(WALL)
    # A: two pods, one misses a scrape at step 5. B arrives at 10 while A still runs.
    answers[spec("deploys")[1]] = [
        pod(SHA_A, [i for i in range(0, 12) if i != 5]),
        pod(SHA_A, range(0, 11)),
        pod(SHA_B, range(10, 20)),
    ]
    service, _, _ = make(FakeRange(answers))
    service.refresh_once()
    assert [(m.short, m.ts) for m in deploys_of(service)] == [(SHA_B[:7], start + 10 * step)]


# ---- never overlap, never pile up ------------------------------------------------------


def test_a_refresh_is_skipped_and_counted_while_a_worker_of_the_previous_one_is_alive():
    release, started = threading.Event(), threading.Event()
    inner = FakeRange()
    entered = []  # calls of stuck() itself: a second worker would add to this

    def stuck(promql, start, end, step):
        entered.append(promql)
        if promql == spec("budget")[1]:
            started.set()
            release.wait(10)
        return inner(promql, start, end, step)

    service, _, _ = make(stuck, refresh_timeout=0.3, registry=(registry := CollectorRegistry()))
    try:
        assert service.refresh_once() is False  # the first one runs into its deadline
        assert started.is_set()
        assert registry.get_sample_value("status_history_refresh_failures_total") == 1
        calls, threads = len(entered), threading.active_count()
        assert service.refresh_once() is False  # skipped: the worker is still alive
        # Asserted on stuck() itself: without the guard a second worker would call it
        # (and block in it), so this fails when the guard is removed (review L2).
        assert len(entered) == calls and threading.active_count() == threads
        assert registry.get_sample_value("status_history_refresh_failures_total") == 2
    finally:
        release.set()
    deadline = time.monotonic() + 3
    while service.refresh_once() is False and time.monotonic() < deadline:
        time.sleep(0.05)
    assert service.read() is not None  # and it recovers once the worker is gone


def test_threads_never_pile_up_across_refreshes_when_every_query_hangs():
    release = threading.Event()

    def hang(*_):
        release.wait(10)
        return []

    service, _, _ = make(hang, refresh_timeout=0.1)
    try:
        before = threading.active_count()
        for _ in range(8):
            service.refresh_once()
        assert threading.active_count() <= before + 1  # ONE stuck worker, not eight
    finally:
        release.set()


def test_refreshes_never_overlap():
    inside, overlaps = [0], [0]
    lock = threading.Lock()
    inner = FakeRange()

    def slow(promql, start, end, step):
        with lock:
            inside[0] += 1
            overlaps[0] = max(overlaps[0], inside[0])
        time.sleep(0.01)
        with lock:
            inside[0] -= 1
        return inner(promql, start, end, step)

    service, _, _ = make(slow)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(service.refresh_once())) for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert overlaps[0] == 1
    assert results.count(True) >= 1


def test_a_refresh_never_raises_even_when_rendering_the_page_fails(monkeypatch):
    service, _, _ = make(registry=(registry := CollectorRegistry()))
    service.refresh_once()
    good = service.read()

    def boom(*a, **k):
        raise RuntimeError("renderer bug")

    monkeypatch.setattr(history_page, "render_page", boom)
    assert service.refresh_once() is False  # no raise
    assert service.read() is good  # the last good snapshot stays
    assert service.page() is not None
    assert registry.get_sample_value("status_history_refresh_failures_total") == 1


# ---- stale and age ---------------------------------------------------------------------


def test_the_page_turns_stale_after_three_refresh_intervals_and_a_success_clears_it():
    service, fake, clock = make(refresh_seconds=300.0)
    service.refresh_once()
    fresh = service.page()
    assert "history-stale" not in fresh
    clock.now += 900
    assert service.page() == fresh  # exactly 3 intervals is still fine
    clock.now += 1
    stale = service.page()
    assert stale != fresh and "history-stale" in stale
    service.refresh_once()
    assert "history-stale" not in service.page()


def test_the_stale_threshold_is_three_intervals_and_can_be_given():
    service, _, _ = make(refresh_seconds=60.0)
    assert service.stale_after == 180.0
    service, _, _ = make(refresh_seconds=60.0, stale_after=10.0)
    assert service.stale_after == 10.0


def test_the_age_gauge_follows_the_clock_and_is_minus_one_before_any_success():
    service, _, clock = make(registry=(registry := CollectorRegistry()))
    assert registry.get_sample_value("status_history_snapshot_age_seconds") == -1
    service.refresh_once()
    clock.now += 42
    assert registry.get_sample_value("status_history_snapshot_age_seconds") == 42


# ---- metrics ---------------------------------------------------------------------------


def test_the_metrics_have_no_labels_and_exist_on_the_given_registry():
    service, fake, _ = make(registry=(registry := CollectorRegistry()))
    names = {m.name for m in registry.collect()}
    assert {"status_history_refresh_failures", "status_history_snapshot_age_seconds"} <= names
    for metric in registry.collect():
        assert all(not s.labels for s in metric.samples), metric.name


def test_both_services_share_one_registry_without_a_name_collision():
    registry = CollectorRegistry()
    StatusService(FakePrometheus(), registry=registry)
    HistoryService(FakeRange(), registry=registry)
    names = {m.name for m in registry.collect()}
    assert {
        "status_refresh_failures",
        "status_snapshot_age_seconds",
        "status_history_refresh_failures",
        "status_history_snapshot_age_seconds",
    } <= names


def test_a_service_without_a_registry_uses_a_private_one():
    HistoryService(FakeRange())
    HistoryService(FakeRange())  # a second one must not collide


# ---- the thread ------------------------------------------------------------------------


def test_the_refresher_thread_refreshes_repeatedly_and_stops_cleanly():
    service, fake, _ = make(refresh_seconds=0.05)
    service.start()
    try:
        deadline = time.monotonic() + 5
        while len(fake.calls) < 2 * len(EXPECTED) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(fake.calls) >= 2 * len(EXPECTED)
        assert service.running
    finally:
        service.stop()
    assert not service.running


def test_start_is_idempotent_and_the_thread_is_a_daemon():
    service, _, _ = make(refresh_seconds=30)
    service.start()
    service.start()
    try:
        threads = [t for t in threading.enumerate() if t.name == "history-refresher"]
        assert len(threads) == 1 and threads[0].daemon
    finally:
        service.stop()


def test_stop_returns_quickly_even_while_a_query_is_stuck():
    release = threading.Event()
    started = threading.Event()

    def stuck(*_):
        started.set()
        release.wait(30)
        return []

    service, _, _ = make(stuck, refresh_timeout=25.0, refresh_seconds=300)
    service.start()
    try:
        assert started.wait(3)
        t0 = time.monotonic()
        service.stop()
        assert time.monotonic() - t0 < 2.0  # not refresh_timeout + 1
        assert not service.running
    finally:
        release.set()


def test_the_thread_survives_a_prometheus_that_is_down():
    fake = FakeRange()
    fake.fail_all()
    service, _, _ = make(fake, refresh_seconds=0.05)
    service.start()
    try:
        time.sleep(0.3)
        assert service.running
        assert len(fake.calls) >= 2 * len(EXPECTED)
    finally:
        service.stop()


@pytest.mark.parametrize("mode", ["body", "headers"])
def test_a_real_dribbling_prometheus_leaks_no_threads_and_counts_failures(mode):
    server = DripServer(mode, interval=0.05 if mode == "body" else 0.2)
    try:
        client = status.PrometheusQuery(server.url, timeout=0.3)
        service, _, _ = make(
            client.query_range, refresh_timeout=1.0, registry=(registry := CollectorRegistry())
        )
        before = {t.ident for t in threading.enumerate()}
        assert service.refresh_once() is False
        assert registry.get_sample_value("status_history_refresh_failures_total") == 1
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            extra = [t for t in threading.enumerate() if t.ident not in before and t.name != "drip"]
            if not [t for t in extra if t.name.startswith("history-query")]:
                break
            time.sleep(0.05)
        assert not [t for t in threading.enumerate() if t.name.startswith("history-query")]
    finally:
        server.close()


# ---- configuration from the environment ------------------------------------------------

URL = "http://prometheus.example.invalid:9090"


def from_env(**env):
    return history.history_from_env(env, CollectorRegistry())


def test_history_is_off_unless_the_switch_is_on_and_the_url_is_valid():
    assert from_env() is None
    assert from_env(STATUS_PROMETHEUS_URL=URL) is None  # URL alone is not enough
    assert from_env(STATUS_HISTORY="on") is None  # the switch alone is not enough
    assert from_env(STATUS_HISTORY="on", STATUS_PROMETHEUS_URL="  ") is None
    for off in ("off", "", "true", "1", "yes", "ON!", "onn"):
        assert from_env(STATUS_HISTORY=off, STATUS_PROMETHEUS_URL=URL) is None, off
    assert isinstance(from_env(STATUS_HISTORY="on", STATUS_PROMETHEUS_URL=URL), HistoryService)


def test_a_config_map_value_with_a_trailing_newline_still_counts():
    assert from_env(STATUS_HISTORY="on\n", STATUS_PROMETHEUS_URL=URL + "\n")


def test_an_invalid_url_turns_history_off_and_logs_without_stopping_the_app(caplog):
    with caplog.at_level("ERROR", logger="app"):
        assert from_env(STATUS_HISTORY="on", STATUS_PROMETHEUS_URL="ftp://x") is None
    assert "STATUS_PROMETHEUS_URL" in caplog.text


def test_a_default_service_refreshes_every_300_seconds():
    service = from_env(STATUS_HISTORY="on", STATUS_PROMETHEUS_URL=URL)
    assert service.refresh_seconds == 300
    assert history.MIN_REFRESH_SECONDS == 60 and history.MAX_REFRESH_SECONDS == 900


@pytest.mark.parametrize(
    "value, expected",
    [
        ("60", 60),
        ("120", 120),
        ("900", 900),
        ("59", 300),
        ("901", 300),
        ("0", 300),
        ("-5", 300),
        ("abc", 300),
        ("", 300),
        ("1e9", 300),
        ("inf", 300),
        ("-inf", 300),
        ("nan", 300),
        ("NaN", 300),
    ],
)
def test_the_refresh_interval_comes_from_the_env_between_60_and_900(value, expected):
    service = from_env(
        STATUS_HISTORY="on", STATUS_PROMETHEUS_URL=URL, STATUS_HISTORY_REFRESH_SECONDS=value
    )
    assert service.refresh_seconds == expected


def test_an_unusable_interval_logs_the_allowed_range(caplog):
    with caplog.at_level("WARNING", logger="app"):
        from_env(
            STATUS_HISTORY="on", STATUS_PROMETHEUS_URL=URL, STATUS_HISTORY_REFRESH_SECONDS="nan"
        )
    assert "STATUS_HISTORY_REFRESH_SECONDS" in caplog.text
    assert "60" in caplog.text and "900" in caplog.text and "300" in caplog.text


def test_the_default_answers_cover_the_whole_allowlist():
    # Guards the fake itself: a new query without a canned answer fails here.
    assert set(default_range_answers()) == {q for _, q, _, _ in history.RANGE_QUERIES}
    assert SHA_A and SHA_B and SHA_C


def test_a_failed_query_does_not_keep_its_parsed_answer_alive_during_the_refresh():
    # Found by the memory experiment (PR notes): a stored exception keeps its
    # traceback, and the traceback keeps the frame that held the parsed body. Nine
    # failed queries then held nine parsed 128 KiB bodies at once (28 MiB at the
    # peak instead of about 3).
    import gc
    import weakref

    class Big:
        pass

    refs = []
    leaked = []

    def failing(promql, start, end, step):
        gc.collect()
        leaked.extend(r for r in refs if r() is not None)
        big = Big()  # stands for the parsed body in the frame of the failing parser
        refs.append(weakref.ref(big))
        raise QueryError("too many points")

    service, _, _ = make(failing)
    service.refresh_once()
    assert len(refs) == len(EXPECTED)
    assert leaked == [], "an earlier query's frame is still alive"
