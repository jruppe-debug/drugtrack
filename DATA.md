# Data not stored in git

Large or regenerable files are listed in `.gitignore`. This file says where each one comes from.

| Path | Size | How to get it back |
|---|---|---|
| `data/opentargets_bulk/*.parquet`, `AACT_bridge/inputs/opentargets/*.parquet` | ~97 MB | `build_full_universe.py` calls `sources/open_targets/bulk.py: download_bulk_datasets()`. That function pulls release 26.06 from `https://ftp.ebi.ac.uk/pub/databases/opentargets/platform/26.06/output/<dataset>/`. |
| `metabase/aact/postgres.dmp` | 2.56 GB | Download the AACT snapshot from https://aact.ctti-clinicaltrials.org (free account). Then restore it as `metabase/README.md` describes ("Refresh the local AACT copy"). A newer snapshot will not match byte for byte. The snapshot used here had sha256 `ffdf2362f4e6ea00fe0b6a813783bf0b2cbdef4f1a6b1345b4e81fed946ab6d6`. |
| `output/clinical_tracker.db`, `AACT_bridge/inputs/clinical_tracker.db` | 335 MB each | Rebuild with `build_full_universe.py` and `build_truth_table.py`. Both need AACT credentials in `.env`. |
| `AACT_bridge/inputs/source_index.db` | 13 MB | `DrugTrack/tools/source_index.py --refresh` |
| `output/*.json`, `output/*.csv` | small | Pipeline outputs. Rebuild with the root scripts. |
| `DrugTrack/data/*.json` (`tree.json`, `match.json`, `central_anchor.json`, the caches) | ~67 MB | Pipeline outputs. `tree.json` comes from `DrugTrack/tools/export_disease_tree.py`. The viz server (`npm run viz`) needs `tree.json`. |
| `DrugTrack/data/references/` (FDA 656 MB / 908 files, ClinicalTrials.gov 65 MB / 1628 files) | 721 MB | Fetched by `DrugTrack/tools/approval_anchor.py` from accessdata.fda.gov and clinicaltrials.gov. FDA may answer automated requests with a bot challenge, so a refetch can be slow or incomplete. |

## Planned: release asset

The FDA references, `tree.json`, `central_anchor.json` and `clinical_tracker.db` are slow to rebuild. The plan is to upload them as one zip attached to a GitHub Release, not to commit them. Not done yet.

## Secrets

- `.env` and `DrugTrack/.env` hold the AACT password and other settings. They are never committed.
- Copy `.env.example` to `.env` and fill in the values.
- `.env.bak` is also ignored.
