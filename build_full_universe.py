"""
Full-universe pipeline: loads Open Targets' complete drug/indication/
evidence catalog (22,407 drugs) from bulk Parquet -- zero GraphQL calls
-- into a local SQLite store, then runs ONE global AACT confirmation pass
over every distinct AACT-sourced NCT ID referenced anywhere in that
catalog, instead of repeating the confirmation per-drug the way
build_truth_table.py does for its 10-drug sample.

Why bulk Parquet instead of the GraphQL client used elsewhere in this
repo: looping OpenTargetsClient.drug_indications() over 22,407 drugs
means 22,407 sequential network round trips. The bulk datasets are the
exact same underlying data, downloaded once (~95MB, see
sources/open_targets/bulk.py) and queried locally.

Why SQLite instead of another JSON/CSV dump: 10 drugs already produced a
1MB JSON dominated by NCT-ID lists, and a flat file can't be queried --
you'd reload and re-parse the whole thing for any question. SQLite gives
real indexed lookups (by drug, by disease, by NCT ID) and proper
normalized tables instead of a semicolon-joined string in a CSV cell, for
zero new infrastructure (stdlib sqlite3, one file).

Usage:
    python build_full_universe.py                  # build + confirm everything against AACT
    python build_full_universe.py --skip-confirm    # load Open Targets tables only, skip the AACT pass
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from pathlib import Path

import pyarrow.parquet as pq
from dotenv import load_dotenv

from sources.aact.client import AACTClient
from sources.open_targets.bulk import download_bulk_datasets

load_dotenv()

DB_PATH = Path(__file__).parent / "output" / "clinical_tracker.db"
NCT_RE = re.compile(r"^nct(\d+)$", re.IGNORECASE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS drugs (
    chembl_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    drug_type TEXT,
    maximum_clinical_stage TEXT
);

CREATE TABLE IF NOT EXISTS diseases (
    disease_id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS indications (
    indication_id TEXT PRIMARY KEY,
    chembl_id TEXT NOT NULL,
    disease_id TEXT NOT NULL,
    max_clinical_stage TEXT,
    is_approved INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_indications_chembl_id ON indications(chembl_id);
CREATE INDEX IF NOT EXISTS idx_indications_disease_id ON indications(disease_id);

CREATE TABLE IF NOT EXISTS clinical_reports (
    report_id TEXT PRIMARY KEY,
    source TEXT,
    type TEXT,
    nct_id TEXT,
    confirmed_in_aact INTEGER
);
CREATE INDEX IF NOT EXISTS idx_clinical_reports_nct_id ON clinical_reports(nct_id);

CREATE TABLE IF NOT EXISTS indication_reports (
    indication_id TEXT NOT NULL,
    report_id TEXT NOT NULL,
    PRIMARY KEY (indication_id, report_id)
);
CREATE INDEX IF NOT EXISTS idx_indication_reports_report_id ON indication_reports(report_id);

CREATE TABLE IF NOT EXISTS aact_studies (
    nct_id TEXT PRIMARY KEY,
    brief_title TEXT,
    phase TEXT,
    overall_status TEXT,
    condition_mesh_terms TEXT
);

-- clinical_report.drugs and .diseases are LIST columns in the source
-- Parquet -- one report can name multiple drugs (this is how Open
-- Targets/ChEMBL actually represent combination regimens: one shared
-- evidence record, several drugs attached) and multiple diseases. These
-- two tables expose that instead of collapsing it through `indications`,
-- which is strictly one-drug-per-row and can't represent a combo at all.
CREATE TABLE IF NOT EXISTS report_drugs (
    report_id TEXT NOT NULL,
    chembl_id TEXT,
    drug_from_source TEXT,
    PRIMARY KEY (report_id, drug_from_source)
);
CREATE INDEX IF NOT EXISTS idx_report_drugs_chembl_id ON report_drugs(chembl_id);
CREATE INDEX IF NOT EXISTS idx_report_drugs_report_id ON report_drugs(report_id);

CREATE TABLE IF NOT EXISTS report_diseases (
    report_id TEXT NOT NULL,
    disease_id TEXT,
    disease_from_source TEXT,
    PRIMARY KEY (report_id, disease_from_source)
);
CREATE INDEX IF NOT EXISTS idx_report_diseases_disease_id ON report_diseases(disease_id);
CREATE INDEX IF NOT EXISTS idx_report_diseases_report_id ON report_diseases(report_id);
"""
# Foreign keys are declared above for documentation but not enforced
# (PRAGMA foreign_keys stays OFF): bulk-loading in dependency order is
# simple enough without it, and this store is a batch-built reference
# artifact, not a target for ad-hoc app-level inserts.


def load_drugs(conn: sqlite3.Connection, path: Path) -> None:
    t = pq.read_table(path, columns=["id", "name", "drugType", "maximumClinicalStage"])
    rows = zip(
        t.column("id").to_pylist(),
        t.column("name").to_pylist(),
        t.column("drugType").to_pylist(),
        t.column("maximumClinicalStage").to_pylist(),
    )
    conn.executemany("INSERT OR REPLACE INTO drugs VALUES (?, ?, ?, ?)", rows)


def load_diseases(conn: sqlite3.Connection, path: Path) -> None:
    t = pq.read_table(path, columns=["id", "name"])
    rows = zip(t.column("id").to_pylist(), t.column("name").to_pylist())
    conn.executemany("INSERT OR REPLACE INTO diseases VALUES (?, ?)", rows)


def load_clinical_reports(conn: sqlite3.Connection, path: Path) -> None:
    t = pq.read_table(path, columns=["id", "source", "type"])
    rows = []
    for rid, source, rtype in zip(t.column("id").to_pylist(), t.column("source").to_pylist(), t.column("type").to_pylist()):
        nct_id = None
        if source == "AACT":
            m = NCT_RE.match(rid)
            if m:
                nct_id = f"NCT{m.group(1)}"
        rows.append((rid, source, rtype, nct_id, None))
    conn.executemany("INSERT OR REPLACE INTO clinical_reports VALUES (?, ?, ?, ?, ?)", rows)


def load_report_links(conn: sqlite3.Connection, path: Path) -> None:
    t = pq.read_table(path, columns=["id", "drugs", "diseases"])
    drug_rows = []
    disease_rows = []
    for rid, drug_list, disease_list in zip(t.column("id").to_pylist(), t.column("drugs").to_pylist(), t.column("diseases").to_pylist()):
        for d in drug_list or []:
            drug_rows.append((rid, d.get("drugId"), d.get("drugFromSource")))
        for d in disease_list or []:
            disease_rows.append((rid, d.get("diseaseId"), d.get("diseaseFromSource")))
    conn.executemany("INSERT OR REPLACE INTO report_drugs VALUES (?, ?, ?)", drug_rows)
    conn.executemany("INSERT OR REPLACE INTO report_diseases VALUES (?, ?, ?)", disease_rows)


def load_indications(conn: sqlite3.Connection, path: Path) -> None:
    t = pq.read_table(path, columns=["id", "drugId", "diseaseId", "maxClinicalStage", "clinicalReportIds"])
    indication_rows = []
    junction_rows = []
    for iid, drug_id, disease_id, stage, report_ids in zip(
        t.column("id").to_pylist(),
        t.column("drugId").to_pylist(),
        t.column("diseaseId").to_pylist(),
        t.column("maxClinicalStage").to_pylist(),
        t.column("clinicalReportIds").to_pylist(),
    ):
        indication_rows.append((iid, drug_id, disease_id, stage, 1 if stage == "APPROVAL" else 0))
        for rid in report_ids or []:
            junction_rows.append((iid, rid))
    conn.executemany("INSERT OR REPLACE INTO indications VALUES (?, ?, ?, ?, ?)", indication_rows)
    conn.executemany("INSERT OR REPLACE INTO indication_reports VALUES (?, ?)", junction_rows)


def confirm_against_aact(conn: sqlite3.Connection, chunk_size: int = 5000) -> dict:
    aact = AACTClient()
    candidate_ids = [
        r[0] for r in conn.execute("SELECT DISTINCT nct_id FROM clinical_reports WHERE nct_id IS NOT NULL").fetchall()
    ]

    confirmed_count = 0
    for i in range(0, len(candidate_ids), chunk_size):
        chunk = candidate_ids[i : i + chunk_size]
        matches = aact.studies_by_nct_ids(chunk)
        confirmed_count += len(matches)

        conn.executemany("UPDATE clinical_reports SET confirmed_in_aact = 1 WHERE nct_id = ?", [(m["nct_id"],) for m in matches])
        conn.executemany(
            "INSERT OR REPLACE INTO aact_studies VALUES (?, ?, ?, ?, ?)",
            [
                (m["nct_id"], m["brief_title"], m["phase"], m["overall_status"], json.dumps(m["condition_mesh_terms"] or []))
                for m in matches
            ],
        )
        conn.commit()
        done = min(i + chunk_size, len(candidate_ids))
        print(f"  {done}/{len(candidate_ids)} candidates checked, {confirmed_count} confirmed so far")

    # Anything with an nct_id that never matched an AACT row is a confirmed miss, not "unknown" -- distinguish it from rows with no nct_id at all (which stay NULL, meaning "not applicable").
    conn.execute("UPDATE clinical_reports SET confirmed_in_aact = 0 WHERE nct_id IS NOT NULL AND confirmed_in_aact IS NULL")
    conn.commit()
    return {"candidates": len(candidate_ids), "confirmed": confirmed_count}


def main(skip_confirm: bool) -> None:
    t0 = time.time()

    print("Downloading/verifying bulk Open Targets datasets (cached after first run)...")
    paths = download_bulk_datasets()

    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    print("Loading drugs...")
    load_drugs(conn, paths["drug_molecule"])
    print("Loading diseases...")
    load_diseases(conn, paths["disease"])
    print("Loading clinical reports...")
    load_clinical_reports(conn, paths["clinical_report"])
    print("Loading report drug/disease links (combination-regimen data)...")
    load_report_links(conn, paths["clinical_report"])
    print("Loading indications...")
    load_indications(conn, paths["clinical_indication"])
    conn.commit()

    stats = {
        "drugs": conn.execute("SELECT count(*) FROM drugs").fetchone()[0],
        "diseases": conn.execute("SELECT count(*) FROM diseases").fetchone()[0],
        "indications": conn.execute("SELECT count(*) FROM indications").fetchone()[0],
        "approved_indications": conn.execute("SELECT count(*) FROM indications WHERE is_approved = 1").fetchone()[0],
        "drugs_with_indications": conn.execute("SELECT count(DISTINCT chembl_id) FROM indications").fetchone()[0],
        "clinical_reports": conn.execute("SELECT count(*) FROM clinical_reports").fetchone()[0],
        "aact_candidate_nct_ids": conn.execute(
            "SELECT count(DISTINCT nct_id) FROM clinical_reports WHERE nct_id IS NOT NULL"
        ).fetchone()[0],
    }
    print(
        f"\nLoaded: {stats['drugs']} drugs ({stats['drugs_with_indications']} with >=1 indication), "
        f"{stats['diseases']} diseases, {stats['indications']} indications ({stats['approved_indications']} approved), "
        f"{stats['clinical_reports']} clinical reports, {stats['aact_candidate_nct_ids']} distinct AACT NCT-ID candidates"
    )

    if skip_confirm:
        print("Skipping AACT confirmation pass (--skip-confirm)")
    else:
        print("\nConfirming AACT NCT IDs (chunked, 5000 per query)...")
        confirm_stats = confirm_against_aact(conn)
        rate = confirm_stats["confirmed"] / confirm_stats["candidates"] if confirm_stats["candidates"] else 0
        print(f"AACT confirmation: {confirm_stats['confirmed']}/{confirm_stats['candidates']} ({rate:.1%})")

    conn.close()
    elapsed = time.time() - t0
    size_mb = DB_PATH.stat().st_size / 1_000_000
    print(f"\nDatabase: {DB_PATH} ({size_mb:.1f} MB)")
    print(f"Total time: {elapsed:.1f}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-confirm", action="store_true", help="Load Open Targets tables only, skip the AACT confirmation pass")
    args = parser.parse_args()
    main(skip_confirm=args.skip_confirm)
