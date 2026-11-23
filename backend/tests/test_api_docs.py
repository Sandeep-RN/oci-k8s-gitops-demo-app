# The Swagger UI lives at /api/docs (demo issue #229), with a dark theme.
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import links
from app.main import create_app


@pytest.fixture
def client():
    return TestClient(create_app(), follow_redirects=False)


def test_swagger_ui_is_served_at_api_docs(client):
    r = client.get("/api/docs")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "swagger-ui" in r.text
    # Points at the moved schema, not the FastAPI default.
    assert "/api/openapi.json" in r.text


def test_api_docs_has_a_dark_mode_stylesheet_matching_the_landing_page(client):
    html = client.get("/api/docs").text
    assert "prefers-color-scheme: dark" in html
    assert "#0a0c12" in html
    assert "color-scheme" in html


def test_openapi_schema_moved_and_still_lists_the_link_routes(client):
    r = client.get("/api/openapi.json")
    assert r.status_code == 200
    assert "/api/links" in r.json()["paths"]


@pytest.mark.parametrize("old", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_old_doc_paths_are_plain_404(client, old):
    # Not redirected: "docs" is not a valid 7-character code, so the redirect
    # route answers 404. /redoc is dropped on purpose (one docs UI is enough).
    assert client.get(old).status_code == 404


def test_api_docs_is_not_in_its_own_schema(client):
    assert "/api/docs" not in client.get("/api/openapi.json").json()["paths"]


def test_no_fixed_top_level_route_can_collide_with_a_generated_code():
    # A fixed route is matched before /{code}. If its path were a valid code,
    # generate_code() could hand out a short link that route would shadow.
    app = create_app()
    for route in app.routes:
        path = getattr(route, "path", "")
        segments = path.strip("/").split("/")
        if len(segments) != 1 or "{" in path or not segments[0]:
            continue
        name = segments[0]
        if links.is_valid_code(name):
            assert name in links.RESERVED_CODES, f"/{name} can collide with a short code"


def test_api_prefix_can_never_be_a_code():
    # Codes are exactly 7 base62 characters and one path segment.
    assert not links.is_valid_code("api")
    assert not links.is_valid_code("api/docs")
    assert any(isinstance(r, APIRoute) and r.path == "/{code}" for r in create_app().routes)
