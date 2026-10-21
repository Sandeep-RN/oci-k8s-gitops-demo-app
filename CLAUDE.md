# oci-k8s-gitops-demo-app (application + CI)

Workspace-level rules and PLAN.md live one folder up. Read them first.

- This repo: backend, frontend, Dockerfiles, GitHub Actions. No Kubernetes
  manifests here; those belong in the gitops repo.
- Build backend features test-first. Keep /healthz and /readyz working.
- CI pins third-party Actions by commit SHA. Image tags are immutable (git SHA).
- No secrets in code, images or workflow files. Use GitHub secrets.
