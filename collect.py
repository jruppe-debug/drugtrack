"""
Collection-layer prototype: pull raw, comparable data for one drug from
AACT and Open Targets, and write each source's output to its own file.

This deliberately does NOT join the two outputs. AACT gives back
sponsor/MeSH vocabulary and NCT-keyed trial rows; Open Targets gives back
ChEMBL/MONDO-keyed indication rows. Concatenating those by string-matching
drug/condition names here would be exactly the fragile approach the
normalization layer (RxNorm/RxNav for drug -> RxCUI, MeSH/EFO-MONDO for
indications, UMLS for cross-vocabulary mapping) exists to avoid. This
script's job ends at "both sources' raw, source-native records for the
same real-world drug, sitting side by side on disk."

Usage:
    python collect.py imatinib
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from sources.aact.client import AACTClient, AACTConfigError
from sources.open_targets.client import OpenTargetsClient

load_dotenv()

OUTPUT_DIR = Path(__file__).parent / "output"


def collect_open_targets(drug_name: str) -> dict:
    client = OpenTargetsClient()
    hits = client.search(drug_name, entity_names=["drug"], size=5)
    if not hits:
        return {"drug_name_queried": drug_name, "error": "no match in Open Targets search"}

    # Take the top hit as-is; picking the "right" synonym among multiple
    # salt forms (e.g. IMATINIB vs IMATINIB MESYLATE) is itself a
    # normalization decision we are explicitly deferring.
    top = hits[0]
    indications = client.drug_indications(top["id"])
    return {
        "drug_name_queried": drug_name,
        "search_hits": hits,
        "resolved_chembl_id": top["id"],
        "indications": indications,
    }


def collect_aact(drug_name: str) -> dict:
    try:
        client = AACTClient()
    except AACTConfigError as exc:
        return {"drug_name_queried": drug_name, "error": str(exc)}

    studies = client.studies_by_intervention(drug_name, limit=50)
    return {"drug_name_queried": drug_name, "studies": studies}


def main(drug_name: str) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    slug = drug_name.strip().lower().replace(" ", "_")

    print(f"[open_targets] querying for '{drug_name}'...")
    ot_result = collect_open_targets(drug_name)
    ot_path = OUTPUT_DIR / f"open_targets_{slug}.json"
    ot_path.write_text(json.dumps(ot_result, indent=2))
    if "error" in ot_result:
        print(f"[open_targets] FAILED: {ot_result['error']}")
    else:
        print(
            f"[open_targets] OK: resolved '{drug_name}' -> {ot_result['resolved_chembl_id']}, "
            f"{ot_result['indications']['indications']['count']} indications -> {ot_path}"
        )

    print(f"[aact] querying for '{drug_name}'...")
    aact_result = collect_aact(drug_name)
    aact_path = OUTPUT_DIR / f"aact_{slug}.json"
    aact_path.write_text(json.dumps(aact_result, indent=2, default=str))
    if "error" in aact_result:
        print(f"[aact] FAILED: {aact_result['error']}")
    else:
        print(f"[aact] OK: {len(aact_result['studies'])} studies -> {aact_path}")


if __name__ == "__main__":
    drug = sys.argv[1] if len(sys.argv) > 1 else "imatinib"
    main(drug)
