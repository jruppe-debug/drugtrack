# Clinical Tracker — AACT × Open Targets collection & cross-reference prototype

A collection-layer prototype for two public sources: **AACT**
(ClinicalTrials.gov, mirrored into Postgres) and **Open Targets Platform**
(curated drug/target/disease associations, GraphQL). It pulls raw data
from both, and — now that AACT is live — evaluates which fields can
actually serve as a reliable join key between them, by attempting real
joins and measuring the confirmed match rate. It does not build a general
normalization pipeline; that's still a separate, larger phase (see the
bottom of this file).

## Status (verified 2026-09-17, AACT credentials added same day)

| Piece | Tested against live service? | Result |
|---|---|---|
| Open Targets client (`sources/open_targets/client.py`) | Yes | All 4 methods return real data |
| Open Targets MCP server (`sources/open_targets/mcp_server.py`) | Yes | Tool call verified through the actual MCP `call_tool` path, not just the client |
| AACT client (`sources/aact/client.py`) | Yes | Live query as `aact_u11310`; schema introspected directly from the database (see Architecture) |
| AACT MCP server (`sources/aact/mcp_server.py`) | Imports/registers cleanly; tool calls exercise the same tested client code | 6 tools |
| `collect.py` end-to-end | Yes | Both halves succeed live |
| `cross_reference.py` end-to-end | Yes | NCT-ID join: 261/261 candidate IDs confirmed present in AACT for imatinib (100%) |
| `build_truth_table.py` across 10 drugs | Yes | 8,409/8,409 candidate NCT IDs confirmed (100%), 1,485 indication rows, 85 flagged approved — see below |
| `build_full_universe.py` across all 22,407 drugs | Yes | 230,985/230,990 candidate NCT IDs confirmed (99.998%), 86,468 total indications (11,175 approved), in 86.3s end-to-end — see below |

Credentials live in `.env` (gitignored, never committed): `aact_u11310` /
`aact-db.ctti-clinicaltrials.org:5432/aact`. Open Targets still needs no
key.

## Architecture

```
                 ┌─────────────────────────┐        ┌──────────────────────────────┐
                 │   AACT (Postgres)        │        │  Open Targets (GraphQL)      │
                 │   ctgov schema           │        │  api.platform.opentargets.org│
                 └────────────┬─────────────┘        └───────────────┬───────────────┘
                              │ psycopg2, read-only                  │ requests, POST
                              ▼                                      ▼
                 sources/aact/client.py                 sources/open_targets/client.py
                 (AACTClient: raw SQL +                 (OpenTargetsClient: 4 hand-
                  5 convenience queries)                 verified GraphQL queries)
                              │                                      │
              ┌───────────────┴──────────┐          ┌────────────────┴──────────────┐
              ▼                           ▼          ▼                                ▼
   sources/aact/mcp_server.py   collect.py (orchestration)         sources/open_targets/mcp_server.py
   (MCPServer: 6 stdio tools     — calls both clients directly,     (MCPServer: 4 stdio tools for
    for an agent to call)        writes one JSON file PER SOURCE,    an agent to call)
              │                  no join, no shared ID space)                  │
              │                           │                                    │
              │                           ▼                                    │
              │                  cross_reference.py                            │
              │                  — pulls one drug from both clients directly, │
              │                  — extracts AACT-sourced NCT IDs that Open    │
              │                    Targets embeds in indication evidence,     │
              │                  — looks them up in AACT by exact nct_id,     │
              │                  — reports the real, measured match rate      │
              │                    instead of assuming one.                   │
              └──────────────────────────────┬─────────────────────────────────┘
                                              ▼
                                     mcpServers config
                                (points an agent's MCP client at both
                                 servers as stdio subprocesses — not
                                 wired into any agent harness yet)
```

Three scripts, three different jobs, deliberately not merged into one:
- **`collect.py`** — dumps each source's native, un-joined data for a drug. Proves both sources are individually reachable and answers "what does each one say, in its own vocabulary."
- **`cross_reference.py`** — the new piece. Takes the same per-source data and asks "which fields, if any, actually let me join these two datasets without guessing." It answers that with a measured number, not an assumption.
- **`mcp_server.py`** (×2) — the interface an *agent* would use instead of either script; same underlying client code, different caller.

All three sit on the same two client modules — nothing about the query logic is duplicated between the orchestration scripts and the MCP tools.

## AACT — mechanism, and what the live schema actually looks like

AACT is not an API. It's a nightly `pg_dump` of ClinicalTrials.gov's
entire relational schema, hosted as a public, read-only PostgreSQL
database — you connect with any Postgres client and run SQL. Verified
live by querying `information_schema` directly (not by reading the
[published ERD](https://aact.ctti-clinicaltrials.org/assets/schema/erd-d760da39.png), which turned out to be documentation-only, see below):

- **71 tables** in the `ctgov` schema, `studies` being the hub (603,073 rows).
- **Zero declared foreign-key or primary-key constraints anywhere in the schema.** The ERD's arrows are not enforced by Postgres — every table-to-table relationship (`interventions.nct_id → studies.nct_id`, `browse_conditions.nct_id → studies.nct_id`, etc.) is an implicit string match, not a real FK.
- That implicit structure was then **confirmed empirically, not assumed**: an orphan check (rows in `interventions`, `conditions`, `browse_interventions`, `browse_conditions` whose `nct_id` has no matching row in `studies`) came back **0 orphans across all four tables** against 603,073 studies. The relationship the ERD documents is real, just not enforced — code touching this schema has to preserve that join discipline itself.

The other wrinkle: sponsors free-type intervention and condition names
("Gleevec", "imatinib mesylate", "STI571" could all be the same drug
across different trials). AACT ships `browse_interventions` /
`browse_conditions` — CT.gov's own best-effort mapping of that free text
to MeSH terms. Useful, but not a substitute for RxNorm — `client.py`
pulls both the raw string and the MeSH term for that reason.

`sources/aact/client.py` (`AACTClient`), all verified against real column names:
- `run_query(sql)` — any read-only `SELECT`/`WITH`, guarded client-side and via `default_transaction_read_only=on`.
- `studies_by_intervention(drug_term)` / `studies_by_condition(condition_term)` — substring match against MeSH-mapped columns.
- `studies_by_nct_ids(nct_ids)` — exact-match lookup for a known ID list, with condition MeSH terms aggregated per study. This is the method `cross_reference.py` uses for the real join (see below).
- `interventions_for_study(nct_id)` / `conditions_for_study(nct_id)` — raw + MeSH rows for one trial.

## Open Targets — mechanism

`https://api.platform.opentargets.org/api/v4/graphql`, one POST, no API
key. Aggregates target-disease evidence from 23 sources; for drugs, it
curates indication-level data — for each disease a drug has ever been
trialed or approved for, the highest clinical phase reached *for that
specific indication*.

Two things verified by introspecting the live schema rather than trusting secondhand framing:
1. The GraphQL argument is still named `efoId`, but the platform migrated its disease ontology from EFO to MONDO. Live IDs are `MONDO_*`; an old `EFO_*` ID silently returns `null`.
2. **Each indication row can carry `clinicalReports` — and when one has `source == "AACT"`, its `id` is a literal lowercase ClinicalTrials.gov NCT ID** (e.g. `"nct00080665"`). This is the finding the cross-reference evaluation below is built on.

`sources/open_targets/client.py` (`OpenTargetsClient`), 4 hand-verified queries:
- `search(query_string, entity_names)` — plain-English name → ChEMBL/Ensembl/MONDO ID.
- `drug_indications(chembl_id)` — indications + max phase + `clinicalReports` (source, type, id, url) per indication.
- `disease_associated_targets(mondo_id)` / `target_associated_diseases(ensembl_id)` — score-ranked, inverse of each other.

## Evaluating index values across the two sources

Two candidate join keys were tested for real, not assumed — run via `python cross_reference.py <drug>`:

### 1. NCT ID — structural join, strong

Open Targets sources some of its indication evidence directly from AACT and exposes the underlying trial's NCT ID verbatim. `cross_reference.py`:
1. Pulls a drug's indications from Open Targets, including `clinicalReports`.
2. Extracts every `id` where `source == "AACT"`, normalizes casing (`nct00080665` → `NCT00080665`).
3. Looks those IDs up in AACT via `studies_by_nct_ids()` — an exact-match `WHERE nct_id = ANY(...)`, not a fuzzy search.
4. Reports how many resolved to a real AACT study.

**Live result for imatinib: 261 candidate NCT IDs extracted from 89 indications, 261/261 (100%) confirmed present in AACT.** This is a hard, structural foreign key hiding in Open Targets' data — no vocabulary reconciliation needed for the indications it covers. It doesn't cover everything: only the subset of an indication's evidence that Open Targets itself sourced from AACT (imatinib's other evidence sources were ATC, TTD, EMA Human Drugs — 89/89 indications had *some* clinical report, 86/89 had at least one AACT-sourced one).

### 2. Condition-name string match — baseline, weak

For comparison: case-insensitive equality between Open Targets' `disease.name` and AACT's MeSH condition terms for the same drug's trials.

**Live result for imatinib: 1 exact match out of 89 distinct Open Targets disease names.** ("neoplasm" style AACT MeSH terms vs. Open Targets' more specific disease names essentially never collide as exact strings.) This is the numeric evidence for why the original ask was right to flag name-matching as the failure mode to avoid — it isn't a matter of degree, it's close to useless on its own.

**Conclusion this prototype supports**: the NCT-ID join should be the primary mechanism wherever it's available (it's exact, verified, and free), and the RxNorm/UMLS/MeSH-MONDO normalization layer is what's needed specifically for the remainder — indications Open Targets sourced from somewhere other than AACT (ChEMBL, ATC, EMA, TTD, etc.), where no shared trial ID exists to lean on.

Full per-drug output: `output/cross_reference_<drug>.json` (includes every joined row: AACT title/phase/status/MeSH terms next to Open Targets' disease name/ID/max phase for the same trial).

## The drug → indication source of truth (`build_truth_table.py`)

`cross_reference.py` proves the NCT-ID join on one drug. `build_truth_table.py`
runs the same mechanism across 10 drugs, chosen deliberately to stress-test
generalization rather than repeat easy wins — half oncology biologics/small
molecules (imatinib, osimertinib, pembrolizumab, rituximab, lenalidomide),
half non-oncology and old-generic edge cases (adalimumab, sildenafil,
metformin, atorvastatin, aspirin) where curation is typically messier.

**Standardization rule**: `is_approved` is set from the indication-level
`maxClinicalStage == "APPROVAL"`, not the drug's overall clinical stage —
a drug can be approved for one indication and only investigational for
another (imatinib: approved for CML, phase-1 for breast cancer), and this
table preserves that distinction per-row rather than collapsing it.

**Live result across all 10 drugs**: 1,485 total indication rows, 85
flagged approved, **8,409/8,409 candidate NCT IDs confirmed present in
AACT (100%)** — the confirmation rate held even for the messy stress-test
drugs (aspirin: 379/379; metformin: 1,137/1,137; atorvastatin: 381/381).
Zero drug-name resolution failures or QA flags across all 10.

Spot-checked the actual indication content for correctness, not just the
counts: imatinib's approved list (CML, GIST, dermatofibrosarcoma
protuberans, hypereosinophilic syndrome) and adalimumab's (RA, Crohn's,
ulcerative colitis, psoriasis, ankylosing spondylitis, uveitis...) are all
real, correct approved indications. Aspirin's 30-item approved list also
checks out against its actual decades-long label history (headache,
fever, dysmenorrhea, MI/stroke prevention) — most of those older/OTC
entries show `0` AACT-confirmed NCT IDs, which is expected (approved
before modern trial registration existed), not a defect in the join.

**One real finding from this pass, worth knowing before normalization work starts**: the "disease" ID space Open Targets returns is not
purely MONDO. Aspirin's approved indications include `HP_0002315`
("Headache") and `HP_0001945`("Fever") — Human Phenotype Ontology IDs,
not MONDO — alongside `MONDO_*` and `EFO_*` IDs for other rows. Open
Targets' disease entity spans at least three ontologies depending on how
symptom-like vs. disease-like the indication is; the MeSH/EFO
reconciliation step planned for the next phase will need to handle HPO
too, not just MONDO.

Outputs:
- `output/truth_table.json` — every indication row for all 10 drugs, standardized fields, both approved and investigational.
- `output/truth_table_approved.csv` — filtered to `is_approved == true` only; the actual "foundational source of truth" table, human-readable.
- `output/truth_table_stats.json` — per-drug counts and confirmation rates.

Run with `python build_truth_table.py` (defaults to the 10-drug set) or `python build_truth_table.py <drug1> <drug2> ...` for a custom list.

## Scaling to all 22,407 drugs (`build_full_universe.py`)

`build_truth_table.py` loops the GraphQL client per drug — fine for 10, not for 22,407 (that's hours of sequential network round trips). The full-universe version instead:

1. **Pulls the same data in bulk, not per-drug.** Open Targets publishes every quarterly release as world-readable Parquet files on EBI's FTP server (`sources/open_targets/bulk.py`) — no auth, no API. Four datasets cover everything the GraphQL client was fetching one drug at a time: `drug_molecule` (22,407 rows), `disease` (47,080), `clinical_indication` (86,468 drug×disease×stage rows), and `clinical_report` (289,955 deduplicated evidence records — the same `clinicalReports`/NCT-ID mechanism `cross_reference.py` found, just pre-flattened and shared across indications instead of nested per-indication). Row counts were cross-checked against the live GraphQL API's own wildcard-search totals (drug: 22,407 both ways; disease: 47,080 both ways) — same data, two access paths.
2. **Loads it into SQLite**, not another JSON file. `output/clinical_tracker.db` — proper normalized tables (`drugs`, `diseases`, `indications`, `clinical_reports`, `indication_reports` junction, `aact_studies` cache), real indexes, actually queryable instead of requiring a full reparse for every question. See "Storage architecture" below for why SQLite over Parquet/Postgres.
3. **Runs the AACT confirmation once, globally**, not once per drug. All 230,990 distinct AACT-sourced NCT IDs across the *entire* catalog get extracted, deduplicated, and confirmed against AACT in 47 chunked `WHERE nct_id = ANY(...)` queries (5,000 IDs each) — instead of re-confirming the same shared trials (e.g. a big basket trial cited by 10 different drugs) once per drug the way the 10-drug script would.

**Live result, full run, 2026-09-17**: 22,407 drugs loaded (11,364 with ≥1 curated indication — the other ~11,000 are experimental/tool compounds with no indication data yet), 86,468 indications (11,175 approved), 289,955 clinical reports, **230,985/230,990 AACT NCT-ID candidates confirmed (99.998%)**. Total pipeline time including the download, load, and full confirmation pass: **86.3 seconds**. Database size: 215.3 MB.

**The 5 unconfirmed IDs, checked rather than waved off**: queried AACT directly for all 5 (`NCT02404896`, `NCT04229004`, `NCT04284254`, `NCT04693598`, `NCT05147662`) — none exist in AACT's live database at all, right now. This isn't a bug in the join; it's real drift between two sources with different refresh cadences: Open Targets' 26.06 release was built mid-June 2026 and cited these as AACT-sourced evidence at that time, but AACT refreshes nightly from ClinicalTrials.gov, and these 5 trials have since been withdrawn or removed from the registry entirely. Worth knowing for the eventual production pipeline: a small, nonzero fraction of cross-source references will always be stale by the time you check them, and that's expected, not a defect to chase to zero.

**It's queryable now, not just storable** — two real ad-hoc SQL queries run against the store, not pre-baked into any script:

```sql
-- Which drugs have the most approved indications?
SELECT d.name, count(*) AS n FROM indications i
JOIN drugs d ON d.chembl_id = i.chembl_id
WHERE i.is_approved = 1 GROUP BY d.name ORDER BY n DESC LIMIT 5;
-- DEXAMETHASONE (75), PREDNISONE (70), DEXAMETHASONE SODIUM PHOSPHATE (62), PREDNISOLONE (57), CORTISONE ACETATE (43)
-- (correct, unsurprising: broad-spectrum corticosteroids get used across the most conditions)

-- Imatinib's approved indications, joined live to their confirmed AACT trial counts:
SELECT dis.name, i.max_clinical_stage,
  (SELECT count(*) FROM indication_reports ir JOIN clinical_reports cr ON cr.report_id = ir.report_id
   WHERE ir.indication_id = i.indication_id AND cr.confirmed_in_aact = 1) AS confirmed_trials
FROM indications i JOIN drugs d ON d.chembl_id = i.chembl_id JOIN diseases dis ON dis.disease_id = i.disease_id
WHERE d.name = 'IMATINIB' AND i.is_approved = 1;
-- CML (101 trials), GIST (44), dermatofibrosarcoma protuberans (3), hypereosinophilic syndrome (2), neoplasm (1), myelodysplastic/myeloproliferative disease (0)
```

Neither of those was possible against the flat JSON/CSV output without loading the whole thing into memory and writing custom Python to replicate what SQL already does.

### Storage architecture: why SQLite here, not Parquet or Postgres

All three were evaluated before building this:
- **SQLite (chosen)**: embedded, zero new infrastructure (stdlib `sqlite3`, one file), real B-tree indexes for point lookups, proper normalized relational schema. Right fit because this is a single-writer batch pipeline, not concurrent multi-reader/writer access.
- **Parquet**: the *input* format here (that's what made the bulk download practical), but a poor fit as the *output* store — no point-lookup index, no update-in-place, write-once/read-many. Good as an export target for the future pandas/DuckDB-based normalization phase, not as the system of record.
- **Postgres**: right call only if this becomes a live, concurrently-queried service (e.g. the MCP servers reading from our own Postgres instead of static files) — not needed for a batch ETL job, and would mean standing up and operating a server this project doesn't otherwise need.

Run with `python build_full_universe.py` (downloads bulk data once, cached after; re-running re-loads and re-confirms) or `python build_full_universe.py --skip-confirm` to load the Open Targets side only.

## Combination regimens (`report_drugs`, `report_diseases`, `regimen_prevalence.py`)

`clinical_report.drugs` and `.diseases` are list columns in the source Parquet — one report can name multiple drugs, and that's genuinely how Open Targets/ChEMBL represent a combination regimen: one shared evidence record with several drugs attached, not one row per drug. The `indications` table can't represent this at all (strictly one drug per row), so two more tables expose it directly: `report_drugs(report_id, chembl_id, drug_from_source)` and `report_diseases(report_id, disease_id, disease_from_source)`, loaded by `load_report_links()` in `build_full_universe.py`.

**Validated, not assumed**: cross-checked a sample report against AACT's raw intervention list. Result: Open Targets' `drugs` field is a *ChEMBL-curated* view — it consolidated two vaccine formulations into one canonical entity and correctly dropped a non-drug adjuvant that AACT lists as a separate intervention. It's not a literal 1:1 parse of AACT's intervention rows, but it correctly captures real co-administered combinations (verified on venetoclax+azacitidine's real VIALE-A trial: exactly those two drugs, placebo correctly excluded).

`regimen_prevalence.py <drug1> <drug2> ... --disease-contains "<text>"` computes, per matching disease subgroup: how many reports show the full combo (subset match — a 3-drug report containing both named drugs still counts, even with a third agent added) versus each drug appearing without that combo (which includes true monotherapy *and* other combinations with different partners — the column deliberately doesn't claim to isolate monotherapy alone).

**Live result, venetoclax + azacitidine, AML**: 214/619 (34.6%) of matching evidence reports show the full combination; nearly all of that (213/213) confirmed present in AACT. Only 4 of the ~70 AML-related disease entities in the ontology have *any* evidence for either drug — almost everything is tagged to the general "acute myeloid leukemia" term rather than a molecular/cytogenetic subtype, so subtype-level breakdown isn't very informative for this particular pair with today's curation.

## How MCP fits in

Each source has a plain client module (the actual query logic) and a thin
`mcp_server.py` wrapping it with `MCPServer` (the `mcp` SDK's high-level
server class — `FastMCP` in SDK v1, renamed in v2, same `@mcp.tool()`
decorator API):

```python
@mcp.tool()
def ot_drug_indications(chembl_id: str) -> dict:
    """All curated indications for a drug..."""
    return _client.drug_indications(chembl_id)
```

An agent's MCP client lists the server's tools (name, docstring, schema
from type hints), calls one by name, gets structured JSON back — never
raw SQL/GraphQL it has to construct, never a webpage to scrape. Verified
end-to-end for Open Targets through the actual `call_tool` protocol path,
not just the underlying client. To point an agent (Qwen via `dsh`,
Claude, anything MCP-aware) at these servers:

```json
{
  "mcpServers": {
    "aact": { "command": "python", "args": ["-m", "sources.aact.mcp_server"] },
    "open_targets": { "command": "python", "args": ["-m", "sources.open_targets.mcp_server"] }
  }
}
```

This config isn't wired into any agent harness yet — that's a separate step.

## Running it

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      # fill in AACT_USER / AACT_PASSWORD, or use the working .env already here
python collect.py imatinib          # per-source raw dump, no join
python cross_reference.py imatinib  # evaluates + performs the real NCT-ID join, reports match rate
```

Outputs, per drug, all un-committed (gitignored):
- `output/open_targets_<drug>.json` — search hits + full indication list, ChEMBL/MONDO-keyed.
- `output/aact_<drug>.json` — matching trials, NCT-keyed, MeSH-vocabulary.
- `output/cross_reference_<drug>.json` — the evaluated join: confirmed NCT-ID matches (with side-by-side AACT/Open Targets fields) plus the weak string-match baseline.

## What's still not here: general normalization

The NCT-ID join closes a real gap, but only for indications whose evidence traces back through AACT specifically. Measured across the full 86,468-indication universe, not just the 10-drug sample:

- **86.0%** of all indications (74,347/86,468) have at least one AACT-confirmed trial — the NCT-ID join covers most of the catalog when investigational-stage indications are included.
- **But only 37.4%** of *approved* indications (4,178/11,175) do. Approved indications skew heavily toward having no AACT-linked evidence at all — plausibly because many approvals are older than modern trial-registration requirements, or sourced from drug labels/regulatory filings (DailyMed, EMA, FDA, ATC) rather than a registered trial. **This is the number that actually matters for the "approved indication source of truth" use case**: for roughly 6 in 10 approved indications, the free NCT-ID shortcut doesn't apply, and real normalization is the only way to anchor them to AACT (or to conclude, correctly, that AACT simply has no trial for that approval).

So the general normalization layer is still necessary, not optional-nice-to-have:

- drug name → RxCUI, via RxNorm/RxNav (needed for AACT's raw sponsor-entered intervention names, which have no ID at all today)
- cross-vocabulary concept mapping, via UMLS Metathesaurus (RxCUI ↔ ChEMBL ↔ MeSH)
- indication/disease → MeSH ↔ MONDO ↔ HPO reconciliation (see the HPO finding above) for the ~62.6% of approved indications with no AACT-sourced clinical report to anchor on

That's still separate, still not started — this repo now gets you a verified, full-universe, queryable relational store from both sources *plus* one proven, measured shortcut (the NCT-ID join) that covers the easier ~37% of the approved-indication problem, and precisely quantifies the ~63% that the normalization work actually needs to solve.
