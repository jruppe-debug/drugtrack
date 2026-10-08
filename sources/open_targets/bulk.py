"""
Bulk Parquet access for Open Targets Platform -- the mechanism for
querying the FULL drug universe (22,407 drugs) instead of one at a time
through the GraphQL API in client.py.

Mechanism: Open Targets publishes every quarterly release as a directory
of Parquet files, world-readable, on EBI's FTP server (also mirrored to a
public GCS bucket). No auth, no API, just HTTP GET. Path pattern
(confirmed live by listing the actual FTP directory, not guessed):

    https://ftp.ebi.ac.uk/pub/databases/opentargets/platform/<release>/output/<dataset>/

Release "26.06" (year.month) is current as of 2026-09 and matches the
GraphQL API's own reported apiVersion (26.6.3) -- confirmed by
cross-checking row counts against live GraphQL wildcard-search totals
(drug: 22,407 both ways; disease: 47,080 both ways).

This module downloads exactly the 4 datasets this pipeline needs, not
the full ~50-dataset release:

  - drug_molecule       22,407 rows -- one row per drug. Replaces looping
                         client.search() + client.drug_indications() over
                         every drug individually.
  - disease              47,080 rows -- disease id -> name lookup.
  - clinical_indication  86,468 rows -- drug x disease x max clinical
                         stage, with a clinicalReportIds list linking out
                         to the evidence backing it. Replaces the
                         GraphQL `indications` field.
  - clinical_report     289,955 rows -- individual evidence records, deduplicated
                         and shared across indications (not one row per
                         indication). Of these, 230,990 have
                         source == "AACT", and for those, `id` is a
                         literal lowercase NCT ID -- same discovery as
                         client.py's drug_indications(), just at full
                         scale and pre-deduplicated.

Fragility worth knowing: `clinical_indication` and `clinical_report` are
each written as one named file, but `drug_molecule` (and some other
datasets) are partitioned Spark output with a per-release random hash in
the filename (e.g. `part-00000-5581d2c3-...-c000.snappy.parquet`). That
hash will differ next release. Rather than hardcode it, this module lists
each dataset's directory and picks the first real .parquet file it finds.
"""

from __future__ import annotations

import re
import urllib.request
from pathlib import Path

RELEASE = "26.06"
BASE_URL = f"https://ftp.ebi.ac.uk/pub/databases/opentargets/platform/{RELEASE}/output"

DATASET_DIRS = {
    "drug_molecule": "drug_molecule",
    "disease": "disease",
    "clinical_indication": "clinical_indication",
    "clinical_report": "clinical_report",
}

DATA_DIR = Path(__file__).parent.parent.parent / "data" / "opentargets_bulk"


def _resolve_filename(dataset_dir: str) -> str:
    """List a dataset's directory on the FTP server and return the first
    real .parquet filename (skips manifest files like _SUCCESS)."""
    url = f"{BASE_URL}/{dataset_dir}/"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", errors="replace")
    files = re.findall(r'href="([^"]+\.parquet)"', html)
    if not files:
        raise RuntimeError(f"No .parquet file found under {url}")
    return files[0]


def download_bulk_datasets(force: bool = False) -> dict[str, Path]:
    """Download each dataset to data/opentargets_bulk/<name>.parquet if
    not already present locally. Returns {dataset_name: local_path}."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name, dataset_dir in DATASET_DIRS.items():
        local_path = DATA_DIR / f"{name}.parquet"
        paths[name] = local_path
        if local_path.exists() and not force:
            continue
        filename = _resolve_filename(dataset_dir)
        url = f"{BASE_URL}/{dataset_dir}/{filename}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=300) as resp, open(local_path, "wb") as f:
            f.write(resp.read())
    return paths
