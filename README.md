# oci-k8s-gitops-demo-app

Application code and CI for the OCI Kubernetes GitOps demo: a small **link
shortener** with a FastAPI backend, a static frontend and PostgreSQL storage.

> **Status (2026-11-01):** backend API on PostgreSQL with full CI and the
> GitOps tag bump, live at https://demo.sandeeprn.in (HTTPS; creating a link
> needs a token, see below). The frontend is not written yet.

## API

| Method and path | Result |
|---|---|
| `POST /api/links` `{"url": "https://..."}` with `Authorization: Bearer <token>` | `201 {code, url, created_at}`; `401` with `WWW-Authenticate: Bearer` if the token is missing or unknown; `422` unless the URL is http(s) and at most 2048 characters |
| `GET /{code}` | `302` to the stored URL with `Cache-Control: no-store`, or `404`. No token needed |
| `GET /api/links/{code}` | `200 {code, url, created_at}`, or `404`. No token needed |
| `GET /healthz` | Liveness: `200` whenever the process runs. Never checks the database |
| `GET /readyz` | Readiness: `200` once this pod can serve (its connection pool is open). Deliberately does **not** check Postgres (ADR 0016) |

- **Redirects are 302, not 301.** Browsers cache a 301 forever, which would
  hide repeat visits from the redirect SLO (ADR 0008) and make links impossible
  to change.
- **Creating a link needs a token** (ADR 0020). On a public domain, a link
  shortener anyone can use is an open redirector: spam and phishing links would
  get the domain blocklisted. Redirects, lookups, health checks and metrics
  stay public.
  - Tokens are named. They are files in `LINK_TOKENS_DIR` (default
    `/etc/link-shortener/tokens`), one per token: the file name is the token's
    name (`owner`, `k6`), the content its value. That is the layout a mounted
    Kubernetes Secret produces, so adding or revoking one token is one key in
    one SealedSecret. Mount the whole Secret as a volume, not with `subPath`
    (a `subPath` mount never sees Secret updates). Values under 32 characters
    are ignored; generate them
    with `openssl rand -hex 32`. Never put a token in the image, an
    environment variable or the repo.
  - The directory is read on each create, so a revoked token stops working
    once the kubelet refreshes the mounted Secret (about a minute), with no
    restart. With no tokens, the app still starts and serves redirects; every
    create answers `401`.
  - The comparison is constant-time. Each link stores the creating token's
    **name** (`created_by`, not shown by the public lookup), and the log
    records it. Token values are never logged or returned.
  - `LINK_CREATE_MODE` is `token` (the default). `open` is reserved: the app
    refuses to start with it until a stricter rate limit, a destination
    allow/blocklist, link expiry and a way to disable reported links exist.
- **Codes** are 7 random base62 characters, with a unique key and retry on
  collision.
- **A database outage** returns `503`, never `500`, and fails fast: requests
  answer within about 2 s (`PG_POOL_TIMEOUT`, default 2 s) and `/readyz` within
  about 1.5 s. Every store call also runs under a hard time limit, because a
  pooled connection to a frozen server can block the driver with no timeout of
  its own. Liveness and readiness both stay up: the database is shared by
  every replica, so taking pods out of the Service would only turn these fast
  503s into refused connections that no metric counts (ADR 0016, measured in
  issue #53 of the demo repo).
- **Configuration** comes from the standard libpq variables (`PGHOST`, `PGPORT`,
  `PGUSER`, `PGPASSWORD`, `PGDATABASE`). In the container, the app port is
  `UVICORN_PORT` (default 8000); any other uvicorn option can be set the same
  way, for example `UVICORN_LOG_LEVEL`. The table is created at startup, or by
  the first request if Postgres was down then, under an advisory lock, so
  replicas can start together safely. The same step adds the `created_by`
  column to a table created before the token gate.
- **Metrics** for the redirect SLO are served on a separate port
  (`METRICS_PORT`, default 9000) at `/metrics`, never on the app port, which
  is behind the public edge (NLB and Traefik). One histogram,
  `http_request_duration_seconds{method, route, status}`, labelled by route
  *template* (`/{code}`, not each code) so the number of series stays fixed;
  unmatched paths share one `<unmatched>` label. Its buckets include 0.2 s, the
  latency SLO threshold.

Part of a three-repo project. Architecture, ADRs and the issue tracker live in
[oci-k8s-gitops-demo](https://github.com/Sandeep-RN/oci-k8s-gitops-demo);
Kubernetes manifests live in
[oci-k8s-gitops-demo-gitops](https://github.com/Sandeep-RN/oci-k8s-gitops-demo-gitops).
No manifests belong in this repo.

## Layout

```
backend/                FastAPI service (/healthz, /readyz, link endpoints, token gate, metrics)
frontend/               Static frontend
.github/workflows/      CI (ci.yml), secret scan (gitleaks.yml)
```

## CI (ADR 0009)

On every PR: lint, test, a Trivy scan and a multi-arch (amd64 + arm64) build
without pushing (ADR 0015). `test` is a required check on `main`. A separate
`gitleaks` workflow scans the full git history on every PR and push. On `main`
it also:

1. pushes the image to GHCR, tagged with the git SHA (tags are immutable), and
2. opens a PR in the gitops repo that sets that tag, with auto-merge on. The PR
   merges once the gitops `validate` check passes; ArgoCD then deploys it.
   Older open bump PRs are closed as superseded.

The image and tag bump run only when `backend/**` or `.github/workflows/ci.yml`
changed, so a docs-only merge does not rebuild the image or restart pods.
Lint and tests always run. If CI cannot work out the diff, it builds.

CI never talks to a cluster. The cross-repo step uses `GITOPS_TOKEN`, a
fine-grained token limited to the gitops repo (contents and pull requests:
write). Rotate it before it expires.

## Run locally

```
cd backend
python -m venv .venv && .venv/Scripts/activate   # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
pytest                     # unit tests; the Postgres integration tests skip without PGHOST
```

With a throwaway local Postgres (the integration tests then run too):

```
docker run -d --name ls-pg -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1:55432:5432 postgres:18.6-alpine
export PGHOST=localhost PGPORT=55432 PGUSER=postgres PGDATABASE=postgres
pytest -rs

docker build -t link-shortener:dev .
docker network create ls-net && docker network connect ls-net ls-pg
mkdir -p /tmp/ls-tokens && openssl rand -hex 32 > /tmp/ls-tokens/dev   # a local token named "dev"
docker run --rm --network ls-net -p 8000:8000 -v /tmp/ls-tokens:/etc/link-shortener/tokens:ro \
  -e PGHOST=ls-pg -e PGUSER=postgres -e PGDATABASE=postgres link-shortener:dev
curl -s -X POST localhost:8000/api/links -H "authorization: Bearer $(cat /tmp/ls-tokens/dev)" \
  -H 'content-type: application/json' -d '{"url":"https://example.com"}'
```

CI runs the same integration tests against a Postgres service container and
fails if any test is skipped.

## Working here

- Build backend features test-first; keep `/healthz` and `/readyz` working.
- Pin third-party GitHub Actions by commit SHA.
- Changes go through a pull request (ADR 0011).
- Install hooks once: `pip install pre-commit && pre-commit install`
  (gitleaks with custom OCI rules). CI runs gitleaks too, since
  `--no-verify` skips hooks.
- **This repo is public.** No secrets in code, images or workflow files; use
  GitHub secrets.
