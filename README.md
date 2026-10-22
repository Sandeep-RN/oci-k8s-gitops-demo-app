# oci-k8s-gitops-demo-app

Application code and CI for the OCI Kubernetes GitOps demo: a small **link
shortener** with a FastAPI backend, a static frontend and PostgreSQL storage.

> **Status:** scaffolding only. Backend, frontend and workflows are not
> written yet (Phase 1 starts with the backend skeleton, built test-first).

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

## Planned CI (ADR 0009)

On each change: lint, test, Trivy scan, build an arm64 image, push it to GHCR
tagged with the git SHA (tags are immutable), then open an auto-merging
tag-bump PR in the gitops repo. ArgoCD deploys from there; CI never touches
the cluster.

## Working here

- Build backend features test-first; keep `/healthz` and `/readyz` working.
- Pin third-party GitHub Actions by commit SHA.
- Changes go through a pull request (ADR 0011).
- Install hooks once: `pip install pre-commit && pre-commit install`
  (gitleaks with custom OCI rules).
- **This repo is public.** No secrets in code, images or workflow files; use
  GitHub secrets.
