# Edge cases around the redirect and create routes (audit finding C15).
import pytest
from fastapi.testclient import TestClient

from app.links import InMemoryLinkStore
from app.main import create_app
from tests.helpers import AUTH, TOKENS


@pytest.fixture
def client():
    return TestClient(create_app(store=InMemoryLinkStore(), tokens=TOKENS), follow_redirects=False)


def create(client, url):
    return client.post("/api/links", json={"url": url}, headers=AUTH)


def test_head_redirects_like_get(client):
    # Link checkers and `curl -I` use HEAD. It must see the same answer as GET.
    code = create(client, "https://example.com/a").json()["code"]
    r = client.head(f"/{code}")
    assert r.status_code == 302
    assert r.headers["location"] == "https://example.com/a"
    assert r.headers["cache-control"] == "no-store"
    assert r.content == b""


def test_head_unknown_and_malformed_codes_are_404(client):
    assert client.head("/zzzzzzz").status_code == 404
    assert client.head("/abc").status_code == 404


def test_head_does_not_shadow_the_health_routes(client):
    # The health routes stay GET-only (probes use GET). HEAD on them falls
    # through to the redirect route, finds no such code and answers 404: it must
    # never turn into a redirect, and GET must be untouched.
    assert client.head("/healthz").status_code == 404
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@example.com/",
        "https://user@example.com/",
        "http://:secret@example.com/path",
    ],
)
def test_urls_with_embedded_credentials_are_rejected(client, url):
    # A shortener must not store or relay credentials in a Location header.
    assert create(client, url).status_code == 422


def test_an_at_sign_outside_the_host_part_is_still_fine(client):
    assert create(client, "https://example.com/@user?mail=a@b.c").status_code == 201


@pytest.mark.parametrize("path", ["/abc", "/abc-def", "/Abc12345", "/%20%20%20%20%20%20%20"])
def test_a_bad_code_is_404_on_both_routes(client, path):
    # The metadata route used to answer 422 and describe the code format.
    assert client.get(path).status_code == 404
    assert client.get(f"/api/links{path}").status_code == 404
