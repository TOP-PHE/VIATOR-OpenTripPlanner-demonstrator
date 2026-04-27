# VIATOR

UIC MERITS-aligned rail journey-planning demonstrator built on OpenTripPlanner.

> **Powered by TrackOnPath SAS** &nbsp;·&nbsp; Contact: [patrick.heuguet@trackonpath.com](mailto:patrick.heuguet@trackonpath.com)
>
> **© 2026 UIC — International Union of Railways. All rights reserved.**
> Licensed under the Apache License, Version 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

| Document | What's inside |
|---|---|
| [`VIATOR-strategy.md`](VIATOR-strategy.md) | Why this exists, data sources, master data, multi-session strategy, roadmap |
| [`VIATOR-technical-spec.md`](VIATOR-technical-spec.md) | Detailed engineering spec — data model, APIs, DevOps, implementation order |
| [`docker/README.md`](docker/README.md) | Phase-1 container stack documentation |
| [`docker/INSTALL.md`](docker/INSTALL.md) | VPS install procedure |
| [`branding/VIATOR-brand-brief.md`](branding/VIATOR-brand-brief.md) | Brand identity (icon, palette, typography) |

## Repository layout

```
OpenJourneyPlanner/
├── app/                          # FastAPI admin app + worker (Python 3.12)
├── tests/                        # pytest suite (unit + integration)
├── docker/                       # container stack (web, worker, otp, postgres, nginx)
├── branding/                     # VIATOR icon, lockup, brand brief
├── .github/workflows/            # CI: lint, type, test, scan, sonar
├── pyproject.toml                # ruff, black, mypy, pytest, coverage config
├── requirements.txt              # runtime deps
├── requirements-dev.txt          # CI / dev deps (test, lint, type, security)
├── .pre-commit-config.yaml       # pre-commit hooks
├── sonar-project.properties      # SonarCloud config
└── .gitignore
```

## Quick start (local dev)

```bash
# 1. Install Python deps
python -m venv .venv && source .venv/bin/activate    # or .venv\Scripts\activate on Windows
pip install -r requirements-dev.txt

# 2. Install pre-commit hooks
pre-commit install
pre-commit install --hook-type pre-push

# 3. Run tests + lint
pytest
ruff check .
mypy app/

# 4. Run the stack
cd docker
cp .env.example .env             # edit secrets
docker compose up -d
```

For VPS deployment, see [`docker/INSTALL.md`](docker/INSTALL.md).

## First-platform-admin bootstrap

After `docker compose up -d`, the database has no users. Create the first
**platform admin** with a one-time bootstrap call:

```bash
# 1. In .env, set BOOTSTRAP_TOKEN to a random secret string.
echo "BOOTSTRAP_TOKEN=$(openssl rand -hex 32)" >> docker/.env
docker compose restart web

# 2. Hit the bootstrap endpoint (replace TOKEN with the value from .env):
curl -X POST http://VPS_IP/api/auth/bootstrap-platform-user \
  -H 'Content-Type: application/json' \
  -d '{
    "token": "TOKEN",
    "email": "you@example.org",
    "name": "Your Name",
    "password": "a-strong-passphrase-12+chars"
  }'

# 3. Visit http://VPS_IP/login and sign in.
# 4. Optionally remove BOOTSTRAP_TOKEN from .env — the endpoint always 403s once
#    a platform admin exists, so it's defence-in-depth.
```

## Admin surfaces

After signing in as a platform admin, the nav exposes:

- **`/journey`** — the public search UI (fanout-default, with origin badges per trip)
- **`/admin/users`** — promote / deactivate users
- **`/admin/sessions`** — create / archive sessions, toggle fanout
- **`/admin/config`** — SMTP, concurrency limits, registration policy, retention; SMTP test button
- **`/admin/master/stations`** — Trainline-seeded UIC station registry; refresh + drift queue
- **`/admin/reports`** — search volume per session/user, top O&D pairs, trip-source distribution

The **content_manager** role gets `/journey` + master-data write access.
The **end_user** role only gets `/journey`.

## Operational crons (in-process via APScheduler)

Run automatically inside the `web` container:

| Cron | Schedule (UTC) | What it does |
|---|---|---|
| `retention` | daily 03:00 | Three-tier prune of raw responses → trips → search summaries → audit per `JOURNEY_*_RETENTION_DAYS` |
| `master_stations_refresh` | daily 04:00 | Pulls Trainline CSV; manual edits never overwritten — drift surfaced in admin UI |

Set env var `VIATOR_DISABLE_CRONS=1` to disable (used in tests).

## License & attribution

VIATOR is licensed under the **Apache License, Version 2.0** — see [`LICENSE`](LICENSE).

- **Copyright** © 2026 UIC — International Union of Railways. All rights reserved.
- **Powered by** [TrackOnPath SAS](mailto:patrick.heuguet@trackonpath.com).
- **Third-party software and data** acknowledged in [`NOTICE`](NOTICE).

This attribution must be preserved in any redistribution and must appear in the
footer of any user-facing surface (web UI, generated documents, exported reports).
