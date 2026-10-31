# The landing page at / (demo issue #103): static HTML served by the backend.
import pytest
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from app.main import create_app


@pytest.fixture
def client():
    return TestClient(create_app(), follow_redirects=False)


def test_root_is_a_cacheable_html_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert r.headers["cache-control"] == "public, max-age=300"


def test_root_does_not_count_toward_the_redirect_slo():
    # The SLO recording rules (gitops repo, slo-rules.yaml) select route="/{code}"
    # only. Page views must land under their own label, not in the redirect SLI.
    registry = CollectorRegistry()
    TestClient(create_app(registry=registry)).get("/")
    labels = {"method": "GET", "route": "/", "status": "200"}
    assert registry.get_sample_value("http_request_duration_seconds_count", labels) == 1
    routes = {s.labels.get("route") for m in registry.collect() for s in m.samples}
    assert "/{code}" not in routes


REPOS = (
    "https://github.com/Sandeep-RN/oci-k8s-gitops-demo",
    "https://github.com/Sandeep-RN/oci-k8s-gitops-demo-app",
    "https://github.com/Sandeep-RN/oci-k8s-gitops-demo-gitops",
)


def test_root_links_to_the_api_docs_and_the_three_repos(client):
    html = client.get("/").text
    assert 'href="/docs"' in html
    for repo in REPOS:
        assert f'href="{repo}"' in html


def test_root_is_small_with_no_scripts_or_third_party_assets(client):
    r = client.get("/")
    assert len(r.content) < 10 * 1024
    html = r.text.lower()
    assert "<script" not in html
    # Outside URLs appear only as plain links (href), never as loaded assets.
    for attr in ("src=", "<link", "@import", "url("):
        assert attr not in html


def test_root_sends_a_csp_that_allows_no_scripts_or_outside_assets(client):
    csp = client.get("/").headers["content-security-policy"]
    directives = dict(d.strip().split(" ", 1) for d in csp.split(";"))
    assert directives["default-src"] == "'none'"
    assert directives["style-src"] == "'unsafe-inline'"
    assert directives["frame-ancestors"] == "'none'"
    assert "script-src" not in directives


# The example link is a row in that cluster's database, so its code is
# configuration, not part of the image.
def test_root_shows_the_configured_example_link():
    html = TestClient(create_app(example_code="Ab3dE9z")).get("/").text
    assert 'href="/Ab3dE9z"' in html


def test_root_has_no_example_link_when_none_is_configured(client):
    assert "example" not in client.get("/").text.lower()


@pytest.mark.parametrize("bad", ["", "abc", "Ab3dE9z/", '"><b>x</b>', "Ab3dE9zz"])
def test_an_invalid_example_code_is_left_out_not_fatal(bad, caplog):
    # A cosmetic setting must never stop the app that serves the redirects.
    r = TestClient(create_app(example_code=bad)).get("/")
    assert r.status_code == 200
    assert "example" not in r.text.lower()
    assert "<b>" not in r.text
    if bad:
        assert "EXAMPLE_LINK_CODE" in caplog.text
