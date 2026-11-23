# The Prometheus HTTP client behind /api/status (demo #230). It must treat the
# answer as hostile: Prometheus has no authentication, and a bug or a changed
# response must never crash the refresher or leak into the page.
import io
import json
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from app import status
from app.status import PrometheusQuery, QueryError
from tests.loopback import DripServer, RedirectServer

BASE = "http://prometheus.example.invalid:9090"


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


def vector(*series, status_="success", result_type="vector"):
    return json.dumps(
        {"status": status_, "data": {"resultType": result_type, "result": list(series)}}
    ).encode()


def series(value, **labels):
    return {"metric": labels, "value": [1_795_000_000.0, value]}


def client(opener, **kwargs):
    return PrometheusQuery(BASE, opener=opener, **kwargs)


def test_it_asks_for_the_encoded_query_with_a_2_second_timeout():
    opener = FakeOpener(vector(series("1")))
    client(opener)('sum(rate(x{a="b c"}[5m]))')
    request, timeout = opener.requests[0]
    parts = urlsplit(request.full_url)
    assert parts.scheme + "://" + parts.netloc == BASE
    assert parts.path == "/api/v1/query"
    assert parse_qs(parts.query) == {"query": ['sum(rate(x{a="b c"}[5m]))']}
    assert request.get_method() == "GET"
    assert timeout == 2.0


def test_a_trailing_slash_on_the_base_url_is_fine():
    opener = FakeOpener(vector(series("1")))
    PrometheusQuery(BASE + "/", opener=opener)("up")
    assert urlsplit(opener.requests[0][0].full_url).path == "/api/v1/query"


def test_a_vector_is_parsed_into_samples():
    body = vector(series("0.25", job="app"), series("3", job="other"))
    samples = client(FakeOpener(body))("up")
    assert [s.value for s in samples] == [0.25, 3.0]
    assert samples[0].labels == {"job": "app"}


def test_an_empty_vector_is_an_empty_list_not_an_error():
    assert client(FakeOpener(vector()))("argocd_app_info") == []


@pytest.mark.parametrize("text", ["1e-3", "-4", "0", "12.5", "1E3", "+1"])
def test_ordinary_numbers_are_accepted(text):
    assert client(FakeOpener(vector(series(text))))("q")[0].value == float(text)


@pytest.mark.parametrize(
    "text",
    ["NaN", "nan", "+Inf", "-Inf", "inf", "Infinity", "abc", "", " 1", "1 ", "1_000", "0x10",
     "1e999", "٣"],
)  # fmt: skip
def test_non_numeric_and_non_finite_sample_values_are_dropped(text):
    # NaN is a normal answer (0/0 with no traffic): it means "no value".
    assert client(FakeOpener(vector(series(text), series("2"))))("q") == [status.Sample({}, 2.0)]


@pytest.mark.parametrize("value", [1, None, [1], {"a": 1}, True])
def test_a_sample_value_that_is_not_a_string_is_dropped(value):
    body = vector({"metric": {}, "value": [1.0, value]})
    assert client(FakeOpener(body))("q") == []


@pytest.mark.parametrize(
    "sample",
    [
        {},
        {"metric": {}},
        {"value": [1, "1"]},
        {"metric": [], "value": [1, "1"]},
        {"metric": {}, "value": "1"},
        {"metric": {}, "value": [1]},
        {"metric": {}, "value": [1, "1", 3]},
        "text",
        7,
        None,
    ],
)
def test_malformed_samples_are_dropped_without_failing_the_query(sample):
    body = vector(sample, series("5"))
    assert [s.value for s in client(FakeOpener(body))("q")] == [5.0]


def test_labels_that_are_not_text_are_dropped_and_long_ones_truncated():
    body = vector(series("1", ok="x", number=5, nested={"a": 1}, big="y" * 10_000))
    labels = client(FakeOpener(body))("q")[0].labels
    assert labels["ok"] == "x"
    assert "number" not in labels and "nested" not in labels
    assert len(labels["big"]) <= 512


def test_extra_fields_in_the_answer_are_ignored():
    body = json.dumps(
        {
            "status": "success",
            "warnings": ["w"],
            "data": {"resultType": "vector", "result": [], "stats": {"x": 1}},
            "extra": 1,
        }
    ).encode()
    assert client(FakeOpener(body))("q") == []


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"\xff\xfe",  # not UTF-8
        b"[]",
        b"null",
        b'"x"',
        b"{}",
        b'{"status": "error", "errorType": "bad_data", "error": "boom"}',
        b'{"status": "success"}',
        b'{"status": "success", "data": []}',
        b'{"status": "success", "data": {"resultType": "vector"}}',
        b'{"status": "success", "data": {"resultType": "vector", "result": {}}}',
        b'{"status": "success", "data": {"resultType": "matrix", "result": []}}',
        b'{"status": "success", "data": {"resultType": "scalar", "result": [1, "1"]}}',
        b'{"status": "success", "data": {"resultType": "vector", "result": [NaN]}}',
        b'{"status": "success", "data": {"resultType": "vector", "result": [Infinity]}}',
        b'{"status": "success", "status": 1',
    ],
)
def test_a_wrong_shape_or_invalid_json_is_a_query_error(body):
    with pytest.raises(QueryError):
        client(FakeOpener(body))("q")


def test_the_response_cap_is_64_kib_and_a_realistic_answer_is_far_under_it():
    # Review of PR #42 (M2): 1 MiB x 6 workers cost ~125 MiB of Python memory per
    # refresh. The largest real answer is a few KB, so the cap can be small.
    assert status.MAX_RESPONSE_BYTES == 64 * 1024
    assert status.QUERY_WORKERS == 3
    apps = [
        series(
            "1",
            name=f"app-{i}",
            namespace="argocd",
            project="default",
            repo="https://example.invalid/org/repo.git",
            dest_server="https://kubernetes.default.svc",
            dest_namespace="link-shortener",
            sync_status="Synced",
            health_status="Healthy",
            autosync_enabled="true",
            instance="10.0.0.9:8082",
            job="argocd-metrics",
        )
        for i in range(20)
    ]
    body = vector(*apps)
    assert len(body) < status.MAX_RESPONSE_BYTES // 3
    assert len(client(FakeOpener(body))("q")) == 20


def test_a_body_over_the_cap_is_refused():
    huge = vector(series("1", pad="x" * 400)) + b" " * status.MAX_RESPONSE_BYTES
    with pytest.raises(QueryError, match="large"):
        client(FakeOpener(huge))("q")


def test_a_body_just_under_the_cap_is_read():
    body = vector(series("1"))
    body += b" " * (status.MAX_RESPONSE_BYTES - len(body))
    assert len(body) == status.MAX_RESPONSE_BYTES
    assert client(FakeOpener(body))("q")[0].value == 1.0


def test_thousands_of_series_are_refused():
    many = [series("1") for _ in range(status.MAX_SERIES + 1)]  # small enough for the byte cap
    with pytest.raises(QueryError, match="series"):
        client(FakeOpener(vector(*many)))("q")


def test_a_non_200_status_is_a_query_error():
    with pytest.raises(QueryError):
        client(FakeOpener(vector(), status_code=204))("q")


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("refused"),
        urllib.error.HTTPError(BASE, 500, "boom", {}, None),
        TimeoutError("slow"),
        ConnectionResetError("reset"),
        OSError("network is unreachable"),
        ValueError("odd"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_network_failures_become_a_query_error(error):
    with pytest.raises(QueryError):
        client(FakeOpener(error=error))("q")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "prometheus:9090",
        "ftp://prometheus:9090",
        "file:///etc/passwd",
        "http://",
        "http://user:pass@prometheus:9090",
        "http://prometheus:9090/?a=1",
        "http://prometheus:9090/#x",
        "//prometheus:9090",
        "http://prometheus:notaport",
        "http://pro metheus:9090",
        "http://prometheus:0",  # port 0 is never a listening port
        "http://prometheus\x00:9090",
        "http://prom\retheus:9090",
        "http://prometheus:9090/\x1b[31m",
        "http://prometheus:9090/pa\x7fth",
    ],
)
def test_an_unusable_base_url_is_refused_when_building_the_client(url):
    with pytest.raises(ValueError):
        PrometheusQuery(url)


def test_a_path_prefix_in_the_base_url_is_kept():
    # Prometheus can be served under a prefix (--web.route-prefix).
    opener = FakeOpener(vector())
    PrometheusQuery("http://p.invalid:9090/prom/", opener=opener)("up")
    assert urlsplit(opener.requests[0][0].full_url).path == "/prom/api/v1/query"


# ---- the real urllib path, against a loopback server ------------------------------


class _Handler(BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_GET(self):  # noqa: N802
        type(self).seen.append(self.path)
        body = vector(series("0.5", instance="10.0.0.9:9100"))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def loopback():
    _Handler.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(5)


def test_the_default_opener_talks_real_http(loopback):
    samples = PrometheusQuery(loopback)('up{job="a b"}')
    assert samples[0].value == 0.5
    assert parse_qs(urlsplit(_Handler.seen[0]).query) == {"query": ['up{job="a b"}']}


def test_the_default_opener_does_not_follow_redirects():
    # The redirect points at ANOTHER PATH ON THE SAME SERVER, so "was it followed"
    # is visible: the path would be requested. (A redirect to a closed port fails
    # the same way whether or not it was followed: the review of PR #42, M3.)
    server = RedirectServer()
    try:
        with pytest.raises(QueryError):
            PrometheusQuery(server.url)("up")
        assert len(server.seen) == 1
        assert not any(path.startswith("/elsewhere") for path in server.seen)
    finally:
        server.close()


def test_the_default_opener_ignores_proxy_environment_variables(loopback, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    assert PrometheusQuery(loopback)("up")[0].value == 0.5


# ---- a hostile server that dribbles bytes (review of PR #42, M1 and M3) -----------
# Each byte arrives well inside the 2 s socket timeout, so only a bound on the
# WHOLE exchange can stop it. Real sockets: the fake opener these tests used to
# have replaced the very code that was wrong.


@pytest.fixture
def drip():
    servers = []

    def make(mode, interval=0.05):
        servers.append(DripServer(mode, interval))
        return servers[-1]

    yield make
    for server in servers:
        server.close()


def test_a_body_dribbled_one_byte_at_a_time_is_cut_off_at_the_query_timeout(drip, monkeypatch):
    # The watchdog is parked far away so ONLY the read loop's own deadline can
    # save this: read(8192) would block until 8192 bytes arrive (about 7 minutes
    # at this speed); read1() returns after one receive and lets the deadline run.
    monkeypatch.setattr(status, "WATCHDOG_GRACE", 30.0)
    server = drip("body")
    started = time.monotonic()
    with pytest.raises(QueryError, match="too slow"):
        PrometheusQuery(server.url, timeout=0.4)("up")
    assert time.monotonic() - started < 1.5


def test_headers_dribbled_one_byte_at_a_time_are_cut_off_by_the_watchdog(drip):
    # No body is ever reached: the status line and a header never finish. Neither
    # the socket timeout nor the body deadline can fire; the watchdog closes the
    # socket from another thread.
    server = drip("headers", interval=0.3)  # under the 0.4 s socket timeout
    started = time.monotonic()
    with pytest.raises(QueryError, match="too slow"):
        PrometheusQuery(server.url, timeout=0.4)("up")
    elapsed = time.monotonic() - started
    assert elapsed < 0.4 + status.WATCHDOG_GRACE + 0.8


@pytest.mark.parametrize("mode", ["body", "headers"])
def test_the_socket_is_really_closed_after_a_cut_off(drip, mode):
    server = drip(mode)
    with pytest.raises(QueryError):
        PrometheusQuery(server.url, timeout=0.3)("up")
    assert server.peer_gone.wait(3), "the client left its socket open"


def test_the_watchdog_timer_does_not_outlive_a_normal_query(loopback):
    def timers():
        return [t for t in threading.enumerate() if isinstance(t, threading.Timer)]

    before = len(timers())
    for _ in range(5):
        assert PrometheusQuery(loopback)("up")[0].value == 0.5
    # Each call arms a timer and must cancel it: none may sit out its 2.5 s.
    deadline = time.monotonic() + 1.0
    while len(timers()) > before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(timers()) <= before


# ---- what an error may say (review of PR #42, info) --------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b'{"status":"error","errorType":"bad_data","error":"CANARY-LABEL secret=hunter2"}',
        b'{"status":"success","data":{"resultType":"matrix","result":"CANARY-LABEL"}}',
        b'{"status":"success","data":{"resultType":"vector","result":{"CANARY-LABEL":1}}}',
        b"CANARY-LABEL not json",
        b'{"status":"success","data":{"resultType":"vector","result":[NaN,"CANARY-LABEL"]}}',
    ],
)
def test_a_query_error_message_never_carries_body_or_label_text(body):
    with pytest.raises(QueryError) as caught:
        client(FakeOpener(body))("q")
    assert "CANARY" not in str(caught.value)
    assert "hunter2" not in str(caught.value)


def test_a_failed_request_message_names_the_exception_type_only():
    error = urllib.error.URLError("refused by http://10.1.2.3:9090 CANARY-LABEL")
    with pytest.raises(QueryError) as caught:
        client(FakeOpener(error=error))("q")
    assert str(caught.value) == "prometheus request failed: URLError"
