# oci-k8s-gitops-demo-app (application + CI)

Workspace-level rules and PLAN.md live one folder up. Read them first.

- This repo: backend, frontend, Dockerfiles, GitHub Actions. No Kubernetes
  manifests here; those belong in the gitops repo.
- Build backend features test-first. Keep /healthz and /readyz working.
- CI pins third-party Actions by commit SHA. Image tags are immutable (git SHA).
- No secrets in code, images or workflow files. Use GitHub secrets.

## Agent skills

### Issue tracker

Issues for all three repos live in GitHub Issues on Sandeep-RN/oci-k8s-gitops-demo; pass `-R Sandeep-RN/oci-k8s-gitops-demo` to `gh issue` commands. See that repo's `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md` in the demo repo.

### Domain docs

Single-context, kept in the demo repo: one GLOSSARY.md and docs/adr/ at its root (this repo has none). See `docs/agents/domain.md` in the demo repo.
