# Link creation is token-gated (ADR 0020): on a public domain, an open link
# shortener is an open redirector. Tokens are files in a mounted directory, one
# per token: the file name is the token's name, the content is its value. That
# is the layout a Kubernetes Secret volume produces.
import pytest
from fastapi.testclient import TestClient

from app.auth import DirectoryTokens
from app.links import InMemoryLinkStore
from app.main import StartupError, create_app, create_production_app

# Fake values, long enough to pass the minimum length check.
OWNER_TOKEN = "owner-test-token-0123456789abcdef0123456789"
K6_TOKEN = "k6-test-token-fedcba9876543210fedcba98765432"


@pytest.fixture
def token_dir(tmp_path):
    (tmp_path / "owner").write_text(OWNER_TOKEN + "\n")  # trailing newline, as from `echo`
    (tmp_path / "k6").write_text(K6_TOKEN)
    return tmp_path


@pytest.fixture
def store():
    return InMemoryLinkStore()


@pytest.fixture
def client(store, token_dir):
    app = create_app(store=store, tokens=DirectoryTokens(token_dir))
    return TestClient(app, follow_redirects=False)


def create(client, token=None, url="https://example.com/x"):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.post("/api/links", json={"url": url}, headers=headers)


def test_create_without_a_token_is_401_with_a_bearer_challenge(client):
    r = create(client)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert "detail" in r.json()


@pytest.mark.parametrize("token", [OWNER_TOKEN, K6_TOKEN])
def test_each_named_token_can_create(client, token):
    assert create(client, token).status_code == 201


@pytest.mark.parametrize(
    "token",
    [
        "not-a-real-token-0123456789abcdef0123456789",
        OWNER_TOKEN[:-1],  # a prefix of a real token
        OWNER_TOKEN + "x",
        "owner",  # a token's name is not its value
    ],
)
def test_create_with_an_unknown_token_is_401(client, token):
    r = create(client, token)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert "detail" in r.json()


def test_only_the_bearer_scheme_is_accepted(client):
    r = client.post(
        "/api/links",
        json={"url": "https://example.com"},
        headers={"Authorization": f"Basic {OWNER_TOKEN}"},
    )
    assert r.status_code == 401


def test_the_link_records_which_token_created_it(client, store):
    code = create(client, K6_TOKEN).json()["code"]
    assert store.get(code).created_by == "k6"


def test_token_name_is_logged_and_values_never_are(client, caplog):
    caplog.set_level("DEBUG")
    caplog.set_level("DEBUG", logger="app")  # the app logger sets its own level
    created = create(client, K6_TOKEN)
    refused = create(client, OWNER_TOKEN + "x")

    assert created.status_code == 201
    assert "k6" in caplog.text
    for token in (K6_TOKEN, OWNER_TOKEN):
        assert token not in caplog.text
        for r in (created, refused):
            assert token not in r.text
            assert token not in str(r.headers)


def test_the_token_name_is_not_public(client):
    # Token names identify testers; the public lookup does not reveal them.
    code = create(client, K6_TOKEN).json()["code"]
    assert "k6" not in client.get(f"/api/links/{code}").text


def test_redirect_lookup_and_health_need_no_token(client):
    code = create(client, OWNER_TOKEN, url="https://example.com/public").json()["code"]
    # A fresh client proves no auth state carries over from the create.
    anonymous = TestClient(client.app, follow_redirects=False)
    r = anonymous.get(f"/{code}")
    assert r.status_code == 302
    assert r.headers["location"] == "https://example.com/public"
    assert anonymous.get(f"/api/links/{code}").status_code == 200
    assert anonymous.get("/healthz").status_code == 200
    assert anonymous.get("/readyz").status_code == 200


def test_the_token_is_checked_before_the_body(client):
    # An unauthenticated caller learns nothing about input validation.
    assert create(client, url="javascript:alert(1)").status_code == 401
    assert create(client, OWNER_TOKEN, url="javascript:alert(1)").status_code == 422


def test_open_mode_refuses_to_start():
    # ADR 0020: `open` needs abuse safeguards that do not exist yet.
    with pytest.raises(StartupError, match="LINK_CREATE_MODE=open"):
        create_app(create_mode="open")


def test_open_mode_from_the_environment_refuses_to_start(monkeypatch):
    monkeypatch.setenv("LINK_CREATE_MODE", "open")
    with pytest.raises(StartupError, match="safeguards"):
        create_production_app()


def test_an_unknown_mode_refuses_to_start(monkeypatch):
    # A typo must not silently pick a mode.
    monkeypatch.setenv("LINK_CREATE_MODE", "tokens")
    with pytest.raises(StartupError, match="LINK_CREATE_MODE"):
        create_production_app()


def test_the_production_app_is_token_gated_by_default():
    # The module-level app was built at import from the default environment.
    # Without LINK_TOKENS_DIR mounted it has no tokens, so creation fails closed.
    from app.main import app as production_app

    client = TestClient(production_app)
    assert create(client).status_code == 401
    assert create(client, "not-a-real-token-0123456789abcdef0123456789").status_code == 401
