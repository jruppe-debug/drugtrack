"""Compact per-disease view of the generic pipeline for the viewer: data/diseases/<disease>/paradigm.json (a few hundred KB,
instead of the 50 MB tree.json). One entry per approved drug: approval events (date, application, setting, biomarker, chemo
eligibility, regimen per phase, lead + supporting trials, link level, approval letter), lineage (first-in-human, precursors,
phase 3 not yet a basis), readouts (primary completion, PubMed primary article). Plus the excluded / needs-review records
with their reasons and the NCI coverage table. Reads only the pipeline outputs; changes nothing.
Run:  python tools/export_paradigm.py --disease cll
"""
import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disease_config
from common import load_outputs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--disease", required=True)
    a = ap.parse_args()
    cfg = disease_config.load(a.disease)
    R, V, L = load_outputs(a.disease, "indication_records.json", "regimen_verification.json", "development_lineage.json")
    rec = {r["record_id"]: r for r in R["records"]}
    ver = {r["record_id"]: r for r in V["results"]}
    drugs = []
    for name, x in sorted(L["drugs"].items()):
        evs = []
        for e in x["events"]:
            r, v = rec.get(e["record_id"], {}), ver.get(e["record_id"], {})
            phases = {ph: [[p.get("name") for p in alt] for alt in d["alternatives"]] or [["monotherapy"]]
                      for ph, d in (r.get("phases") or {}).items()}
            evs.append(dict(date=e["date"], application=e["application"], submission=e["submission"], kind=e["kind"],
                            link=e["link_level"], setting=e["setting"], biomarker=e["biomarker"],
                            eligibility=(r.get("indication") or {}).get("chemo_eligibility", {}).get("value"),
                            indication=((r.get("indication") or {}).get("text") or "")[:400], phases=phases,
                            regimen_keys=e["regimen_keys"], lead=e["lead"], lead_rule=e["lead_rule"], supporting=e["supporting"][:6],
                            named_in_documents_only=e.get("named_in_documents_not_label", []), letter=e["letter"],
                            label=(r.get("label") or {}).get("setid"), label_version=(r.get("label") or {}).get("version")))
        trials = {t["nct"]: t for t in x["trials"]}
        pick = lambda role: [dict(nct=t["nct"], phase=t["phase"], start=t["start"], regimen=t["regimen"], title=t["title"][:90])
                             for t in x["trials"] if t["role"] == role]
        drugs.append(dict(drug=name, first_in_human=x["first_in_human"]["first"], fih_route=x["first_in_human"]["route"],
                          events=evs, readouts=x["readouts"], precursors=pick("precursor")[:12],
                          phase3_not_yet_basis=pick("phase3_not_yet_basis")[:12], role_counts=x["counts"],
                          lead_trials={n: dict(phase=trials[n]["phase"], start=trials[n]["start"], acronym=trials[n]["acronym"],
                                               enrollment=trials[n]["enrollment"], title=trials[n]["title"][:100])
                                       for n in {e["lead"] for e in x["events"] if e["lead"]} if n in trials}))
    other = [dict(record=k, status=v["status"], reason=v.get("excluded_reason") or ", ".join(v["failed"]))
             for k, v in sorted(ver.items()) if not v["status"].startswith("verified")]
    out = dict(disease=a.disease, scope=dict(mondo=cfg["scope_id"], mesh=cfg.get("primary_mesh")), drugs=drugs,
               not_verified=other, nci_coverage=L.get("nci_coverage", {}),
               counts=dict(records=len(rec), verified=sum(v["status"].startswith("verified") for v in ver.values()),
                           drugs=len(drugs)))
    p = os.path.join(disease_config.data_dir(a.disease), "paradigm.json")
    json.dump(out, open(p, "w", encoding="utf-8"), separators=(",", ":"))
    print(f"{len(drugs)} drugs, {out['counts']} -> {p} ({os.path.getsize(p) // 1024} KB)")


if __name__ == "__main__":
    main()
