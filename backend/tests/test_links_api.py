import re

import pytest
from fastapi.testclient import TestClient

from app import links
from app.links import CodeCollision, InMemoryLinkStore, StoreUnavailable
from app.main import create_app

CODE_RE = re.compile(r"^[0-9A-Za-z]{7}$")


@pytest.fixture
def client():
    return TestClient(create_app(store=InMemoryLinkStore()), follow_redirects=False)


def create(client, url):
    return client.post("/api/links", json={"url": url})


def test_create_returns_201_with_code_and_url(client):
    r = create(client, "https://example.com/some/page?q=1")
    assert r.status_code == 201
    body = r.json()
    assert CODE_RE.match(body["code"])
    assert body["url"] == "https://example.com/some/page?q=1"
    assert r.headers["location"] == f"/api/links/{body['code']}"


def test_redirect_is_302_and_not_cacheable(client):
    code = create(client, "https://example.com/a").json()["code"]
    r = client.get(f"/{code}")
    # 302, not 301: a cached 301 would hide repeat visits from the redirect SLO.
    assert r.status_code == 302
    assert r.headers["location"] == "https://example.com/a"
    assert r.headers["cache-control"] == "no-store"


def test_unknown_code_is_404(client):
    assert client.get("/zzzzzzz").status_code == 404


def test_malformed_code_is_404_not_422(client):
    # Anything that is not a valid code is simply "not found".
    assert client.get("/abc").status_code == 404
    assert client.get("/abc-def").status_code == 404


def test_metadata_endpoint(client):
    code = create(client, "http://example.org").json()["code"]
    r = client.get(f"/api/links/{code}")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == code
    assert body["url"] == "http://example.org/"
    assert "created_at" in body
    assert client.get("/api/links/zzzzzzz").status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "javascript:alert(1)",
        "not a url",
        "",
        "https://example.com/" + "a" * 2048,  # over 2048 characters in total
    ],
)
def test_rejects_invalid_urls(client, url):
    assert create(client, url).status_code == 422


def test_accepts_url_of_exactly_2048_chars(client):
    base = "https://example.com/"
    url = base + "a" * (2048 - len(base))
    assert create(client, url).status_code == 201


def test_health_routes_are_not_shadowed_by_codes(client):
    # "healthz" is 7 alphanumeric characters, i.e. a syntactically valid code.
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_generated_codes_never_collide_with_reserved_paths(monkeypatch):
    seq = iter(["healthz", "readyz0", "Abc1234"])
    monkeypatch.setattr(links, "_random_code", lambda: next(seq))
    assert links.generate_code() == "readyz0"  # 7 chars, not reserved
    seq2 = iter(["healthz", "Abc1234"])
    monkeypatch.setattr(links, "_random_code", lambda: next(seq2))
    assert links.generate_code() == "Abc1234"


def test_collision_is_retried(monkeypatch):
    store = InMemoryLinkStore()
    seq = iter(["AAAAAAA", "AAAAAAA", "BBBBBBB"])
    monkeypatch.setattr(links, "generate_code", lambda: next(seq))
    c = TestClient(create_app(store=store))
    assert create(c, "https://a.example").json()["code"] == "AAAAAAA"
    # Second create hits the taken code once, then succeeds with the next one.
    assert create(c, "https://b.example").json()["code"] == "BBBBBBB"


def test_gives_up_after_repeated_collisions(monkeypatch):
    store = InMemoryLinkStore()
    store.create("AAAAAAA", "https://a.example")
    monkeypatch.setattr(links, "generate_code", lambda: "AAAAAAA")
    c = TestClient(create_app(store=store), raise_server_exceptions=False)
    assert create(c, "https://b.example").status_code == 500


class DownStore(InMemoryLinkStore):
    def create(self, code, url):
        raise StoreUnavailable("db down")

    def get(self, code):
        raise StoreUnavailable("db down")


def test_store_outage_returns_503_not_500():
    c = TestClient(create_app(store=DownStore()), follow_redirects=False)
    assert create(c, "https://a.example").status_code == 503
    assert c.get("/AAAAAAA").status_code == 503
    assert c.get("/api/links/AAAAAAA").status_code == 503
    # Liveness must stay up during a database outage.
    assert c.get("/healthz").status_code == 200


def test_in_memory_store_rejects_duplicate_code():
    s = InMemoryLinkStore()
    s.create("AAAAAAA", "https://a.example")
    with pytest.raises(CodeCollision):
        s.create("AAAAAAA", "https://b.example")
