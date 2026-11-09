"""Standardized disease config: build config/diseases/<name>.json from a MONDO id, recording where every value came from.
Stage seed   (ID-derived, before any label is read)
  scope_id                 given
  primary_mesh             MONDO dbXRefs MESH: of the scope node; else the first MONDO parent that has one
  label_title_extra_terms  MONDO exact synonyms that are acronyms (2-6 capitals / digits / hyphen), e.g. AML, ANLL, B-CLL
  everything else          config/defaults.json (generic oncology)
Stage derive (after tools/scope_labels.py has found the disease's US labels; reads section 1 + 2 at approval-time versions)
  phase_words              defaults master entries that occur in those labels
  settings                 defaults settings_master entries that occur in section 1
  class_words              defaults class_master entries that occur in sections 1-2 (each maps to ATC codes)
  eligibility              defaults eligibility_master phrases that occur in section 1
  biomarkers               HGNC approved symbols containing a digit (FLT3, IDH1, TP53, CD33) + defaults biomarker_patterns
                           (del(17p), IGHV, t(9;22)) found in section 1
  intensive_regimen        NOT derived: a disease-specific clinical rule. Copied from an existing file if present, else null
                           and listed under curated_required.
Every derived list keeps its label hit counts under _provenance, so a reviewer sees why each word is there.
Run:  python tools/make_disease_config.py --name cll --mondo MONDO_0004948 --stage seed
      python tools/scope_labels.py --disease cll
      python tools/make_disease_config.py --name cll --stage derive
      (--out <file> writes elsewhere, e.g. to regenerate AML next to the hand-written file for a diff)
"""
import argparse, json, os, re, sys, urllib.request
from collections import Counter
import xml.etree.ElementTree as ET
import pyarrow.parquet as pq
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disease_config
from indication_records import sections

BRIDGE = os.environ.get("AACT_BRIDGE_DIR", r"C:\Users\jwrup\Clinical Tracker\AACT_bridge")
OT = os.path.join(BRIDGE, "inputs", "opentargets")
HGNC_URL = "https://storage.googleapis.com/public-download-files/hgnc/json/json/hgnc_complete_set.json"
HGNC = os.path.join(BRIDGE, "inputs", "hgnc", "hgnc_complete_set.json")
VCACHE = os.path.join(disease_config.DATA, "dailymed", "versions")
ACRONYM = re.compile(r"^[A-Z][A-Z0-9-]{1,5}$")


def hgnc_symbols():
    if not os.path.exists(HGNC):
        os.makedirs(os.path.dirname(HGNC), exist_ok=True)
        req = urllib.request.Request(HGNC_URL, headers={"User-Agent": "clinical-tracker"})
        open(HGNC, "wb").write(urllib.request.urlopen(req, timeout=300).read())
    d = json.load(open(HGNC, encoding="utf-8"))["response"]["docs"]
    return {x["symbol"] for x in d if x.get("status") == "Approved" and re.search(r"\d", x["symbol"]) and len(x["symbol"]) >= 3}


def seed(name, mondo):
    rows = {r["id"]: r for r in pq.read_table(os.path.join(OT, "disease.parquet"),
                                              columns=["id", "name", "parents", "dbXRefs", "exactSynonyms"]).to_pylist()}
    d = rows[mondo]
    mesh = [x.split(":", 1)[1] for x in d["dbXRefs"] or [] if x.startswith("MESH:")]
    mesh_from = "MONDO dbXRefs"
    if not mesh:
        for p in d["parents"] or []:
            m = [x.split(":", 1)[1] for x in rows.get(p, {}).get("dbXRefs") or [] if x.startswith("MESH:")]
            if m:
                mesh, mesh_from = m, f"parent {p} dbXRefs (scope node has no MeSH)"
                break
    acr = sorted({s for s in d["exactSynonyms"] or [] if ACRONYM.match(s)})
    return dict(disease=name.upper(), scope_id=mondo, scope_name=d["name"], primary_mesh=mesh[0] if mesh else None,
                label_title_extra_terms=acr,
                _provenance=dict(primary_mesh=mesh_from, label_title_extra_terms="MONDO exact synonyms matching " + ACRONYM.pattern,
                                 stage="seed"),
                curated_required=["eligibility.intensive_regimen"])


def label_texts(name):
    sl = json.load(open(os.path.join(disease_config.data_dir(name), "scope_labels.json"), encoding="utf-8"))
    out = []
    for k, v in sl["labels"].items():
        from common import sections_for
        S = sections_for(os.path.join(VCACHE, f"{v['setid']}_v{v['version']}.xml"))
        keep = {g.split(" ", 1)[0] for g in v["section1"]}          # only the subsections that passed the scope gate
        s1 = " ".join(x for n, t, x in S.get("1", {}).get("subs", []) if n in keep) or S.get("1", {}).get("text", "")
        s2 = S.get("2", {}).get("text", "")
        out.append((k, s1, s2))
    return out


def count(pattern, texts):
    rx = re.compile(pattern, re.I)
    return sum(1 for t in texts if rx.search(t))


def derive(name, cfg_path):
    cur = json.load(open(cfg_path, encoding="utf-8"))
    base = json.load(open(disease_config.DEFAULTS, encoding="utf-8"))
    L = label_texts(name)
    s1 = [a for _, a, _ in L]
    s12 = [a + " " + b for _, a, b in L]
    prov = cur.setdefault("_provenance", {})
    prov["stage"] = "derive"
    prov["labels_read"] = len(L)
    pw = {w: count(rf"\b{re.escape(w)}\b", s12) for w in base["phase_words"]}
    cur["phase_words"] = [w for w, n in pw.items() if n]
    prov["phase_words"] = {w: n for w, n in pw.items() if n}
    st = {k: count(v, s1) for k, v in base["settings_master"].items()}
    cur["settings"] = {k: base["settings_master"][k] for k, n in st.items() if n}
    prov["settings"] = {k: n for k, n in st.items() if n}
    cl = {k: count(rf"(?<![A-Za-z]){re.escape(k)}(?![A-Za-z])", s1) for k in base["class_master"]}   # section 1 only: partner clauses
    cur["class_words"] = {k: base["class_master"][k] for k, n in cl.items() if n}
    prov["class_words"] = {k: n for k, n in cl.items() if n}
    el = {}
    for side in ("ineligible", "eligible"):
        hits = {p: count(p, s1) for p in base["eligibility_master"][side]}
        el[side] = [p for p, n in hits.items() if n]
        prov[f"eligibility.{side}"] = {p: n for p, n in hits.items() if n}
    el["intensive_regimen"] = (cur.get("eligibility") or {}).get("intensive_regimen")
    cur["eligibility"] = el
    syms = hgnc_symbols()
    bm = Counter()
    for t in s1:
        found = {m for m in re.findall(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9-]{2,9}(?![A-Za-z0-9])", t) if m in syms}
        for p in base["biomarker_patterns"]:
            found |= set(re.findall(p, t))
        bm.update(found)
    cur["biomarkers"] = sorted(bm)
    prov["biomarkers"] = dict(bm.most_common())
    prov["biomarkers_rule"] = "HGNC approved symbols with a digit, plus defaults biomarker_patterns, found in section 1"
    # MONDO descendants named in section 1 (e.g. APL under AML): listed for a curator to decide whether each is its own disease
    rows = {r["id"]: r for r in pq.read_table(os.path.join(OT, "disease.parquet"), columns=["id", "name", "descendants", "exactSynonyms"]).to_pylist()}
    sub = Counter()
    for dsc in rows[cur["scope_id"]]["descendants"] or []:
        r = rows.get(dsc)
        if not r:
            continue
        terms = [r["name"]] + [s for s in r["exactSynonyms"] or [] if ACRONYM.match(s)]
        for t in terms:
            n = count(r"(?<![A-Za-z])" + re.escape(t) + r"(?![A-Za-z])", s1)
            if n:
                sub[f"{r['name']} ({dsc}) via '{t}'"] += n
    cur.setdefault("excluded_subtypes", {})
    prov["review_subtypes"] = dict(sub.most_common())
    cur["curated_required"] = ([] if el["intensive_regimen"] else ["eligibility.intensive_regimen"]) + \
                              (["excluded_subtypes (see _provenance.review_subtypes)"] if sub and not cur["excluded_subtypes"] else [])
    return cur


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--mondo")
    ap.add_argument("--stage", choices=["seed", "derive"], required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    path = a.out or os.path.join(disease_config.CFG_DIR, f"{a.name.lower()}.json")
    if a.stage == "seed":
        c = seed(a.name, a.mondo)
        if os.path.exists(path):                      # keep curated keys of an existing file
            old = json.load(open(path, encoding="utf-8"))
            if (old.get("eligibility") or {}).get("intensive_regimen"):
                c["eligibility"] = {"intensive_regimen": old["eligibility"]["intensive_regimen"]}
                c["curated_required"] = []
    else:
        c = derive(a.name, path)
    json.dump(c, open(path, "w", encoding="utf-8"), indent=1)
    print(f"{a.stage}: {path}")
    print(json.dumps({k: v for k, v in c.items() if not k.startswith("_")}, indent=1)[:2500])


if __name__ == "__main__":
    main()
