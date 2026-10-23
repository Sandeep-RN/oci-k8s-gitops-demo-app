# oci-k8s-gitops-demo-app

Application code and CI for the OCI Kubernetes GitOps demo: a small **link
shortener** with a FastAPI backend, a static frontend and PostgreSQL storage.

> **Status:** backend skeleton (`/healthz`, `/readyz`, Dockerfile) with full CI
> and the GitOps tag bump. Link endpoints and the frontend are not written yet.

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
pytest && ruff check . && ruff format --check .
docker build -t link-shortener:dev . && docker run --rm -p 8000:8000 link-shortener:dev
```

## Working here

- Build backend features test-first; keep `/healthz` and `/readyz` working.
- Pin third-party GitHub Actions by commit SHA.
- Changes go through a pull request (ADR 0011).
- Install hooks once: `pip install pre-commit && pre-commit install`
  (gitleaks with custom OCI rules).
- **This repo is public.** No secrets in code, images or workflow files; use
  GitHub secrets.
