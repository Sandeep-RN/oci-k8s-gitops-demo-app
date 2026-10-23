# oci-k8s-gitops-demo-app

Application code and CI for the OCI Kubernetes GitOps demo: a small **link
shortener** with a FastAPI backend, a static frontend and PostgreSQL storage.

> **Status:** backend API on PostgreSQL with full CI and the GitOps tag bump.
> The frontend is not written yet.

## API

| Method and path | Result |
|---|---|
| `POST /api/links` `{"url": "https://..."}` | `201 {code, url, created_at}`; `422` unless the URL is http(s) and at most 2048 characters |
| `GET /{code}` | `302` to the stored URL with `Cache-Control: no-store`, or `404` |
| `GET /api/links/{code}` | `200 {code, url, created_at}`, or `404` |
| `GET /healthz` | Liveness: `200` whenever the process runs. Never checks the database |
| `GET /readyz` | Readiness: `200` when Postgres answers, otherwise `503` |

- **Redirects are 302, not 301.** Browsers cache a 301 forever, which would
  hide repeat visits from the redirect SLO (ADR 0008) and make links impossible
  to change.
- **Codes** are 7 random base62 characters, with a unique key and retry on
  collision.
- **A database outage** returns `503`, never `500`. Liveness stays up so
  Kubernetes does not restart healthy pods; readiness takes them out of the
  Service until Postgres is back.
- **Configuration** comes from the standard libpq variables (`PGHOST`, `PGPORT`,
  `PGUSER`, `PGPASSWORD`, `PGDATABASE`). The table is created at startup under
  an advisory lock, so replicas can start together safely.

Part of a three-repo project. Architecture, ADRs and the issue tracker live in
[oci-k8s-gitops-demo](https://github.com/Sandeep-RN/oci-k8s-gitops-demo);
Kubernetes manifests live in
[oci-k8s-gitops-demo-gitops](https://github.com/Sandeep-RN/oci-k8s-gitops-demo-gitops).
No manifests belong in this repo.

## Layout

```
backend/                FastAPI service (/healthz, /readyz, link endpoints)
frontend/               Static frontend
.github/workflows/      CI
```

## CI (ADR 0009)

On every PR: lint, test, a Trivy scan and a multi-arch (amd64 + arm64) build
without pushing (ADR 0015). On `main` it also:

1. pushes the image to GHCR, tagged with the git SHA (tags are immutable), and
2. opens a PR in the gitops repo that sets that tag, with auto-merge on. The PR
   merges once the gitops `validate` check passes; ArgoCD then deploys it.
   Older open bump PRs are closed as superseded.

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
docker run --rm --network ls-net -p 8000:8000 -e PGHOST=ls-pg -e PGUSER=postgres -e PGDATABASE=postgres link-shortener:dev
curl -s -X POST localhost:8000/api/links -H 'content-type: application/json' -d '{"url":"https://example.com"}'
```

CI runs the same integration tests against a Postgres service container and
fails if any test is skipped.

## Working here

- Build backend features test-first; keep `/healthz` and `/readyz` working.
- Pin third-party GitHub Actions by commit SHA.
- Changes go through a pull request (ADR 0011).
- Install hooks once: `pip install pre-commit && pre-commit install`
  (gitleaks with custom OCI rules).
- **This repo is public.** No secrets in code, images or workflow files; use
  GitHub secrets.
