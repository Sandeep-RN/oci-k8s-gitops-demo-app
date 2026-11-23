# The landing page at / (demo issue #103): static HTML served by the backend.
# Its metric label (it must stay out of the redirect SLO) is in test_metrics.py.
import base64
import hashlib
import re
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app import landing
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
    assert 'href="/api/docs"' in html
    assert 'href="/docs"' not in html
    for repo in REPOS:
        assert f'href="{repo}"' in html


def test_root_is_small_with_one_inline_script_and_no_third_party_assets(client):
    # Changed for demo #230 (live status strip): the page used to have no script.
    r = client.get("/")
    assert len(r.content) < 60 * 1024
    html = r.text.lower()
    assert html.count("<script") == 1
    assert html.count("</script>") == 1
    assert "<script>" in html  # no src, no type, no attributes: inline only
    # Outside URLs appear only as plain links (href), never as loaded assets.
    for attr in ("src=", "<link", "@import"):
        assert attr not in html
    # url(...) is allowed only as a same-page fragment (the SVG arrowhead).
    assert not re.search(r"url\((?!#)", html)


def test_root_has_no_inline_event_handlers_or_javascript_urls(client):
    html = client.get("/").text
    assert not re.search(r"<[^>]+\son[a-z]+\s*=", html, re.IGNORECASE)
    assert "javascript:" not in html.lower()


def test_root_names_no_hosts_but_github_and_the_assistant(client):
    html = client.get("/").text
    hosts = {urlsplit(u).netloc for u in re.findall(r"https?://[^\s\"'<>)]+", html)}
    assert hosts <= {"github.com", "ask.sandeeprn.in"}


def test_the_script_links_only_to_the_app_repo_commit_page(client):
    html = client.get("/").text
    script = re.search(r"<script>(.*?)</script>", html, re.DOTALL).group(1)
    urls = re.findall(r"https?://[^\s\"'<>)]+", script)
    assert urls == ["https://github.com/Sandeep-RN/oci-k8s-gitops-demo-app/commit/"]
    assert "innerHTML" not in script and "outerHTML" not in script
    assert "insertAdjacentHTML" not in script and "document.write" not in script
    assert "eval(" not in script and "new Function" not in script


def csp_directives(client):
    csp = client.get("/").headers["content-security-policy"]
    return dict(d.strip().split(" ", 1) for d in csp.split(";"))


def test_root_sends_a_csp_that_allows_only_the_one_script_and_same_origin_fetches(client):
    directives = csp_directives(client)
    assert directives["default-src"] == "'none'"
    assert directives["style-src"] == "'unsafe-inline'"
    assert directives["connect-src"] == "'self'"
    assert directives["frame-ancestors"] == "'none'"
    assert directives["base-uri"] == "'none'"
    assert directives["form-action"] == "'none'"
    assert re.fullmatch(r"'sha256-[A-Za-z0-9+/]{43}='", directives["script-src"])
    assert "unsafe-inline" not in directives["script-src"]
    assert "unsafe-eval" not in directives["script-src"]


def test_the_csp_script_hash_is_the_hash_of_the_served_script(client):
    r = client.get("/")
    script = re.search(r"<script>(.*?)</script>", r.text, re.DOTALL).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode()
    assert csp_directives(client)["script-src"] == f"'sha256-{digest}'"


def test_the_hash_follows_the_page_so_an_edit_cannot_leave_a_stale_hash(monkeypatch, tmp_path):
    page = tmp_path / "index.html"
    page.write_text("<html><script>var a = 1;</script></html>", encoding="utf-8")
    monkeypatch.setattr(landing, "PAGE", page)
    c = TestClient(create_app())
    digest = base64.b64encode(hashlib.sha256(b"var a = 1;").digest()).decode()
    assert f"script-src 'sha256-{digest}'" in c.get("/").headers["content-security-policy"]


@pytest.mark.parametrize(
    "html",
    [
        "<html></html>",  # no script
        "<script>a</script><script>b</script>",  # two scripts
        '<script src="x.js"></script>',  # an external one
        '<script type="module">a</script>',  # attributes: not the exact form we hash
        "<script>a",  # unterminated
    ],
)
def test_a_page_without_exactly_one_plain_inline_script_fails_at_startup(
    html, monkeypatch, tmp_path
):
    page = tmp_path / "index.html"
    page.write_text(html, encoding="utf-8")
    monkeypatch.setattr(landing, "PAGE", page)
    with pytest.raises(ValueError):
        create_app()


def test_root_has_a_live_strip_that_reads_without_javascript(client):
    html = client.get("/").text
    assert "Live platform data" in html
    # The fallback text is in the HTML itself; the script replaces it.
    assert "Live data unavailable" in html.split("<script>")[0]
    assert "/api/status" in html


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


def test_root_links_to_the_assistant_and_has_a_header_nav(client):
    html = client.get("/").text
    assert 'href="https://ask.sandeeprn.in"' in html
    assert "<nav" in html


def test_root_has_an_accessible_diagram_and_dark_mode(client):
    html = client.get("/").text
    assert "<svg" in html and 'role="img"' in html
    assert "<title" in html.split("<svg", 1)[1]
    assert "prefers-color-scheme: dark" in html
    assert "#0a0c12" in html


def test_root_proof_numbers_link_to_their_sources(client):
    html = client.get("/").text
    for source in (
        "demo/tree/main/docs/adr",
        "demo/tree/main/docs/runbooks",
        "postgres-backup-restore.md",
        "/pulls?q=is%3Apr+is%3Amerged",
    ):
        assert source in html


def test_root_is_mobile_friendly(client):
    html = client.get("/").text
    assert 'name="viewport"' in html
    assert "@media (max-width" in html
