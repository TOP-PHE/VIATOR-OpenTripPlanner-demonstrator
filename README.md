# VIATOR

UIC MERITS-aligned rail journey-planning demonstrator built on OpenTripPlanner.

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

## License

TBD (likely Apache-2.0 to match OSCAR).
