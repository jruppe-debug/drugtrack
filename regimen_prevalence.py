"""
Combination-regimen prevalence: for a given set of drugs and a disease
filter, counts how many clinical-trial evidence records test all of them
together (the combo) versus each one individually or in some other
combination, broken down per matching disease subgroup.

Mechanism: uses report_drugs/report_diseases (see build_full_universe.py)
-- the exploded clinicalReport.drugs / .diseases list columns from Open
Targets' bulk data. One report_id can carry multiple drugs; a report
where {venetoclax, azacitidine} are both present is real evidence that
trial tested that combination together, not two independent monotherapy
facts. Validated by spot-checking against AACT's raw intervention list
(see README) -- it's a ChEMBL-curated view of "which canonical drugs this
trial is evidence for", not a literal parse of every AACT intervention
row (it can consolidate formulations and drops non-drug interventions
like adjuvants/procedures), but it correctly captures real combinations.

"Subgroup" here means disease entity in Open Targets' ontology (MONDO/
EFO/HPO) -- the only subgroup axis actually available in this data. It is
NOT patient-level subgroups (age, fitness, biomarker status) unless those
happen to be encoded as a distinct disease entity (e.g. "AML with mutated
NPM1" is; "elderly, unfit for induction chemo" is not).

Usage:
    python regimen_prevalence.py venetoclax azacitidine --disease-contains "myeloid leukemia"
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "output" / "clinical_tracker.db"


def resolve_chembl_ids(conn: sqlite3.Connection, drug_names: list[str]) -> dict[str, str]:
    resolved = {}
    for name in drug_names:
        row = conn.execute("SELECT chembl_id, name FROM drugs WHERE name LIKE ? ORDER BY length(name) LIMIT 1", (f"%{name.upper()}%",)).fetchone()
        if row is None:
            raise SystemExit(f"No drug matching '{name}' found in the drugs table.")
        resolved[name] = row[0]
        print(f"  resolved '{name}' -> {row[1]} ({row[0]})")
    return resolved


def main(drug_names: list[str], disease_contains: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("Resolving drug names to ChEMBL IDs:")
    chembl_ids = resolve_chembl_ids(conn, drug_names)
    combo_label = " + ".join(drug_names)

    # Every report that touches a disease matching the filter, and how many/which of our drugs it names.
    placeholders = ",".join("?" for _ in chembl_ids)
    rows = conn.execute(
        f"""
        SELECT dis.disease_id, dis.name AS disease_name, rd.report_id, rd.chembl_id,
               cr.confirmed_in_aact
        FROM report_diseases rdz
        JOIN diseases dis ON dis.disease_id = rdz.disease_id
        JOIN report_drugs rd ON rd.report_id = rdz.report_id
        JOIN clinical_reports cr ON cr.report_id = rd.report_id
        WHERE dis.name LIKE ? AND rd.chembl_id IN ({placeholders})
        """,
        (f"%{disease_contains}%", *chembl_ids.values()),
    ).fetchall()

    # report_id -> {disease_id, disease_name, set of our chembl_ids present, any confirmed_in_aact}
    reports: dict[str, dict] = {}
    for r in rows:
        entry = reports.setdefault(r["report_id"], {"disease_id": r["disease_id"], "disease_name": r["disease_name"], "drugs": set(), "confirmed": False})
        entry["drugs"].add(r["chembl_id"])
        entry["confirmed"] = entry["confirmed"] or bool(r["confirmed_in_aact"])

    target_set = set(chembl_ids.values())
    per_disease: dict[str, dict] = {}
    for report_id, info in reports.items():
        d = per_disease.setdefault(info["disease_name"], {"combo": 0, "combo_confirmed": 0, **{n: 0 for n in drug_names}, "total_reports_touching_any": 0})
        d["total_reports_touching_any"] += 1
        # Subset match, not exact-set match: a report naming venetoclax +
        # azacitidine + a third agent still counts as the combo being
        # present (real triplet-extension trials exist for this regimen).
        # Only reports where the backbone combo is genuinely absent fall
        # into the individual "present without the combo" buckets below.
        if target_set.issubset(info["drugs"]):
            d["combo"] += 1
            if info["confirmed"]:
                d["combo_confirmed"] += 1
        else:
            for name, cid in chembl_ids.items():
                if cid in info["drugs"]:
                    d[name] += 1

    print(f"\n=== Prevalence of [{combo_label}] vs. each drug without that combo, by disease subgroup ===")
    print(f"(disease name contains '{disease_contains}'; counts are distinct clinical-trial evidence reports)")
    print(f"('{combo_label}' columns = all named drugs present, possibly plus others; per-drug columns = that drug present but NOT the full combo -- includes true monotherapy AND other combinations with different partner drugs, not monotherapy alone)\n")

    ordered = sorted(per_disease.items(), key=lambda kv: kv[1]["total_reports_touching_any"], reverse=True)
    total_combo = sum(d["combo"] for _, d in ordered)
    total_any = sum(d["total_reports_touching_any"] for _, d in ordered)

    header = f"{'disease subgroup':55} {'combo':>7} {'(AACT-conf)':>11}"
    for name in drug_names:
        header += f" {(name + ' (no combo)')[:18]:>18}"
    header += f" {'total (any)':>12}"
    print(header)
    print("-" * len(header))
    for disease_name, d in ordered:
        line = f"{disease_name[:55]:55} {d['combo']:>7} {d['combo_confirmed']:>11}"
        for name in drug_names:
            line += f" {d[name]:>18}"
        line += f" {d['total_reports_touching_any']:>12}"
        print(line)

    print("-" * len(header))
    print(f"{'TOTAL':55} {total_combo:>7} {'':>11}" + "".join(f" {'':>18}" for _ in drug_names) + f" {total_any:>12}")
    rate = total_combo / total_any if total_any else 0
    print(f"\nAcross all matching subgroups: {combo_label} combination evidence appears in {total_combo}/{total_any} reports ({rate:.1%})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("drugs", nargs="+", help="Drug names to check in combination (e.g. venetoclax azacitidine)")
    parser.add_argument("--disease-contains", required=True, help="Substring filter on disease name (e.g. 'myeloid leukemia')")
    args = parser.parse_args()
    main(args.drugs, args.disease_contains)
