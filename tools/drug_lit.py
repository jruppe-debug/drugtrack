"""
Drug + indication literature support, independent of individual trials.

Each Open Targets indication (drug, disease) gets an "academic article" branch next to its NCT branch.
Articles are tagged by NLM MeSH indexing, not by trial: a PubMed record supports the pair when NLM
indexers assigned it both the drug's MeSH heading and the disease's MeSH heading.

Disease -> MeSH heading, in order of trust (the tier is stored with every pair):
  1. xref    - MESH:D###### cross-reference on the Open Targets disease record (exact ID join).
  2. entry   - the disease name, or an Open Targets exact synonym, equals a MeSH heading or entry term, case-insensitive (exact string
               match against the controlled vocabulary, never fuzzy).
  3. none    - no MeSH heading; the pair keeps only its NCT branch.
Drug -> MeSH heading uses the entry-term rule (e.g. imatinib -> "Imatinib Mesylate").

Treatment support uses MeSH subheadings: drug/therapeutic use AND disease/drug therapy. Per pair it
records PubMed counts (any co-indexing, treatment, adverse effects, [Majr] focus, clinical trial, RCT, review, case report), the
newest clinical-trial articles, and a corroboration check: how many PMIDs linked to the pair's NCT IDs
(PubMed [si]) fall inside the MeSH set. NO LLM.

Usage (from DrugTrack/):  ../.venv/Scripts/python tools/drug_lit.py [drug]
Output: data/drug_lit_<drug>.json
"""
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DRUGTRACK = os.path.dirname(HERE)
REPO = os.path.dirname(DRUGTRACK)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
OT_GRAPHQL = "https://api.platform.opentargets.org/api/v4/graphql"
NCBI_PAUSE_S = 0.4  # NCBI allows 3 requests/s without an API key
PUB_TYPES = {
    "clinical_trial": "Clinical Trial[pt]",
    "rct": "Randomized Controlled Trial[pt]",
    "review": "Review[pt]",
    "case_report": "Case Reports[pt]",
}
TOP_N = 5
COMBO_H = "Antineoplastic Combined Chemotherapy Protocols"


def eutils(tool, **params):
    """POST so long NCT OR-lists fit."""
    params.update(retmode="json", tool="clinical-tracker")
    data = urllib.parse.urlencode(params).encode()
    for attempt in range(4):
        try:
            with urllib.request.urlopen(EUTILS + tool + ".fcgi", data=data, timeout=60) as r:
                out = json.load(r)
            time.sleep(NCBI_PAUSE_S)
            return out
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503) or attempt == 3:
                raise
            time.sleep(2 ** attempt * 2)


def count(term):
    return int(eutils("esearch", db="pubmed", term=term, retmax=0)["esearchresult"]["count"])


def mesh_by_ui(ui):
    """MeSH descriptor ID (D015464) -> [preferred heading, *entry terms]."""
    ids = eutils("esearch", db="mesh", term=f"{ui}[mui]")["esearchresult"]["idlist"]
    if not ids:
        return None
    return eutils("esummary", db="mesh", id=ids[0])["result"][ids[0]]["ds_meshterms"]


def mesh_by_entry_term(name):
    """Exact (case-insensitive) match of name against a descriptor's heading or entry terms."""
    ids = eutils("esearch", db="mesh", term=f'"{name}"', retmax=20)["esearchresult"]["idlist"]
    if not ids:
        return None
    res = eutils("esummary", db="mesh", id=",".join(ids))["result"]
    for uid in res.get("uids", []):
        rec = res[uid]
        if not rec.get("ds_meshui", "").startswith("D"):  # descriptors only, not supplementary concepts
            continue
        if name.lower() in (t.lower() for t in rec["ds_meshterms"]):
            return rec["ds_meshterms"][0], rec["ds_meshui"]
    return None


def ot_diseases(disease_ids):
    """disease_id -> (MeSH descriptor xrefs, exact synonyms) from Open Targets."""
    q = "query($ids:[String!]!){diseases(efoIds:$ids){id dbXRefs synonyms{relation terms}}}"
    req = urllib.request.Request(OT_GRAPHQL, data=json.dumps({"query": q, "variables": {"ids": disease_ids}}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        rows = json.load(r)["data"]["diseases"]
    return {d["id"]: ([x.split(":", 1)[1] for x in d["dbXRefs"] if x.upper().startswith("MESH:D")],
                      [t for s in d["synonyms"] if s["relation"] == "hasExactSynonym" for t in s["terms"]])
            for d in rows}


def map_disease(name, uis, synonyms):
    """Disease -> MeSH heading. With several xrefs, prefer the one whose entry terms name the disease."""
    names = {n.lower() for n in [name, *synonyms]}
    found = [(ui, terms) for ui in uis if (terms := mesh_by_ui(ui))]
    for ui, terms in found:
        if names & {t.lower() for t in terms}:
            return {"tier": "xref", "ui": ui, "heading": terms[0]}
    if found:
        return {"tier": "xref", "ui": found[0][0], "heading": found[0][1][0]}
    for n in [name, *synonyms]:
        if (hit := mesh_by_entry_term(n)):
            return {"tier": "entry", "ui": hit[1], "heading": hit[0], "matched": n}
    return {"tier": "none"}


def indications(db_path, drug):
    """(disease_id, name, max stage, approved, NCT IDs) for every Open Targets indication of the drug."""
    con = sqlite3.connect(db_path)
    rows = con.execute(
        """SELECT i.disease_id, ds.name, i.max_clinical_stage, max(i.is_approved),
                  group_concat(DISTINCT upper(cr.nct_id))
           FROM drugs d JOIN indications i ON i.chembl_id = d.chembl_id
           JOIN diseases ds ON ds.disease_id = i.disease_id
           LEFT JOIN indication_reports ir ON ir.indication_id = i.indication_id
           LEFT JOIN clinical_reports cr ON cr.report_id = ir.report_id AND cr.confirmed_in_aact = 1
           WHERE lower(d.name) = ? GROUP BY i.disease_id""", (drug.lower(),)).fetchall()
    return [{"disease_id": d, "disease": n, "max_stage": s, "approved": bool(a),
             "ncts": sorted(set((nc or "").split(",")) - {""})} for d, n, s, a, nc in rows]


def pair_support(drug_h, dis_h, ncts):
    # Subheadings split efficacy from safety. Treatment = disease/drug therapy AND the drug as /therapeutic use,
    # /administration & dosage, or inside a combined-chemotherapy protocol (how NLM indexes combination
    # regimens). drug/adverse effects = the disease shows up as a side effect (e.g. imatinib-induced pneumonitis).
    any_ = f'"{drug_h}"[Mesh] AND "{dis_h}"[Mesh]'
    combo = f'"{drug_h}"[Mesh] AND "{COMBO_H}/therapeutic use"[Mesh] AND "{dis_h}/drug therapy"[Mesh]'
    base = (f'("{drug_h}/therapeutic use"[Mesh] OR "{drug_h}/administration & dosage"[Mesh] OR ({combo})) '
            f'AND "{dis_h}/drug therapy"[Mesh]')
    out = {"query": base, "any_count": count(any_), "count": count(base), "combo_count": count(combo),
           "adverse_count": count(f'"{drug_h}/adverse effects"[Mesh] AND "{dis_h}"[Mesh]'),
           "focus_count": count(f'"{drug_h}/therapeutic use"[Majr] AND "{dis_h}/drug therapy"[Majr]')}
    out["by_type"] = {k: count(f"({base}) AND {v}") for k, v in PUB_TYPES.items()} if out["count"] else {}
    out["top_trials"] = []
    if out["by_type"].get("clinical_trial"):
        ids = eutils("esearch", db="pubmed", term=f"({base}) AND Clinical Trial[pt]", retmax=TOP_N,
                     sort="pub_date")["esearchresult"]["idlist"]
        res = eutils("esummary", db="pubmed", id=",".join(ids))["result"]
        out["top_trials"] = [{"pmid": p, "title": res[p]["title"], "year": res[p]["pubdate"][:4]} for p in ids]
    # Corroboration: articles PubMed links to this pair's trials, and how many of them the MeSH set contains.
    if ncts:
        si = " OR ".join(f"{n}[si]" for n in ncts)
        out["nct_linked"] = count(si)
        out["nct_linked_with_drug"] = count(f'({si}) AND "{drug_h}"[Mesh]') if out["nct_linked"] else 0
        out["nct_linked_in_mesh"] = count(f"({si}) AND ({base})") if out["nct_linked_with_drug"] else 0
    return out


def main(drug="imatinib"):
    pairs = indications(os.path.join(REPO, "output", "clinical_tracker.db"), drug)
    hit = mesh_by_entry_term(drug)
    if not hit:
        sys.exit(f"No MeSH heading for drug {drug!r}")
    drug_h, drug_ui = hit
    print(f"{drug} -> {drug_h} ({drug_ui}); {len(pairs)} indications")

    ot = ot_diseases([p["disease_id"] for p in pairs])
    for i, p in enumerate(pairs, 1):
        p["mesh"] = map_disease(p["disease"], *ot.get(p["disease_id"], ([], [])))
        if p["mesh"]["tier"] != "none":
            p["articles"] = pair_support(drug_h, p["mesh"]["heading"], p["ncts"])
        print(f"[{i}/{len(pairs)}] {p['disease']} -> {p['mesh'].get('heading', '-')} "
              f"({p['mesh']['tier']}) {p.get('articles', {}).get('count', '')}")

    mapped = [p for p in pairs if p["mesh"]["tier"] != "none"]
    corrob = [p for p in mapped if p["articles"].get("nct_linked")]
    summary = {
        "drug": drug, "drug_mesh": {"ui": drug_ui, "heading": drug_h},
        "indications": len(pairs),
        "mesh_tier": {t: sum(p["mesh"]["tier"] == t for p in pairs) for t in ("xref", "entry", "none")},
        "with_articles": sum(p["articles"]["count"] > 0 for p in mapped),
        "with_trial_articles": sum(p["articles"]["by_type"].get("clinical_trial", 0) > 0 for p in mapped),
        "nct_linked_articles": sum(p["articles"]["nct_linked"] for p in corrob),
        "nct_linked_with_drug": sum(p["articles"]["nct_linked_with_drug"] for p in corrob),
        "nct_linked_in_mesh": sum(p["articles"]["nct_linked_in_mesh"] for p in corrob),
    }
    out = os.path.join(DRUGTRACK, "data", f"drug_lit_{drug}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "pairs": pairs}, f, indent=1)
    print(json.dumps(summary, indent=1))
    print("wrote", out)


if __name__ == "__main__":
    main(*sys.argv[1:])
