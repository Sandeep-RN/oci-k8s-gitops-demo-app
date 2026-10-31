# The landing page at / (demo issue #103): static HTML served by the backend.
# Its metric label (it must stay out of the redirect SLO) is in test_metrics.py.
import re

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

REPOS = (
    "https://github.com/Sandeep-RN/oci-k8s-gitops-demo",
    "https://github.com/Sandeep-RN/oci-k8s-gitops-demo-app",
    "https://github.com/Sandeep-RN/oci-k8s-gitops-demo-gitops",
)
# A link to a short code: "/" plus 7 base62 characters.
CODE_LINK = re.compile(r'href="/[0-9A-Za-z]{7}"')


@pytest.fixture
def client():
    return TestClient(create_app(), follow_redirects=False)


def test_root_is_a_cacheable_html_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert r.headers["cache-control"] == "public, max-age=300"


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
    assert CODE_LINK.findall(html) == ['href="/Ab3dE9z"']


def test_root_has_no_example_link_when_none_is_configured(client):
    assert CODE_LINK.search(client.get("/").text) is None


@pytest.mark.parametrize("bad", ["", "abc", "Ab3dE9z/", '"><b>x</b>', "Ab3dE9zz", "Ab3dE9z\n"])
def test_an_invalid_example_code_is_left_out_not_fatal(bad, caplog):
    # A cosmetic setting must never stop the app that serves the redirects.
    r = TestClient(create_app(example_code=bad)).get("/")
    assert r.status_code == 200
    assert CODE_LINK.search(r.text) is None
    assert "<b>" not in r.text
    if bad:
        assert "EXAMPLE_LINK_CODE" in caplog.text
