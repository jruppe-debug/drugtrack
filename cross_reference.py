"""
Evaluates candidate join keys ("index values") between AACT and Open
Targets for one drug, by actually attempting the joins and reporting the
confirmed match rate -- empirically, not by assumption.

Two candidates are evaluated:

  1. NCT ID (strong, structural). Open Targets embeds literal
     ClinicalTrials.gov NCT IDs inside drug.indications.clinicalReports
     whenever an indication's evidence traces back to a trial AACT itself
     sources (report.source == "AACT"). We extract those IDs and look them
     up directly in AACT by exact nct_id match -- an ID join, not a name
     match. This is the "confirmation of references" step: it proves the
     trials Open Targets says exist really do exist in AACT, and pulls
     AACT's own record for a side-by-side comparison.

  2. Condition-name string match (weak, baseline). Case-insensitive
     equality between Open Targets' disease.name and AACT's MeSH condition
     terms for studies of the same drug. Kept only to quantify how much
     worse plain name-matching does than the NCT-ID join -- this is the
     gap the RxNorm/MeSH-EFO normalization layer exists to close, for the
     indications the NCT-ID join can't reach (those sourced by Open
     Targets from ChEMBL, ATC, EMA, TTD, etc. instead of AACT).

Usage:
    python cross_reference.py imatinib
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from sources.aact.client import AACTClient
from sources.open_targets.client import OpenTargetsClient

load_dotenv()

OUTPUT_DIR = Path(__file__).parent / "output"
NCT_RE = re.compile(r"^nct(\d+)$", re.IGNORECASE)


def extract_aact_nct_ids(indication_row: dict) -> list[str]:
    """AACT-sourced NCT IDs from one indication row's clinicalReports,
    normalized to AACT's own uppercase NCTxxxxxxxx form."""
    ids = []
    for report in indication_row.get("clinicalReports") or []:
        if report.get("source") == "AACT":
            m = NCT_RE.match(report["id"])
            if m:
                ids.append(f"NCT{m.group(1)}")
    return ids


def evaluate(drug_name: str) -> dict:
    ot = OpenTargetsClient()
    aact = AACTClient()

    hits = ot.search(drug_name, entity_names=["drug"], size=1)
    if not hits:
        raise SystemExit(f"No Open Targets drug match for '{drug_name}'")
    chembl_id = hits[0]["id"]
    drug = ot.drug_indications(chembl_id)
    indication_rows = drug["indications"]["rows"]

    # -- Index 1: NCT ID, structural join --------------------------------
    nct_to_indication: dict[str, dict] = {}
    for row in indication_rows:
        for nct_id in extract_aact_nct_ids(row):
            nct_to_indication.setdefault(nct_id, row)

    candidate_nct_ids = list(nct_to_indication.keys())
    aact_matches = aact.studies_by_nct_ids(candidate_nct_ids)
    confirmed = {m["nct_id"] for m in aact_matches}
    unconfirmed = [n for n in candidate_nct_ids if n not in confirmed]

    joined = []
    for m in aact_matches:
        ot_row = nct_to_indication[m["nct_id"]]
        joined.append(
            {
                "nct_id": m["nct_id"],
                "aact_brief_title": m["brief_title"],
                "aact_phase": m["phase"],
                "aact_overall_status": m["overall_status"],
                "aact_condition_mesh_terms": m["condition_mesh_terms"],
                "open_targets_disease_name": ot_row["disease"]["name"],
                "open_targets_disease_id": ot_row["disease"]["id"],
                "open_targets_max_clinical_stage": ot_row["maxClinicalStage"],
            }
        )

    confirmation_rate = round(len(confirmed) / len(candidate_nct_ids), 3) if candidate_nct_ids else None

    # -- Index 2: condition-name string match, weak baseline --------------
    aact_studies_for_drug = aact.studies_by_intervention(drug_name, limit=500)
    aact_condition_terms = {
        s["condition_mesh_term"].strip().lower() for s in aact_studies_for_drug if s.get("condition_mesh_term")
    }
    ot_disease_names = {row["disease"]["name"].strip().lower() for row in indication_rows}
    name_overlap = aact_condition_terms & ot_disease_names

    return {
        "drug_name_queried": drug_name,
        "resolved_chembl_id": chembl_id,
        "total_open_targets_indications": len(indication_rows),
        "index_1_nct_id": {
            "candidate_nct_ids_from_open_targets": len(candidate_nct_ids),
            "confirmed_present_in_aact": len(confirmed),
            "unconfirmed": unconfirmed,
            "confirmation_rate": confirmation_rate,
            "joined_rows": joined,
        },
        "index_2_condition_name_string_match": {
            "distinct_aact_mesh_condition_terms_for_drug": len(aact_condition_terms),
            "distinct_open_targets_disease_names": len(ot_disease_names),
            "exact_case_insensitive_overlap": sorted(name_overlap),
            "overlap_count": len(name_overlap),
        },
    }


def main(drug_name: str) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    result = evaluate(drug_name)
    slug = drug_name.strip().lower().replace(" ", "_")
    path = OUTPUT_DIR / f"cross_reference_{slug}.json"
    path.write_text(json.dumps(result, indent=2, default=str))

    idx1 = result["index_1_nct_id"]
    idx2 = result["index_2_condition_name_string_match"]
    rate_str = f"{idx1['confirmation_rate']:.0%}" if idx1["confirmation_rate"] is not None else "n/a"

    print(f"Drug: {drug_name} -> {result['resolved_chembl_id']}")
    print(f"Open Targets indications: {result['total_open_targets_indications']}")
    print(
        f"Index 1 (NCT ID, structural join): {idx1['candidate_nct_ids_from_open_targets']} candidate IDs, "
        f"{idx1['confirmed_present_in_aact']} confirmed present in AACT ({rate_str} confirmation rate)"
    )
    print(
        f"Index 2 (condition-name string match, baseline): {idx2['overlap_count']} exact matches out of "
        f"{idx2['distinct_open_targets_disease_names']} distinct Open Targets disease names for this drug"
    )
    print(f"Full report -> {path}")


if __name__ == "__main__":
    drug = sys.argv[1] if len(sys.argv) > 1 else "imatinib"
    main(drug)
