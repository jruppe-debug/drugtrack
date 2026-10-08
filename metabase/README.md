# AACT + Clinical Tracker in Metabase

This project serves two purposes:

- It runs a local copy of AACT (the ClinicalTrials.gov PostgreSQL mirror) in Docker.
- It loads the Clinical Tracker ontology into that same database, so Metabase can compare the two.

Paths below are relative to `%USERPROFILE%` (PowerShell `$env:USERPROFILE`, Git Bash `$USERPROFILE`).

## Containers

| Container | Image | Host port | Volume | Purpose |
|---|---|---|---|---|
| `aact-db` | postgres:17 | **5433** (container 5432) | `aact-pgdata` | AACT restore + tracker schemas |
| `metabase` | metabase/metabase | 3000 | `metabase-data` (H2 app DB, `MB_DB_FILE=/metabase-data/metabase.db`) | Dashboards |

- Both containers are on the Docker network `aact-net`.
- Both use `--restart unless-stopped`, so they come back on their own after Docker Desktop restarts.
- `aact-db` also mounts `metabase-postgres-project/aact` read-only at `/dump`. That folder holds the original `postgres.dmp`.

**Why port 5433:** a native Windows PostgreSQL 18 already listens on `localhost:5432`. Port 5432 on localhost is NOT the AACT container.

## Connection settings

| From | Host | Port | DB | User | Password | SSL |
|---|---|---|---|---|---|---|
| Metabase, local copy | `aact-db` | 5432 | aact | metabase_ro | metabase_ro | off |
| Metabase, hosted AACT | `aact-db.ctti-clinicaltrials.org` | 5432 | aact | aact_u11310 | `AACT_PASSWORD` in `Clinical Tracker\.env` | on |
| Windows apps (psql, DBeaver, Python) | `localhost` | 5433 | aact | metabase_ro | metabase_ro | off |
| Windows apps, hosted AACT | `aact-db.ctti-clinicaltrials.org` | 5432 | aact | aact_u11310 | from `.env` | require |

Connection strings:

```
postgresql://metabase_ro:metabase_ro@aact-db:5432/aact         # inside Docker / Metabase
postgresql://metabase_ro:metabase_ro@localhost:5433/aact       # from Windows
postgresql://aact_u11310:<AACT_PASSWORD>@aact-db.ctti-clinicaltrials.org:5432/aact?sslmode=require
```

- Do not use `localhost` inside Metabase. Metabase runs in a container, so `localhost` means Metabase itself.
- The admin login for the local copy is `postgres` / `aact_admin`. Use it only for loading data and creating views.
- Metabase UI: http://localhost:3000. Add a database under Admin settings > Databases > Add database > PostgreSQL. Then run "Sync database schema now".

Quick check (expect 605812 rows):

```
docker exec -it aact-db psql -U metabase_ro -d aact -c "select count(*) from ctgov.studies;"
```

## Schemas in the `aact` database

| Schema | Source | Notes |
|---|---|---|
| `ctgov` | AACT dump (restored with `pg_restore --no-owner --no-acl -j 4`) | Never modified, except one added index `browse_conditions_nct_term` |
| `tracker` | `Clinical Tracker/AACT_bridge/inputs/clinical_tracker.db` | Copied by `load_tracker.py` |
| `source_index` | `Clinical Tracker/AACT_bridge/inputs/source_index.db` | Copied by `load_tracker.py` |
| `tracker_debug` | `debug_views.sql` | Comparison views (see below) |

`metabase_ro` has USAGE and SELECT on all four schemas.

## Debug logic (`tracker_debug` views)

| View | What it compares | `issue` values |
|---|---|---|
| `study_drift` | `tracker.aact_studies` full join `ctgov.studies` on nct_id; compares phase and overall_status (empty tracker phase is treated as NULL) | missing_in_aact, new_in_aact, phase_and_status_changed, phase_changed, status_changed, match |
| `report_aact_check` | `tracker.clinical_reports.confirmed_in_aact` vs whether the NCT exists in AACT | no_nct_id, flag_true_but_missing, flag_false_but_present, flag_null, ok |
| `mesh_drift` | Tracker `condition_mesh_terms` JSON array vs `ctgov.browse_conditions` (all mesh types, because the tracker stores mesh-list plus mesh-ancestor terms). Limited to tracker NCTs | only_in_tracker, only_in_aact |
| `orphans` | Seven referential checks inside the tracker (report_drugs, report_diseases, indications, indication_reports vs drugs, diseases, clinical_reports, indications) | `check_name` column + count `n` |
| `trial_drug_links` | For CLINICAL_TRIAL reports: does the tracker drug name or `drug_from_source` appear (LIKE match) in `ctgov.interventions` for that NCT | boolean `name_found_in_aact_interventions` |

### Findings so far

Comparison of the tracker snapshot against the AACT dump, 2026-10-07:

- `study_drift`:
  - 734 status_changed, 10 phase_changed, 10 phase_and_status_changed.
  - 374,829 AACT trials are not in the tracker.
- `report_aact_check`: 2 NCTs have `confirmed_in_aact = 1` but are missing from AACT.
- `mesh_drift`: 78 only_in_aact, 7 only_in_tracker.
- Mapping gaps:
  - 77,054 `report_drugs` rows have NULL `chembl_id`.
  - 85,066 `report_diseases` rows have NULL `disease_id`.
  - These are likely the main ontology gaps.
- `orphans`: `CHEMBL453514` is referenced but missing from `drugs`.

## Common tasks

### Reload the tracker after the SQLite files change

Run in Git Bash:

```bash
MSYS_NO_PATHCONV=1 docker run --rm --network aact-net \
  -v "$(cygpath -w "$USERPROFILE/metabase-postgres-project"):/app:ro" \
  -v "$(cygpath -w "$USERPROFILE/Clinical Tracker/AACT_bridge/inputs"):/in:ro" \
  -e TRACKER_DIR=/in -e PGHOST=aact-db python:3.11-slim \
  sh -c "pip install -q psycopg2-binary && python /app/load_tracker.py"
```

The loader drops and recreates each table, and strips NUL bytes because Postgres text cannot hold them. Re-apply the views afterwards; see the next section.

### Re-apply the debug views

Run in Git Bash. `MSYS_NO_PATHCONV=1` stops Git Bash from rewriting `/tmp/...` into a Windows path.

```bash
export MSYS_NO_PATHCONV=1
docker cp "$USERPROFILE/metabase-postgres-project/debug_views.sql" aact-db:/tmp/debug_views.sql
docker exec aact-db psql -U postgres -d aact -v ON_ERROR_STOP=1 -q -f /tmp/debug_views.sql
```

The script is safe to rerun. Afterwards, re-sync the schema in Metabase.

### Start / stop

```
docker start aact-db metabase
docker stop metabase aact-db
```

Data persists in the volumes `aact-pgdata` and `metabase-data`.

### Refresh the local AACT copy

1. Download a newer dump from aact.ctti-clinicaltrials.org.
2. Extract it into `metabase-postgres-project/aact/`.
3. `pg_restore` it into a fresh database.
4. Re-apply `debug_views.sql`.

As an alternative, point Metabase at hosted AACT, which refreshes nightly.

## Hosted AACT password (open item, 2026-10-07)

- The password was reset on the AACT site.
- `Clinical Tracker\.env` now holds the new password. The previous file is saved as `.env.bak`.
- At 2026-10-07 the hosted database still rejected the new password and accepted the old one. The reset had not propagated yet.
- Once the new password works, delete `.env.bak`.
- If the new password still fails, restore `.env.bak`, or redo the reset.

## Visualizing in Metabase

1. Go to + New > Question > AACT local > Tracker Debug > pick a view.
2. Use Summarize and Group by (usually on `issue`), then Visualize, then Save.
3. Collect the saved questions on a dashboard (+ New > Dashboard).

Suggested starting charts:

- `study_drift`: count by `issue`.
- `report_aact_check`: count by `issue`.
- `orphans`: sum of `n` by `check_name`.
- `mesh_drift`: count by `issue`.
- `trial_drug_links`: count by `name_found_in_aact_interventions`.
