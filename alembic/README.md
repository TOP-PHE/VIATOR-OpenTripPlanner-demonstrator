# Alembic — VIATOR migrations

## Day-to-day

```bash
# Apply all pending migrations to the configured database
alembic upgrade head

# Show current revision
alembic current

# History (oldest → newest)
alembic history

# Generate a new revision after changing models
alembic revision --autogenerate -m "add foo column to bar"

# Generate an empty revision (handwritten DDL, e.g. a VIEW or extension)
alembic revision -m "create journey_trip_provenance view"
```

## Database URL

Read from `app.settings.settings.database_url`, which is in turn read from the
`DATABASE_URL` environment variable. **Never hard-code it in `alembic.ini`.**

Example for local CLI use:

```bash
export DATABASE_URL="postgresql+psycopg://viator_ci:viator_ci@localhost:5432/viator_ci"
alembic upgrade head
```

## Migration naming

Filenames are templated as `YYYYMMDD_HHMM_<slug>.py` (UTC) — see `file_template`
in `alembic.ini`. Choose a slug that describes the *change*, not the *project state*:
good: `add_role_aliases_table`; bad: `update_schema_v3`.

## Postgres-only features used

The schema relies on Postgres-specific types and extensions:

- `CITEXT` (case-insensitive email) — requires `CREATE EXTENSION IF NOT EXISTS citext`.
- `pg_trgm` (trigram autocomplete index on `master_stations.name`) — requires `CREATE EXTENSION IF NOT EXISTS pg_trgm`.
- `pgcrypto` for `gen_random_uuid()` — requires `CREATE EXTENSION IF NOT EXISTS pgcrypto`.
- `JSONB`, `INET`, `ARRAY`, partial unique indexes, `CHECK` constraints.

These extensions are created in the **initial migration** before any tables are built,
and assume the database role has CREATE EXTENSION rights. Managed-Postgres providers
(AWS RDS, GCP CloudSQL, …) sometimes block this; if so, an admin must pre-create the
extensions and the migration's `CREATE EXTENSION IF NOT EXISTS` lines become no-ops.

## Production deployment flow

The web container's entrypoint runs `alembic upgrade head` before starting Uvicorn,
so deployments naturally apply pending migrations. The worker waits on its rebuild-job
table, which can't exist before the migration has run, so there's no race.

For zero-downtime upgrades that change schema in incompatible ways, follow the standard
two-phase pattern:

1. Migration adds the new column/table; old code still works.
2. Code rolls out using the new schema; old code is replaced.
3. A later migration drops the old column/table once the rollback window has passed.

## Rolling back

Migrations are reversible — every `upgrade()` has a paired `downgrade()`.
However, **rolling back the initial migration drops everything**. Use it only on
empty databases.
