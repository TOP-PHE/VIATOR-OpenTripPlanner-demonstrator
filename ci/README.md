# `ci/` — CI policy and reference files

This folder holds the **policy** files and helpers consumed by the GitHub Actions workflows.
Workflow definitions themselves live in `.github/workflows/`.

| File | Purpose |
|---|---|
| `trivy-config-ignore.rego` | Rego policy for `trivy config` scans (compose / Dockerfile **misconfiguration** scanning). Currently a placeholder; will be wired up when we add `trivy config` to the workflow. |

## Why a Rego policy at all?

Trivy has two scan modes:

1. `trivy image <ref>` — scans the **image filesystem** for known CVEs. Configured today via `.trivyignore` at repo root + the `--severity` and `--ignore-unfixed` flags in `.github/workflows/docker.yml`.
2. `trivy config <path>` — scans **config files** (Dockerfile, docker-compose.yml, kubernetes manifests) for known **misconfigurations** (e.g. running as root, mounting docker.sock, missing healthcheck). Configured via Rego policies passed with `--ignore-policy`.

We use mode 1 in the current workflow. Mode 2 will be added later when we wire `trivy config docker/docker-compose.yml` into the pipeline. At that point we expect to need a Rego rule that whitelists the `worker` service's `/var/run/docker.sock` mount — which is intentional (the worker launches OTP-build containers) and documented in `VIATOR-technical-spec.md` §10.1.

Until then, the placeholder file exists to (a) reserve the location and (b) document the intent so the next contributor doesn't add a blanket `--severity LOW` to silence the warning.

## Adding a new ignore — checklist

Before adding ANY ignore (CVE in `.trivyignore` or rule in `trivy-config-ignore.rego`):

1. **Confirm it's HIGH or CRITICAL.** Lower severities aren't enforced anyway.
2. **Confirm it's fixable.** `--ignore-unfixed` already silences unactionable upstream findings.
3. **Write a one-line justification** in the file alongside the ignore.
4. **Add a calendar reminder** to re-evaluate in 30 days.

Blanket severity downgrades are NOT permitted.
