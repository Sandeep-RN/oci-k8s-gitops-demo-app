# query_range, the second half of the Prometheus client (demo #235, ADR 0027). It
# gets the same hardening as the instant query (demo-app #43) with a bigger
# response cap, and the answer is hostile until proven otherwise. Where the
# finding is about SOCKET behaviour, the test uses a real loopback server.
import json
import threading
import time
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from app import status
from app.status import PrometheusQuery, QueryError, RangeSeries
from tests.history_helpers import FakeOpener, matrix, mseries
from tests.loopback import BodyServer, DripServer, RedirectServer

BASE = "http://prometheus.example.invalid:9090"
STEP = 300
END = 1_794_999_900  # a multiple of 300
START = END - 86400
# What 24 h at 5 min allows: 289 points, plus the margin.
CAP = int((END - START) // STEP) + 1 + status.RANGE_POINT_MARGIN


def client(opener, **kwargs):
    return PrometheusQuery(BASE, opener=opener, **kwargs)


def rng(opener, promql="up", **kwargs):
    return client(opener).query_range(promql, START, END, STEP, **kwargs)


def ok_body(n=5, **labels):
    return matrix(mseries(START, STEP, ["0.5"] * n, **labels))


def many(n, ts_step=STEP):
    return {"metric": {}, "values": [[START + i * ts_step, "1"] for i in range(n)]}


# ---- the request ---------------------------------------------------------------------


def test_it_asks_for_a_range_with_server_chosen_numbers_and_a_2_second_timeout():
    opener = FakeOpener(ok_body())
    rng(opener, 'sum(rate(x{a="b c"}[5m]))')
    request, timeout = opener.requests[0]
    parts = urlsplit(request.full_url)
    assert parts.scheme + "://" + parts.netloc == BASE
    assert parts.path == "/api/v1/query_range"
    assert parse_qs(parts.query) == {
        "query": ['sum(rate(x{a="b c"}[5m]))'],
        "start": [str(START)],
        "end": [str(END)],
        "step": [str(STEP)],
    }
    assert request.get_method() == "GET"
    assert timeout == 2.0


def test_a_path_prefix_in_the_base_url_is_kept_for_ranges_too():
    opener = FakeOpener(ok_body())
    PrometheusQuery("http://p.invalid:9090/prom/", opener=opener).query_range(
        "up", START, END, STEP
    )
    assert urlsplit(opener.requests[0][0].full_url).path == "/prom/api/v1/query_range"


@pytest.mark.parametrize(
    "start, end, step",
    [
        (END, START, STEP),  # backwards
        (START, START, STEP),  # empty
        (START, END, 0),
        (START, END, -5),
        (float("nan"), END, STEP),
        (START, float("inf"), STEP),
        (START, END, float("nan")),
        (0, 10**9, 1),  # more than the 11,000 points Prometheus allows
    ],
)
def test_nonsense_arguments_are_refused_before_anything_is_sent(start, end, step):
    opener = FakeOpener(ok_body())
    with pytest.raises(ValueError):
        client(opener).query_range("up", start, end, step)
    assert opener.requests == []


# ---- parsing --------------------------------------------------------------------------


def test_a_matrix_is_parsed_into_series_of_numbers():
    body = matrix(mseries(START, STEP, ["0.25", "3", "-1e-3"], job="app"))
    (one,) = rng(FakeOpener(body))
    assert isinstance(one, RangeSeries)
    assert one.points == ((START, 0.25), (START + STEP, 3.0), (START + 2 * STEP, -0.001))
    assert one.labels == {"job": "app"}


def test_an_empty_matrix_is_an_empty_list_not_an_error():
    assert rng(FakeOpener(matrix())) == []


def test_a_series_with_no_values_is_kept_empty():
    (one,) = rng(FakeOpener(matrix({"metric": {}, "values": []})))
    assert one.points == ()


@pytest.mark.parametrize(
    "text",
    ["NaN", "nan", "+Inf", "-Inf", "inf", "Infinity", "abc", "", " 1", "1 ", "1_000", "0x10",
     "1e999", "٣", "1" * 65],
)  # fmt: skip
def test_non_numeric_and_non_finite_values_become_gaps(text):
    # NaN is a normal answer (0/0): it means "no value", and a gap is never a zero.
    body = matrix(mseries(START, STEP, ["1", text, "3"]))
    (one,) = rng(FakeOpener(body))
    assert one.points == ((START, 1.0), (START + 2 * STEP, 3.0))


@pytest.mark.parametrize("ts", ["1795", None, True, [1], float("nan"), -5])
def test_a_point_with_a_bad_timestamp_is_dropped(ts):
    series = {"metric": {}, "values": [[ts, "1"], [START, "2"]]}
    if isinstance(ts, float):  # json.dumps would write NaN, which is not JSON
        body = matrix(series).replace(b"NaN", b'"NaN"')
    else:
        body = matrix(series)
    (one,) = rng(FakeOpener(body))
    assert one.points == ((START, 2.0),)


def test_points_outside_the_asked_window_are_dropped():
    # A timestamp is data from a hostile server: it must not move the x axis.
    series = {
        "metric": {},
        "values": [[START - 10_000, "1"], [START, "2"], [END, "3"], [END + 10_000, "4"]],
    }
    (one,) = rng(FakeOpener(matrix(series)))
    assert [v for _, v in one.points] == [2.0, 3.0]


@pytest.mark.parametrize("pair", [[], [1], [1, "1", 3], "x", 7, None, {"a": 1}])
def test_a_malformed_point_is_dropped_without_failing_the_query(pair):
    series = {"metric": {}, "values": [pair, [START, "2"]]}
    (one,) = rng(FakeOpener(matrix(series)))
    assert one.points == ((START, 2.0),)


@pytest.mark.parametrize(
    "series",
    [{}, {"metric": {}}, {"values": [[START, "1"]]}, {"metric": [], "values": []},
     {"metric": {}, "values": "x"}, "text", 7, None],
)  # fmt: skip
def test_a_malformed_series_is_dropped_and_the_others_survive(series):
    body = matrix(series, mseries(START, STEP, ["5"]))
    assert [s.points for s in rng(FakeOpener(body))] == [((START, 5.0),)]


def test_labels_that_are_not_text_are_dropped_and_long_ones_truncated():
    body = matrix(mseries(START, STEP, ["1"], ok="x", number=5, nested={"a": 1}, big="y" * 9999))
    labels = rng(FakeOpener(body))[0].labels
    assert labels["ok"] == "x" and "number" not in labels and "nested" not in labels
    assert len(labels["big"]) <= 512


def test_points_come_back_sorted_and_without_duplicates():
    series = {"metric": {}, "values": [[START + STEP, "2"], [START, "1"], [START, "9"]]}
    (one,) = rng(FakeOpener(matrix(series)))
    assert [t for t, _ in one.points] == [START, START + STEP]


@pytest.mark.parametrize("result_type", ["vector", "scalar", "string", "", None, 5])
def test_only_a_matrix_is_accepted(result_type):
    with pytest.raises(QueryError, match="matrix"):
        rng(FakeOpener(matrix(result_type=result_type)))


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"\xff\xfe",
        b"[]",
        b"null",
        b"{}",
        b'{"status": "error", "errorType": "bad_data", "error": "boom"}',
        b'{"status": "success"}',
        b'{"status": "success", "data": []}',
        b'{"status": "success", "data": {"resultType": "matrix"}}',
        b'{"status": "success", "data": {"resultType": "matrix", "result": {}}}',
        b'{"status": "success", "data": {"resultType": "matrix", "result": [NaN]}}',
        b'{"status": "success", "data": {"resultType": "matrix", "result": [Infinity]}}',
        b'{"status": "success", "status": 1',
    ],
)
def test_a_wrong_shape_or_invalid_json_is_a_query_error(body):
    with pytest.raises(QueryError):
        rng(FakeOpener(body))


@pytest.mark.parametrize(
    "body",
    [
        b'{"status":"error","errorType":"bad_data","error":"CANARY-LABEL secret=hunter2"}',
        b'{"status":"success","data":{"resultType":"vector","result":"CANARY-LABEL"}}',
        b"CANARY-LABEL not json",
        b'{"status":"success","data":{"resultType":"matrix","result":[NaN,"CANARY-LABEL"]}}',
    ],
)
def test_an_error_message_never_carries_body_or_label_text(body):
    with pytest.raises(QueryError) as caught:
        rng(FakeOpener(body))
    assert "CANARY" not in str(caught.value) and "hunter2" not in str(caught.value)


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("refused"),
        urllib.error.HTTPError(BASE, 500, "boom", {}, None),
        TimeoutError("slow"),
        ConnectionResetError("reset"),
        ValueError("odd"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_network_failures_become_a_query_error(error):
    with pytest.raises(QueryError):
        rng(FakeOpener(error=error))


def test_a_non_200_status_is_a_query_error():
    with pytest.raises(QueryError):
        rng(FakeOpener(ok_body(), status_code=204))


# ---- the point caps (ADR 0027) --------------------------------------------------------


def test_the_cap_per_series_is_the_window_over_the_step_plus_a_small_margin():
    assert 0 < status.RANGE_POINT_MARGIN <= 5
    assert CAP == 289 + status.RANGE_POINT_MARGIN


def test_a_series_with_exactly_the_cap_is_accepted():
    # Timestamps repeat past the window, so only the COUNT is under test; the
    # accepted points are those inside the window.
    body = matrix({"metric": {}, "values": [[START, "1"]] * CAP})
    assert len(rng(FakeOpener(body))[0].points) == 1


def test_a_series_with_one_point_more_than_the_cap_is_refused():
    body = matrix({"metric": {}, "values": [[START, "1"]] * (CAP + 1)})
    with pytest.raises(QueryError, match="points"):
        rng(FakeOpener(body))


def test_a_series_that_is_too_dense_for_its_step_is_refused():
    # A server that ignores the step and answers every second.
    with pytest.raises(QueryError, match="points"):
        rng(FakeOpener(matrix(many(CAP + 50, ts_step=1))))


def test_the_total_over_all_series_is_capped_too():
    factor = status.RANGE_TOTAL_FACTOR
    ok = matrix(*[many(CAP) for _ in range(factor)])
    assert len(rng(FakeOpener(ok))) == factor
    with pytest.raises(QueryError, match="points"):
        rng(FakeOpener(matrix(*[many(CAP) for _ in range(factor + 1)])))


def test_the_number_of_series_is_capped_at_64():
    assert status.MAX_RANGE_SERIES == 64
    assert len(rng(FakeOpener(matrix(*[many(1) for _ in range(64)])))) == 64
    with pytest.raises(QueryError, match="series"):
        rng(FakeOpener(matrix(*[many(1) for _ in range(65)])))


# ---- the response cap -----------------------------------------------------------------


def test_the_range_cap_is_128_kib_and_the_instant_cap_is_still_64_kib():
    assert status.MAX_RANGE_RESPONSE_BYTES == 128 * 1024
    assert status.MAX_RESPONSE_BYTES == 64 * 1024


def test_a_realistic_range_answer_is_far_under_the_cap():
    # 24 h at 5 min is about 10 KB; the deploy answer (16 series, 744 points) is 25 KB.
    body = matrix(*[many(46) for _ in range(16)])
    assert len(body) < status.MAX_RANGE_RESPONSE_BYTES // 3
    assert len(rng(FakeOpener(body))) == 16


def test_a_range_body_just_under_the_cap_is_read_and_over_it_refused():
    body = ok_body()
    body += b" " * (status.MAX_RANGE_RESPONSE_BYTES - len(body))
    assert len(body) == status.MAX_RANGE_RESPONSE_BYTES
    assert rng(FakeOpener(body))[0].points
    with pytest.raises(QueryError, match="large"):
        rng(FakeOpener(body + b" "))


def test_an_instant_query_still_refuses_what_a_range_query_may_read():
    # 100 KiB: fine for a range answer, over the 64 KiB cap of an instant query.
    padded = b'{"status":"success","data":{"resultType":"vector","result":[]}}' + b" " * 100_000
    with pytest.raises(QueryError, match="large"):
        client(FakeOpener(padded))("up")


# ---- real sockets ---------------------------------------------------------------------


@pytest.fixture
def served():
    servers = []

    def make(body, status_=200):
        servers.append(BodyServer(body, status_))
        return servers[-1]

    yield make
    for s in servers:
        s.close()


def test_the_default_opener_talks_real_http_for_ranges(served):
    server = served(ok_body(3, instance="10.0.0.9:9100"))
    series = PrometheusQuery(server.url).query_range("up", START, END, STEP)
    assert [v for _, v in series[0].points] == [0.5, 0.5, 0.5]
    (target,) = server.seen
    assert urlsplit(target).path == "/api/v1/query_range"
    assert parse_qs(urlsplit(target).query)["step"] == [str(STEP)]


def test_an_oversized_body_over_a_real_socket_is_refused(served):
    # Valid JSON, padded past the cap: the read stops at the cap.
    body = ok_body() + b" " * (status.MAX_RANGE_RESPONSE_BYTES + 1000)
    server = served(body)
    with pytest.raises(QueryError, match="large"):
        PrometheusQuery(server.url).query_range("up", START, END, STEP)


def test_too_many_points_over_a_real_socket_are_refused(served):
    server = served(matrix(many(CAP + 1)))
    with pytest.raises(QueryError, match="points"):
        PrometheusQuery(server.url).query_range("up", START, END, STEP)


def test_a_vector_answer_to_a_range_request_over_a_real_socket_is_refused(served):
    body = b'{"status":"success","data":{"resultType":"vector","result":[]}}'
    with pytest.raises(QueryError, match="matrix"):
        PrometheusQuery(served(body).url).query_range("up", START, END, STEP)


def test_nan_and_inf_over_a_real_socket_become_gaps(served):
    body = matrix(mseries(START, STEP, ["NaN", "+Inf", "-Inf", "2"]))
    (one,) = PrometheusQuery(served(body).url).query_range("up", START, END, STEP)
    assert one.points == ((START + 3 * STEP, 2.0),)


def test_hostile_labels_over_a_real_socket_are_bounded(served):
    labels = {f"k{i}": "<script>alert(1)</script>" * 40 for i in range(100)}
    body = matrix(mseries(START, STEP, ["1"], **labels))
    (one,) = PrometheusQuery(served(body).url).query_range("up", START, END, STEP)
    assert len(one.labels) <= status.MAX_LABELS
    assert all(len(v) <= status.MAX_LABEL_VALUE for v in one.labels.values())


def test_a_redirect_on_a_same_server_path_is_never_followed_for_ranges():
    server = RedirectServer()
    try:
        with pytest.raises(QueryError):
            PrometheusQuery(server.url).query_range("up", START, END, STEP)
        assert len(server.seen) == 1
        assert not any(p.startswith("/elsewhere") for p in server.seen)
    finally:
        server.close()


def test_proxy_environment_variables_are_ignored_for_ranges(served, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    assert PrometheusQuery(served(ok_body()).url).query_range("up", START, END, STEP)


@pytest.fixture
def drip():
    servers = []

    def make(mode, interval=0.05):
        servers.append(DripServer(mode, interval))
        return servers[-1]

    yield make
    for server in servers:
        server.close()


def test_a_range_body_dribbled_one_byte_at_a_time_is_cut_off_by_the_deadline(drip, monkeypatch):
    # Watchdog parked far away: only the read loop's own deadline (and read1) can stop it.
    monkeypatch.setattr(status, "WATCHDOG_GRACE", 30.0)
    server = drip("body")
    started = time.monotonic()
    with pytest.raises(QueryError, match="too slow"):
        PrometheusQuery(server.url, timeout=0.4).query_range("up", START, END, STEP)
    assert time.monotonic() - started < 1.5


def test_range_headers_dribbled_one_byte_at_a_time_are_cut_off_by_the_watchdog(drip):
    server = drip("headers", interval=0.3)
    started = time.monotonic()
    with pytest.raises(QueryError, match="too slow"):
        PrometheusQuery(server.url, timeout=0.4).query_range("up", START, END, STEP)
    assert time.monotonic() - started < 0.4 + status.WATCHDOG_GRACE + 0.8


@pytest.mark.parametrize("mode", ["body", "headers"])
def test_the_socket_is_really_closed_after_a_range_cut_off(drip, mode):
    server = drip(mode)
    with pytest.raises(QueryError):
        PrometheusQuery(server.url, timeout=0.3).query_range("up", START, END, STEP)
    assert server.peer_gone.wait(3), "the client left its socket open"


def test_the_watchdog_timer_does_not_outlive_a_normal_range_query(served):
    def timers():
        return [t for t in threading.enumerate() if isinstance(t, threading.Timer)]

    before = len(timers())
    server = served(ok_body())
    for _ in range(5):
        PrometheusQuery(server.url).query_range("up", START, END, STEP)
    deadline = time.monotonic() + 1.0
    while len(timers()) > before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(timers()) <= before


def test_json_numbers_are_not_accepted_as_values():
    # Prometheus writes sample values as strings; a bare number is not its format.
    body = json.dumps(
        {"status": "success", "data": {"resultType": "matrix",
         "result": [{"metric": {}, "values": [[START, 1]]}]}}
    ).encode()  # fmt: skip
    assert rng(FakeOpener(body))[0].points == ()
