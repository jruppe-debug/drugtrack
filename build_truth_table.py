"""
Builds the drug -> indication "source of truth" table the rest of this
workflow is meant to be built on top of, across a stress-test set of 10
drugs -- not just the easy, well-curated oncology examples already proven
in cross_reference.py.

Standardization rule: `is_approved` is set from the INDICATION-level
`maxClinicalStage`, not the drug's overall `maximumClinicalStage`. A drug
can be approved for one indication and still only investigational for
another (imatinib: approved for CML, phase-1 for breast cancer) -- that's
exactly the per-indication distinction Open Targets' own indications
widget is built around, and it's the distinction this table preserves.

For each drug: resolve it in Open Targets, pull every curated indication,
and standardize each one into one row with:
  - is_approved                : indication-level maxClinicalStage == "APPROVAL"
  - aact_confirmed_nct_ids     : NCT IDs Open Targets attributes to AACT
                                  for this indication, confirmed to
                                  actually exist in AACT by exact ID
                                  lookup (same mechanism as
                                  cross_reference.py, just run across more
                                  drugs and shaped as a standing table
                                  instead of a one-off printout).

Usage:
    python build_truth_table.py
    python build_truth_table.py imatinib osimertinib ...   # custom drug list
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from cross_reference import extract_aact_nct_ids
from sources.aact.client import AACTClient
from sources.open_targets.client import OpenTargetsClient

load_dotenv()

OUTPUT_DIR = Path(__file__).parent / "output"

# Deliberately not all oncology "easy mode" drugs -- includes old generic
# small molecules (aspirin, metformin, atorvastatin) and a non-oncology
# biologic (adalimumab) specifically to stress-test whether the NCT-ID
# join mechanism generalizes, or only works for well-curated cancer drugs.
DEFAULT_DRUGS = [
    "imatinib",
    "osimertinib",
    "pembrolizumab",
    "adalimumab",
    "rituximab",
    "lenalidomide",
    "sildenafil",
    "metformin",
    "atorvastatin",
    "aspirin",
]


def resolve_drug(ot: OpenTargetsClient, drug_name: str) -> dict | None:
    hits = ot.search(drug_name, entity_names=["drug"], size=5)
    if not hits:
        return None
    top = hits[0]
    # Simple, visible QA flag -- not fuzzy matching, just surfaces the case
    # where the top search hit's ChEMBL preferred name doesn't obviously
    # contain the queried name, so a human can check it rather than
    # silently trusting rank-1. Expect some false positives for generics
    # whose ChEMBL name differs from the common name (e.g. aspirin ->
    # "ACETYLSALICYLIC ACID") -- the flag says "look at this", not "this is wrong".
    top["name_match_flagged"] = drug_name.strip().lower() not in top["name"].strip().lower()
    return top


def build_row(drug_name: str, chembl_id: str, indication_row: dict, confirmed_nct_ids: set[str]) -> dict:
    candidate_ids = extract_aact_nct_ids(indication_row)
    confirmed = [n for n in candidate_ids if n in confirmed_nct_ids]
    return {
        "drug_name_queried": drug_name,
        "chembl_id": chembl_id,
        "disease_id": indication_row["disease"]["id"],
        "disease_name": indication_row["disease"]["name"],
        "max_clinical_stage": indication_row["maxClinicalStage"],
        "is_approved": indication_row["maxClinicalStage"] == "APPROVAL",
        "aact_candidate_nct_ids": candidate_ids,
        "aact_confirmed_nct_ids": confirmed,
        "evidence_sources": sorted({r["source"] for r in (indication_row.get("clinicalReports") or [])}),
    }


def build_for_drug(ot: OpenTargetsClient, aact: AACTClient, drug_name: str) -> tuple[list[dict], dict]:
    resolved = resolve_drug(ot, drug_name)
    if resolved is None:
        return [], {"drug_name_queried": drug_name, "error": "no Open Targets match"}

    chembl_id = resolved["id"]
    drug = ot.drug_indications(chembl_id)
    indication_rows = drug["indications"]["rows"]

    all_candidate_ids = sorted({nct for row in indication_rows for nct in extract_aact_nct_ids(row)})
    aact_matches = aact.studies_by_nct_ids(all_candidate_ids)
    confirmed_nct_ids = {m["nct_id"] for m in aact_matches}

    rows = [build_row(drug_name, chembl_id, r, confirmed_nct_ids) for r in indication_rows]
    approved = [r for r in rows if r["is_approved"]]

    stats = {
        "drug_name_queried": drug_name,
        "resolved_name": resolved["name"],
        "resolved_chembl_id": chembl_id,
        "name_match_flagged": resolved["name_match_flagged"],
        "total_indications": len(rows),
        "approved_indications": len(approved),
        "candidate_nct_ids": len(all_candidate_ids),
        "confirmed_nct_ids": len(confirmed_nct_ids),
        "nct_confirmation_rate": round(len(confirmed_nct_ids) / len(all_candidate_ids), 3) if all_candidate_ids else None,
    }
    return rows, stats


def main(drug_names: list[str]) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    ot = OpenTargetsClient()
    aact = AACTClient()

    all_rows: list[dict] = []
    all_stats: list[dict] = []

    for drug_name in drug_names:
        print(f"[{drug_name}] resolving + pulling indications...")
        rows, stats = build_for_drug(ot, aact, drug_name)
        all_rows.extend(rows)
        all_stats.append(stats)
        if "error" in stats:
            print(f"  FAILED: {stats['error']}")
            continue
        flag = " [name mismatch, check manually]" if stats["name_match_flagged"] else ""
        rate = f"{stats['nct_confirmation_rate']:.0%}" if stats["nct_confirmation_rate"] is not None else "n/a"
        print(
            f"  {stats['resolved_name']} ({stats['resolved_chembl_id']}){flag}: "
            f"{stats['total_indications']} indications, {stats['approved_indications']} approved, "
            f"NCT-ID confirmation {rate} ({stats['confirmed_nct_ids']}/{stats['candidate_nct_ids']})"
        )

    table_path = OUTPUT_DIR / "truth_table.json"
    table_path.write_text(json.dumps(all_rows, indent=2, default=str))

    stats_path = OUTPUT_DIR / "truth_table_stats.json"
    stats_path.write_text(json.dumps(all_stats, indent=2, default=str))

    approved_rows = [r for r in all_rows if r["is_approved"]]
    csv_path = OUTPUT_DIR / "truth_table_approved.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "drug_name_queried",
                "chembl_id",
                "disease_id",
                "disease_name",
                "max_clinical_stage",
                "aact_confirmed_nct_ids",
                "evidence_sources",
            ],
        )
        writer.writeheader()
        for r in approved_rows:
            writer.writerow(
                {
                    "drug_name_queried": r["drug_name_queried"],
                    "chembl_id": r["chembl_id"],
                    "disease_id": r["disease_id"],
                    "disease_name": r["disease_name"],
                    "max_clinical_stage": r["max_clinical_stage"],
                    "aact_confirmed_nct_ids": ";".join(r["aact_confirmed_nct_ids"]),
                    "evidence_sources": ";".join(r["evidence_sources"]),
                }
            )

    total_indications = len(all_rows)
    total_approved = len(approved_rows)
    total_candidates = sum(s.get("candidate_nct_ids", 0) for s in all_stats)
    total_confirmed = sum(s.get("confirmed_nct_ids", 0) for s in all_stats)
    flagged = [s["drug_name_queried"] for s in all_stats if s.get("name_match_flagged")]
    failed = [s["drug_name_queried"] for s in all_stats if "error" in s]

    print(f"\n--- Summary across {len(drug_names)} drugs ---")
    print(f"Total indication rows: {total_indications} ({total_approved} approved)")
    if total_candidates:
        print(f"NCT-ID join: {total_confirmed}/{total_candidates} candidates confirmed ({total_confirmed / total_candidates:.0%})")
    else:
        print("NCT-ID join: no candidates found across any drug")
    print(f"Name-resolution flags (check manually): {flagged or 'none'}")
    print(f"Failed to resolve in Open Targets: {failed or 'none'}")
    print(f"\nFull table -> {table_path}")
    print(f"Per-drug stats -> {stats_path}")
    print(f"Approved-only CSV -> {csv_path}")


if __name__ == "__main__":
    drugs = sys.argv[1:] or DEFAULT_DRUGS
    main(drugs)
