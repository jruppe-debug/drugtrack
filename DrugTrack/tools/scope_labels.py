"""Scope labels: the disease's US-approved drug labels at the approval that added the disease, for any MONDO scope.
  1. Candidates (union, each tagged with its route):
       ot_id     Open Targets clinical_indication, maxClinicalStage = APPROVAL, disease = scope or MONDO descendant
                 -> ChEMBL -> UNII (DrugCentral crosswalk) -> openFDA label search openfda.unii
       label_txt openFDA label search indications_and_usage:"<name>" for the scope disease's ontology name + exact synonyms
                 (text route; every hit still has to pass the gate in step 2)
     Only NDA / BLA labels are kept (ANDA generics repeat the reference label).
  2. Gate: the CURRENT DailyMed SPL for the set id has a section 1 indication subsection naming the disease
     (ontology names + config label_title_extra_terms). Labels that fail are listed with the reason, not dropped silently.
  3. Approval-time version: the earliest SPL version whose section 1 already names the disease (binary search over the
     DailyMed version history; assumes an indication, once added, stays). Approval = the latest Drugs@FDA approval
     (ORIG or SUPPL, status AP) dated on or before that version's publish date.
Output: <data_dir>/scope_labels.json (same shape indication_records.py reads from label_evidence + central_anchor).
Run:  python tools/scope_labels.py --disease cll
"""
import argparse, json, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime
import xml.etree.ElementTree as ET
import pyarrow.parquet as pq
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disease_config
import fidelity as F
from dailymed_label import history, version_xml
from indication_records import sections
from common import sections_for
from regimen_key import Crosswalk

BRIDGE = os.environ.get("AACT_BRIDGE_DIR", os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "AACT_bridge")))
OT = os.path.join(BRIDGE, "inputs", "opentargets")
FDA = "https://api.fda.gov/drug"


def fda(path, params, cache):
    fn = os.path.join(cache, re.sub(r"[^A-Za-z0-9_.-]", "_", path + "_" + json.dumps(params, sort_keys=True))[:180] + ".json")
    if os.path.exists(fn):
        return json.load(open(fn, encoding="utf-8"))
    url = f"{FDA}/{path}.json?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "clinical-tracker"}), timeout=60) as r:
                d = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                d = {"results": []}
                break
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    json.dump(d, open(fn, "w", encoding="utf-8"))
    time.sleep(0.3)
    return d


def scope_names(scope_id):
    rows = pq.read_table(os.path.join(OT, "disease.parquet"), columns=["id", "name", "exactSynonyms"]).to_pylist()
    d = next(r for r in rows if r["id"] == scope_id)
    return [d["name"]] + [s for s in (d["exactSynonyms"] or []) if len(s) >= 8 and "," not in s and "(" not in s][:12]


DM_API = "https://dailymed.nlm.nih.gov/dailymed/services/v2"
DM_CATS = {"NDA": "C73594", "BLA": "C73585"}      # DailyMed marketing_category_code (ANDA generics excluded)
ANDA = "C73584"


def strip_paren(t):
    """Drop short parenthetical asides: 'chronic lymphatic (lymphocytic) leukemia' -> 'chronic lymphatic leukemia'."""
    return re.sub(r"\s*\([^()]{1,30}\)", "", t)


def dm_spls(rxcui, cat):
    cache = os.path.join(disease_config.DATA, "references", "dailymed_search")
    os.makedirs(cache, exist_ok=True)
    fn = os.path.join(cache, f"rxcui_{rxcui}_{cat}.json")
    if os.path.exists(fn):
        return json.load(open(fn, encoding="utf-8"))
    out, page = [], 1
    while True:
        url = f"{DM_API}/spls.json?rxcui={rxcui}&marketing_category_code={cat}&page={page}"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "clinical-tracker"}), timeout=60) as r:
            d = json.load(r)
        out += d.get("data", [])
        if page >= int(d["metadata"].get("total_pages") or 0):
            break
        page += 1
    json.dump(out, open(fn, "w", encoding="utf-8"))
    time.sleep(0.2)
    return out


def spl_application(xml):
    """Every application number in the SPL product data (approval ids, e.g. NDA205552 + NDA210563 for Imbruvica)."""
    out = []
    for e in ET.parse(xml).getroot().iter("{urn:hl7-org:v3}approval"):
        i = e.find("{urn:hl7-org:v3}id")
        if i is not None and i.get("extension") and i.get("extension") not in out:
            out.append(i.get("extension"))
    return out


def nci_drugs(cfg):
    """Generic names from the NCI 'Drugs Approved for <disease>' list: brand entries 'Brand (Generic)' give the generic."""
    url, head = cfg.get("nci_drug_page"), cfg.get("nci_section")
    if not url or not head:
        return []
    import html as H
    cache = os.path.join(disease_config.DATA, "references", "nci")
    os.makedirs(cache, exist_ok=True)
    fn = os.path.join(cache, re.sub(r"[^A-Za-z0-9]", "_", url)[-80:] + ".html")
    if not os.path.exists(fn):
        req = urllib.request.Request(url, headers={"User-Agent": "clinical-tracker"})
        open(fn, "wb").write(urllib.request.urlopen(req, timeout=60).read())
    t = open(fn, encoding="utf-8", errors="replace").read()
    parts = re.split(r"<h[23][^>]*>(.*?)</h[23]>", t, flags=re.S)
    out = set()
    for i in range(1, len(parts), 2):
        if H.unescape(re.sub("<[^>]+>", "", parts[i])).strip().lower() != f"drugs approved for {head}".lower():
            continue
        for m in re.findall(r'<a [^>]*href="/about-cancer/treatment/drugs/[^"]+"[^>]*>(.*?)</a>', parts[i + 1], flags=re.S):
            n = H.unescape(re.sub("<[^>]+>", "", m)).replace("\xa0", " ").strip()
            g = re.search(r"\(([^()]+)\)\s*$", n)
            n = g.group(1) if g else n
            n = re.sub(r"\s+(?:hydrochloride|dihydrochloride|maleate monohydrate|maleate|phosphate|fumarate|citrate|mesylate|sulfate)$", "", n, flags=re.I)
            if len(n) >= 5:
                out.add(n.lower())
    return sorted(out)


def label_signature(xml, scope):
    """(disease section 1 text, section 14 NCT ids + capitalised trial names, gated subsection titles) of one label version."""
    S = sections_for(xml)                          # parsed once, kept in the store
    subs = S.get("1", {}).get("subs", [])
    g = [(n, t, x) for n, t, x in subs if scope.search(strip_paren(t))]
    if not g and "1" in S and scope.search(strip_paren(S["1"]["text"])):
        g = [("1", "", S["1"]["text"])]
    s1 = re.sub(r"\s+", " ", " ".join(x for _, _, x in g)).strip()
    s14 = S.get("14", {}).get("text", "")
    ids = tuple(sorted(set(re.findall(r"NCT\d{8}", s14))))
    return (s1, ids, [f"{n} {t}" for n, t, _ in g])


def label_gate(xml, scope):
    S = sections_for(xml)                          # parsed once, kept in the store
    subs = S.get("1", {}).get("subs", [])
    hits = [f"{n} {t}" for n, t, _ in subs if scope.search(strip_paren(t))]
    if not hits and "1" in S and not any(re.match(r"\d", n or "") for n, _, _ in subs) and scope.search(strip_paren(S["1"]["text"])):
        hits = ["1 (no subsections)"]
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--disease", required=True)
    a = ap.parse_args()
    cfg = disease_config.load(a.disease)
    out_dir = disease_config.data_dir(a.disease)
    cache = os.path.join(disease_config.DATA, "references", "openfda")
    os.makedirs(cache, exist_ok=True)
    xw = Crosswalk()
    scope_rx, _, _ = F.scope_term_regex(os.path.join(OT, "disease.parquet"), cfg["scope_id"])
    scope = re.compile(scope_rx + "".join(r"|(?<![A-Za-z])" + re.escape(t) + r"(?![A-Za-z])"
                                          for t in cfg.get("label_title_extra_terms", [])), re.I)
    # 1. candidates
    D = {r["id"]: r for r in pq.read_table(os.path.join(OT, "disease.parquet"), columns=["id", "descendants"]).to_pylist()}
    sc = {cfg["scope_id"], *(D[cfg["scope_id"]]["descendants"] or [])}
    ci = pq.read_table(os.path.join(OT, "clinical_indication.parquet"), columns=["drugId", "diseaseId", "maxClinicalStage"]).to_pylist()
    chembl = sorted({r["drugId"] for r in ci if r["diseaseId"] in sc and r["maxClinicalStage"] == "APPROVAL"})
    labels = {}

    def add(res, route):
        for r in res:
            of = r.get("openfda", {})
            apps = [x for x in of.get("application_number", []) if x.startswith(("NDA", "BLA"))]
            if not apps or not r.get("set_id"):
                continue
            e = labels.setdefault(r["set_id"], dict(setid=r["set_id"], application=apps[0], brand=(of.get("brand_name") or [None])[0],
                                                    generic=(of.get("generic_name") or [None])[0], unii=of.get("unii", []), routes=set()))
            e["routes"].add(route)

    names = {r["id"]: r["name"] for r in pq.read_table(os.path.join(OT, "drug_molecule.parquet"), columns=["id", "name"]).to_pylist()}
    for c in chembl:
        uniis = [u for s in xw.structs_for(c) for u in xw.structs[s].get("UNII", [])]
        for u in uniis:
            add(fda("label", {"search": f'openfda.unii:"{u}"', "limit": 100}, cache).get("results", []), "ot_id")
        if not uniis and names.get(c):          # DrugCentral has no UNII (new drugs): exact generic name = Open Targets name
            add(fda("label", {"search": f'openfda.generic_name.exact:"{names[c].upper()}"', "limit": 100}, cache).get("results", []),
                "ot_id_generic_name")
    for n in scope_names(cfg["scope_id"]):
        skip = 0
        while True:                              # every page (openFDA allows skip up to 25000)
            d = fda("label", {"search": f'indications_and_usage:"{n}"', "limit": 100, "skip": skip}, cache)
            add(d.get("results", []), "label_txt")
            total = d.get("meta", {}).get("results", {}).get("total", 0)
            skip += 100
            if len(d.get("results", [])) < 100 or skip >= min(total, 25000):
                break
    # ID route through DailyMed (openFDA's label index misses SPLs, e.g. Xospata): ChEMBL -> RxNorm ingredient
    # (DrugCentral UNII -> RxNav, else RxNav exact name of the Open Targets drug) -> DailyMed spls.json?rxcui, NDA / BLA only
    def add_rx(rx, route):
        for r in sorted(rx):
            for cat, code in DM_CATS.items():
                for sp in dm_spls(r, code):
                    e = labels.setdefault(sp["setid"], dict(setid=sp["setid"], application=None, brand=sp["title"].split(" (")[0].split(" [")[0],
                                                            generic=xw.in_name(r), unii=[], routes=set()))
                    e["routes"].add(route)
        if rx and not any(dm_spls(r, code) for r in rx for code in DM_CATS.values()):
            for r in sorted(rx):                    # no NDA / BLA label left (reference product discontinued): ANDA labels
                for sp in sorted(dm_spls(r, ANDA), key=lambda x: x.get("published_date", ""))[:3]:
                    e = labels.setdefault(sp["setid"], dict(setid=sp["setid"], application=None, brand=sp["title"].split(" [")[0],
                                                            generic=xw.in_name(r), unii=[], routes=set()))
                    e["routes"].add("anda_fallback_no_nda_bla_label")

    for c in chembl:
        rx = set()
        for s in xw.structs_for(c):
            for u in xw.structs[s].get("UNII", []):
                rx |= xw.rx_unii(u)
        if not rx and names.get(c):
            rx = {r for r in xw.rx_name(names[c]) if xw.in_name(r)}
        add_rx(rx, "ot_rxcui_dailymed")
    # NCI curated list of FDA-approved drugs for the disease (config nci_drug_page + section heading). Wider seed:
    # names -> RxNorm ingredient (RxNav) -> DailyMed; every hit still has to pass the section 1 gate.
    nci = nci_drugs(cfg)
    nci_map = {}
    for n in nci:
        rx = {r for r in xw.rx_name(n) if xw.in_name(r)}
        before = set(labels)
        add_rx(rx, "nci_list")
        nci_map[n] = dict(rxcui=sorted(rx), setids=sorted({sid for r in rx for code in list(DM_CATS.values()) + [ANDA] for sid in
                                                            [sp["setid"] for sp in dm_spls(r, code)] if sid in labels}),
                          new_setids=sorted(set(labels) - before))
    disease_config.log(a.disease, "scope_labels", f"seeds: {len(chembl)} Open Targets ChEMBL, {len(nci)} NCI names, {len(labels)} set ids")
    for setid, e in labels.items():                 # all application numbers from the SPL itself
        try:
            h = history(setid)
            apps = spl_application(version_xml(setid, h[-1]["version"]))
        except Exception:
            apps = []
        e["applications"] = sorted(set(apps) | ({e["application"]} if e["application"] else set()))
        e["application"] = e["application"] or (apps[0] if apps else None)
    labels = {k: v for k, v in labels.items() if any(a.startswith(("NDA", "BLA")) for a in v["applications"])
              or "anda_fallback_no_nda_bla_label" in v["routes"]}
    print(f"candidates: {len(chembl)} OT-approved ChEMBL ids, {len(labels)} NDA/BLA label set ids")
    # 2-3. gate + approval-time version
    kept, failed = {}, []
    import common                                   # cold runs: fetch histories + current versions 4 at a time first
    common.prefetch(lambda sid: version_xml(sid, history(sid)[-1]["version"]), sorted(labels), 4, "scope_labels", a.disease)
    for i_, (setid, e) in enumerate(sorted(labels.items()), 1):
        disease_config.log(a.disease, "scope_labels", f"gate {i_}/{len(labels)} {e['brand']} {e['application']} routes={sorted(e['routes'])}")
        try:
            h = history(setid)
        except Exception as ex:
            failed.append(dict(setid=setid, brand=e["brand"], reason=f"DailyMed history: {type(ex).__name__}"))
            continue
        cur = h[-1]
        g = label_gate(version_xml(setid, cur["version"]), scope)
        if not g:
            failed.append(dict(setid=setid, brand=e["brand"], routes=sorted(e["routes"]), reason="current section 1 names no scope disease"))
            continue
        lo, hi = 0, len(h) - 1                      # earliest version that already names the disease
        while lo < hi:
            mid = (lo + hi) // 2
            try:
                ok = bool(label_gate(version_xml(setid, h[mid]["version"]), scope))
            except Exception:
                ok = False
            lo, hi = (lo, mid) if ok else (mid + 1, hi)
        v = h[lo]
        first = lo == 0                             # indication already in the first DailyMed version: added before DailyMed
        import store                                # Drugs@FDA bulk tables (was one openFDA call per application)
        apps = [app for app in e["applications"] if store.drugsfda_submissions(app)]
        subs = [s for app in apps for s in store.drugsfda_submissions(app) if s.get("submission_status") == "AP"]
        when = v["published"].strftime("%Y%m%d")
        prior = sorted((s for s in subs if s.get("submission_status_date", "99999999") <= when),
                       key=lambda s: s["submission_status_date"])
        ap_ = prior[-1] if prior else None
        if not apps and e["application"] and e["application"].startswith("BLA"):
            # CBER biologics (cell / gene therapy, vaccines) are not in Drugs@FDA: the version publish date bounds the approval
            ap_ = dict(submission_type="CBER", submission_number=None, _app=e["application"], submission_status_date=when,
                       note="CBER product: no Drugs@FDA record; date = DailyMed version publish date (upper bound)")
        orig = next((s_ for s_ in sorted(subs, key=lambda s_: s_["submission_status_date"]) if s_.get("submission_type") == "ORIG"), None)
        name = (e["generic"] or e["brand"] or setid).upper()
        k = name if name not in kept else f"{name} [{e['brand']}]"
        kept[k] = dict(setid=setid, version=v["version"], published=str(v["published"]), current_version=cur["version"],
                       brand=e["brand"], application=e["application"], candidate_routes=sorted(e["routes"]), section1=g,
                       applications=e["applications"],
                       approval=dict(application=(ap_ or {}).get("_app", e["application"]), submission=(ap_ or {}).get("submission_type"),
                                     number=(ap_ or {}).get("submission_number"),
                                     date=datetime.strptime(ap_["submission_status_date"], "%Y%m%d").strftime("%Y-%m-%d") if ap_ else None,
                                     rule="latest Drugs@FDA AP submission dated on or before the version's publish date",
                                     in_first_version=first,
                                     original_date=datetime.strptime(orig["submission_status_date"], "%Y%m%d").strftime("%Y-%m-%d") if orig else None))
        # approval history: each later efficacy supplement -> first label version published on/after it; a new record
        # only when the disease part of the label changed (section 1 disease subsections or section 14 trial ids)
        hist, prev = [], label_signature(version_xml(setid, v["version"]), scope)
        effs = sorted({s["submission_status_date"]: s for s in subs if s.get("submission_class_code") == "EFFICACY"
                       and s["submission_status_date"] > (ap_ or {}).get("submission_status_date", "0")}.values(),
                      key=lambda s: s["submission_status_date"])
        for s in effs:
            d = datetime.strptime(s["submission_status_date"], "%Y%m%d").date()
            nxt = next((x for x in h if x["published"] >= d), None)
            if not nxt:
                continue
            try:
                sig = label_signature(version_xml(setid, nxt["version"]), scope)
            except Exception:
                continue
            words = set(re.findall(r"[a-z0-9-]+", sig[0].lower())) ^ set(re.findall(r"[a-z0-9-]+", prev[0].lower()))
            if sig[0] and (sig[1] != prev[1] or sig[2] != prev[2] or len(words) >= 8):
                hist.append(dict(version=nxt["version"], published=str(nxt["published"]), section1=sig[2],
                                 approval=dict(application=s["_app"], submission=s["submission_type"], number=s.get("submission_number"),
                                               date=d.strftime("%Y-%m-%d"), in_first_version=False,
                                               original_date=kept[k]["approval"]["original_date"],
                                               rule="efficacy supplement; label version = first published on/after it"),
                                 changed=[n for n, (x, y) in zip(("section1", "s14_ids"), zip(prev[:2], sig[:2])) if x != y]))
                prev = sig
        kept[k]["history"] = hist
        disease_config.log(a.disease, "scope_labels", f"  kept {k}: first v{v['version']} {kept[k]['approval']['date']}, "
                                                      f"+{len(hist)} history events {[x['approval']['date'] for x in hist]}")
    json.dump(dict(disease=a.disease, scope_id=cfg["scope_id"], ot_approved_chembl=chembl, nci=nci_map, labels=kept, gate_failed=failed),
              open(os.path.join(out_dir, "scope_labels.json"), "w", encoding="utf-8"), indent=1)
    print(f"kept {len(kept)} labels, gate failed {len(failed)} -> {os.path.join(out_dir, 'scope_labels.json')}")
    for k, v in sorted(kept.items()):
        print(f"  {k[:34]:34s} {v['application']:10s} v{v['version']:<3} {v['published']} {v['approval']['submission']}"
              f" {v['approval']['date']} {v['candidate_routes']} {v['section1'][:2]}")


if __name__ == "__main__":
    # stage fingerprint: skip when code, config, inputs and bulk stamps are unchanged and outputs exist (--force reruns)
    import argparse as _ap, os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import common, disease_config
    _p = _ap.ArgumentParser(add_help=False)
    _p.add_argument("--disease", default="aml")
    _p.add_argument("--force", action="store_true")
    _p.add_argument("--trace")
    a, _rest = _p.parse_known_args()
    if "--force" in _sys.argv:
        _sys.argv.remove("--force")
    R = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    B = common.BRIDGE
    D = disease_config.data_dir(a.disease)
    os = _os
    INP = [os.path.join(B, "inputs", "opentargets", f) for f in ("disease.parquet", "clinical_indication.parquet", "drug_molecule.parquet")] + [os.path.join(R, "data", "id_crosswalk.json.gz")]
    OUT = [_os.path.join(D, x) for x in ["scope_labels.json"]]
    CODE = ["scope_labels.py", "indication_records.py", "regimen_key.py", "common.py", "disease_config.py", "dailymed_label.py", "fidelity.py"]
    if a.trace or "show" in _sys.argv or "pull" in _sys.argv or "--help" in _sys.argv:
        main()
    elif not common.stage_current(a.disease, "scope_labels", INP, OUT, CODE, a.force):
        main()
        common.stage_done(a.disease, "scope_labels", INP, OUT, CODE)
