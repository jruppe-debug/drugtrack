"""
Sample literature match for one drug's trials: do academic articles complement the NCT findings?

Same principle as the AACT index-value test in the root README (cross_reference.py): join on an exact
identifier, never on free text. Each trial's NCT ID is looked up in two independent registries of
trial -> article links, and the index value is how many trials resolve to at least one article:

  1. AACT study_references  - PMIDs the trial sponsor registered on ClinicalTrials.gov (RESULT / BACKGROUND / DERIVED).
  2. PubMed [si] databank   - PMIDs whose PubMed record NLM indexed with that NCT ID as a secondary source ID.

A PMID found by both is corroborated by two independent linkers. NO LLM: identifier joins plus one
token check (does the article title name the drug) for display only.

Trials: the drug's AACT-confirmed NCT IDs in output/clinical_tracker.db (the Open Targets -> AACT join).

Usage (from DrugTrack/):  ../.venv/Scripts/python tools/lit_match.py [drug]
Output: data/lit_<drug>.json, served by /api/trial-lit and shown on the main viewer.
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DRUGTRACK = os.path.dirname(HERE)
REPO = os.path.dirname(DRUGTRACK)
sys.path.insert(0, REPO)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(REPO, ".env"))
from sources.aact.client import AACTClient  # noqa: E402

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
NCBI_PAUSE_S = 0.4  # NCBI allows 3 requests/s without an API key
# Names the article title may use for the drug; display flag only, never used to create a match.
DRUG_ALIASES = {"imatinib": ["imatinib", "gleevec", "glivec", "sti571", "sti 571", "cgp 57148"]}


def eutils(tool, **params):
    params.update(retmode="json", tool="clinical-tracker")
    url = EUTILS + tool + ".fcgi?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.load(r)
    time.sleep(NCBI_PAUSE_S)
    return data


def tracker_ncts(db_path, drug):
    """The drug's AACT-confirmed NCT IDs from the Open Targets -> AACT join (build_truth_table.py output),
    each with the Open Targets indications that cite it. NCT -> {"indications": [...], "approved": bool}."""
    con = sqlite3.connect(db_path)
    rows = con.execute(
        """SELECT DISTINCT cr.nct_id, ds.name, i.is_approved FROM drugs d
           JOIN indications i ON i.chembl_id = d.chembl_id
           JOIN diseases ds ON ds.disease_id = i.disease_id
           JOIN indication_reports ir ON ir.indication_id = i.indication_id
           JOIN clinical_reports cr ON cr.report_id = ir.report_id
           WHERE lower(d.name) = ? AND cr.nct_id IS NOT NULL AND cr.confirmed_in_aact = 1""", (drug.lower(),)).fetchall()
    ncts = {}
    for nct, disease, approved in rows:
        t = ncts.setdefault(nct.upper(), {"indications": set(), "approved": False})
        t["indications"].add(disease)
        t["approved"] |= bool(approved)
    return {n: {"indications": sorted(t["indications"]), "approved": t["approved"]} for n, t in ncts.items()}


def aact_refs(ncts):
    rows = AACTClient().run_query(
        "SELECT nct_id, pmid, reference_type FROM study_references WHERE nct_id = ANY(%s) AND pmid IS NOT NULL",
        (list(ncts),), limit=100000)
    out = {}
    for r in rows:
        out.setdefault(r["nct_id"], {})[str(r["pmid"]).strip()] = r["reference_type"]
    return out


def pubmed_si(ncts):
    out = {}
    for n in ncts:
        ids = eutils("esearch", db="pubmed", term=f"{n}[si]", retmax=500)["esearchresult"]["idlist"]
        if ids:
            out[n] = set(ids)
    return out


def summaries(pmids):
    meta = {}
    pmids = sorted(pmids)
    for i in range(0, len(pmids), 200):
        res = eutils("esummary", db="pubmed", id=",".join(pmids[i:i + 200]))["result"]
        for p in res.get("uids", []):
            s = res[p]
            meta[p] = {"title": s.get("title", ""), "journal": s.get("source", ""),
                       "year": (s.get("pubdate") or "")[:4], "pubtypes": s.get("pubtype", [])}
    return meta


def main(drug="imatinib"):
    ncts = tracker_ncts(os.path.join(REPO, "output", "clinical_tracker.db"), drug)
    if not ncts:
        sys.exit(f"No AACT-confirmed NCT IDs for {drug} in output/clinical_tracker.db")
    aact, pub = aact_refs(ncts), pubmed_si(ncts)
    all_pmids = {p for d in aact.values() for p in d} | {p for s in pub.values() for p in s}
    meta = summaries(all_pmids)
    alias = re.compile(r"\b(" + "|".join(re.escape(a) for a in DRUG_ALIASES.get(drug, [drug])) + r")\b", re.I)

    trials, n_both, n_aact_only, n_pub_only = {}, 0, 0, 0
    for n, info in sorted(ncts.items()):
        a, p = aact.get(n, {}), pub.get(n, set())
        arts = []
        for pmid in sorted(set(a) | p, key=lambda x: -int(x)):
            src = (["AACT " + a[pmid]] if pmid in a else []) + (["PubMed [si]"] if pmid in p else [])
            n_both += len(src) == 2
            n_aact_only += src == ["AACT " + a.get(pmid, "")] and pmid not in p
            n_pub_only += src == ["PubMed [si]"]
            m = meta.get(pmid, {})
            arts.append({"pmid": pmid, "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "sources": src,
                         "corroborated": len(src) == 2, "title_names_drug": bool(alias.search(m.get("title", ""))), **m})
        trials[n] = {**info, "articles": arts}

    # BACKGROUND refs cite prior work; RESULT/DERIVED (or a PubMed [si] tag) are articles about the trial itself.
    # Note: ClinicalTrials.gov builds DERIVED refs from PubMed [si], so the two linkers are not fully independent.
    for t in trials.values():
        for a in t["articles"]:
            a["reports_trial"] = a["sources"] != ["AACT BACKGROUND"]
        t["outcome_articles"] = sum(a["reports_trial"] for a in t["articles"])
    with_art = sum(1 for t in trials.values() if t["articles"])
    with_out = sum(1 for t in trials.values() if t["outcome_articles"])
    summary = {"drug": drug, "trials": len(trials), "trials_with_article": with_art,
               "index_value": f"{with_art}/{len(trials)}", "trials_with_outcome_article": with_out,
               "outcome_index_value": f"{with_out}/{len(trials)}", "distinct_pmids": len(all_pmids),
               "trial_article_links": n_both + n_aact_only + n_pub_only, "links_in_both": n_both,
               "links_aact_only": n_aact_only, "links_pubmed_only": n_pub_only,
               "pmids_resolved_in_pubmed": f"{len(meta)}/{len(all_pmids)}"}
    out = os.path.join(DRUGTRACK, "data", f"lit_{drug}.json")
    json.dump({"available": True, "summary": summary, "trials": trials}, open(out, "w", encoding="utf8"), indent=1)
    print(json.dumps(summary, indent=1))
    print("wrote", out)


if __name__ == "__main__":
    main(*sys.argv[1:2])
