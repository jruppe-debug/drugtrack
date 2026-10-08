"""Approval anchor pilot: tie a drug's central trial to the FDA approval package that names it.

For each pilot (drug, NCT) pair:
  1. Read every Drugs@FDA approval event (ORIG + efficacy SUPPL) from data/approval_cache.json (live drugsfda fetch for
     drugs missing from the cache; nothing is written back to that cache). Tag ORIG events 'original_indication' and
     efficacy supplements 'label_expansion'.
  2. Download each event's Label / Letter / Review PDF into data/references/fda/<application>/ and extract text (pypdf).
  3. Search the text for the trial's identifiers: NCT ID, org study ID, secondary IDs, acronym (ClinicalTrials.gov v2 API,
     raw JSON kept in data/references/ctgov/). Exact token match after removing spaces/hyphens - no fuzzy matching.
  4. Central rule: the pair is 'anchored' only if a label or review at an approval event names the trial. Otherwise
     'not_anchored' (or 'pre_registry_approval' when every approval predates the 2000 ClinicalTrials.gov launch).
  5. PubMed NCT[si] lookup (raw JSON in data/references/pubmed/). Primary article = earliest record typed
     Clinical Trial, Phase III or Randomized Controlled Trial, else earliest record. Tagged pre/post the anchoring approval.

Pilot only. Output: data/approval_anchor_pilot.json
"""
import gzip, json, os, re, sys, time, urllib.parse
from datetime import date

import requests
from pypdf import PdfReader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
REF = os.path.join(DATA, "references")
sys.path.insert(0, os.path.join(ROOT, "tools"))

PILOT = {
    "NCT02993523": ["VENETOCLAX", "AZACITIDINE"],
    "NCT00651261": ["MIDOSTAURIN"],
    "NCT00492856": ["TRETINOIN", "METHOTREXATE", "MERCAPTOPURINE"],
    "NCT06226571": ["DOCETAXEL", "LORNOXICAM", "CISPLATIN"],
    "NCT07604233": ["ASCIMINIB"],
    "NCT01471444": ["BUSULFAN", "CLOFARABINE", "FLUDARABINE", "METHOTREXATE", "TACROLIMUS"],
}
REGISTRY_START = "2000-02-29"   # ClinicalTrials.gov public launch
UA = {"User-Agent": "DrugTrack approval-anchor pilot (research)"}
MAX_PDF_BYTES = 80_000_000


def norm_id(s):
    return re.sub(r"[\s\-_/.]", "", s or "").upper()


def get(url, **kw):
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=60, **kw)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            return r
        except requests.RequestException as e:
            err = e
            time.sleep(2)
    raise err if "err" in locals() else RuntimeError("HTTP 429 x3")


def fix_url(u):
    """Drugs@FDA URLs carry raw spaces/commas ('208573Orig1s020, s021ltr.pdf'). Encode the path; force https."""
    p = urllib.parse.urlsplit(u.strip())
    return urllib.parse.urlunsplit(("https", p.netloc, urllib.parse.quote(urllib.parse.unquote(p.path)), p.query, ""))


# ---------------------------------------------------------------------------- Drugs@FDA events
def approval_records(drugs):
    cache = json.load(open(os.path.join(DATA, "approval_cache.json"), encoding="utf-8"))
    out, live = {}, None
    for d in drugs:
        rec = cache.get(d)
        source = "approval_cache.json"
        if not rec:
            if live is None:
                try:
                    from dotenv import load_dotenv
                    load_dotenv(os.path.join(os.path.dirname(ROOT), ".env"))
                    load_dotenv(os.path.join(ROOT, ".env"))
                except ImportError:
                    pass
                from export_disease_tree import ApprovalChecker
                live = ApprovalChecker(os.path.join(REF, "_unused_cache.json"), api_key=os.environ.get("OPENFDA_API_KEY"))
            rec = live.fetch(d)
            source = "openFDA drugsfda (live, not cached)"
        out[d] = dict(source=source, status=rec.get("status"), apps=rec.get("apps", []))
    return out


def events_for(apps):
    """One event per (application, submission type, approval date). ORIG = original indication; efficacy SUPPL = expansion."""
    ev = {}
    for ap in apps:
        for d in ap.get("docs", []):
            k = (ap["application"], d["sub"], d["date"])
            e = ev.setdefault(k, dict(application=ap["application"], brands=ap["brands"], sponsor=ap["sponsor"], submission=d["sub"],
                                      date=d["date"], kind="original_indication" if d["sub"] == "ORIG" else "label_expansion", docs={}))
            e["docs"].setdefault(d["type"], [])
            if d["url"] not in e["docs"][d["type"]]:
                e["docs"][d["type"]].append(d["url"])
        if ap.get("first_approval") and not any(k[0] == ap["application"] and k[1] == "ORIG" for k in ev):
            ev[(ap["application"], "ORIG", ap["first_approval"])] = dict(application=ap["application"], brands=ap["brands"], sponsor=ap["sponsor"],
                                                                          submission="ORIG", date=ap["first_approval"], kind="original_indication", docs={})
        for s in ap.get("efficacy_supplements", []):
            if not any(k[0] == ap["application"] and k[2] == s and k[1] != "ORIG" for k in ev):
                ev[(ap["application"], "SUPPL", s)] = dict(application=ap["application"], brands=ap["brands"], sponsor=ap["sponsor"],
                                                           submission="SUPPL", date=s, kind="label_expansion", docs={})
    return sorted(ev.values(), key=lambda e: (e["date"] or "", e["application"]))


# ---------------------------------------------------------------------------- documents
def fetch_doc(url, app):
    """Download once into references/fda/<app>/. Returns (local path, text, status). HTML review TOCs are saved as-is and
    their text is the tag-stripped page."""
    u = fix_url(url)
    name = urllib.parse.unquote(os.path.basename(urllib.parse.urlsplit(u).path)).replace(", ", "_").replace(" ", "_") or "index.html"
    d = os.path.join(REF, "fda", app)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    txt_path = path + ".txt"
    if os.path.exists(txt_path):
        return path, open(txt_path, encoding="utf-8").read(), "cached"
    try:
        r = get(u, stream=True)
        if r.status_code != 200:
            return None, "", f"HTTP {r.status_code}"
        if int(r.headers.get("Content-Length") or 0) > MAX_PDF_BYTES:
            return None, "", "skipped: over size cap"
        blob = r.content
    except Exception as e:
        return None, "", f"{type(e).__name__}"
    open(path, "wb").write(blob)
    if blob[:4] == b"%PDF":
        try:
            text = "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
        except Exception as e:
            text, status = "", f"pdf_parse_error {type(e).__name__}"
        else:
            status = "ok" if text.strip() else "no_text_layer"
    else:
        text = re.sub(r"<[^>]+>", " ", blob.decode("utf-8", "replace"))
        # accessdata sometimes serves a bot-challenge page in place of the review TOC; it carries no document links
        status = "ok_html" if b".pdf" in blob.lower() else "html_no_doc_links"
    open(txt_path, "w", encoding="utf-8").write(text)
    time.sleep(0.4)
    return path, text, status


def find_ids(text, ids):
    """Exact hits of each identifier in the document text (space/hyphen-insensitive, whole-token)."""
    flat = norm_id(text)
    hits = []
    for kind, val in ids:
        n = norm_id(val)
        if len(n) < 5:
            continue
        if kind in ("acronym", "alias"):
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(val) + r"(?![A-Za-z0-9])", text):
                hits.append(f"{kind}:{val}")
        elif n in flat:
            hits.append(f"{kind}:{val}")
    return hits


def review_pdfs(urls):
    """Review TOC pages link to fixed-name PDFs (<base>MedR.pdf = medical review, <base>SumR.pdf = summary review,
    <base>MultidisciplineR.pdf). Add those direct URLs so the review text is read even when the TOC page is unusable."""
    out = list(urls)
    for u in urls:
        m = re.match(r"(.+?)TOC\.html?$", u, re.I)
        if m:
            out += [m.group(1) + s for s in ("MedR.pdf", "SumR.pdf", "MultidisciplineR.pdf")]
    return out


# A trial named only as a postmarketing requirement verifies a later approval; it is not this approval's evidence.
PMR_RX = re.compile(r"\bPMR\b|postmarketing requirement|confirmatory (?:trial|study)|verif(?:y|ies) (?:and isolates )?the clinical", re.I)


def id_context(text, ids, width=400):
    """Text windows around every occurrence of the trial's identifiers (separator-insensitive)."""
    out = []
    for _, val in ids:
        n = norm_id(val)
        if len(n) < 5:
            continue
        rx = r"[\s\-_/.]*".join(map(re.escape, n))
        out += [text[max(0, m.start() - width): m.end() + width] for m in re.finditer(rx, text, re.I)]
    return out


def hit_role(text, ids):
    ctx = id_context(text, ids)
    return "confirmatory_pmr" if ctx and all(PMR_RX.search(c) for c in ctx) else "efficacy_basis"


def section14_mentions(text, nct):
    """Short snippet around the NCT ID or trial name in the label text, for provenance."""
    m = re.search(re.escape(nct), text) if nct else None
    if not m:
        return None
    s = text[max(0, m.start() - 300): m.end() + 300]
    return re.sub(r"\s+", " ", s)


# ---------------------------------------------------------------------------- registry + PubMed
def curated_aliases():
    """Human-entered trial aliases (sponsor protocol IDs, acronyms absent from the registry): data/curated/approval_lineage.json."""
    p = os.path.join(DATA, "curated", "approval_lineage.json")
    return json.load(open(p, encoding="utf-8")).get("aliases", {}) if os.path.exists(p) else {}


def ctgov(nct):
    d = os.path.join(REF, "ctgov")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, nct + ".json")
    if not os.path.exists(p):
        r = get(f"https://clinicaltrials.gov/api/v2/studies/{nct}")
        if r.status_code != 200:
            return {}
        open(p, "w", encoding="utf-8").write(r.text)
    j = json.load(open(p, encoding="utf-8"))
    idm = j.get("protocolSection", {}).get("identificationModule", {})
    ids = [("nct", nct)]
    if idm.get("orgStudyIdInfo", {}).get("id"):
        ids.append(("org_study_id", idm["orgStudyIdInfo"]["id"]))
    ids += [("secondary_id", s["id"]) for s in idm.get("secondaryIdInfos", []) if s.get("id")]
    if idm.get("acronym"):
        ids.append(("acronym", idm["acronym"]))
    ids += [("alias", a) for a in curated_aliases().get(nct, []) if a not in {v for _, v in ids}]
    return dict(ids=ids, acronym=idm.get("acronym"), sponsor=j.get("protocolSection", {}).get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name"))


def pubmed(nct):
    d = os.path.join(REF, "pubmed")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, nct + ".json")
    if not os.path.exists(p):
        es = get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params=dict(db="pubmed", term=f"{nct}[si]", retmode="json", retmax=200)).json()
        ids = es.get("esearchresult", {}).get("idlist", [])
        summ = {}
        if ids:
            time.sleep(0.4)
            summ = get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi", params=dict(db="pubmed", id=",".join(ids), retmode="json")).json()
        json.dump(dict(esearch=es, esummary=summ), open(p, "w", encoding="utf-8"), indent=1)
        time.sleep(0.4)
    j = json.load(open(p, encoding="utf-8"))
    res = j["esummary"].get("result", {}) if j.get("esummary") else {}
    arts = []
    for pmid in res.get("uids", []):
        r = res[pmid]
        sd = r.get("sortpubdate", "")[:10].replace("/", "-")
        arts.append(dict(pmid=pmid, date=sd, journal=r.get("source"), title=r.get("title"), pubtypes=r.get("pubtype", [])))
    arts.sort(key=lambda a: a["date"])
    pivotal = [a for a in arts if {"Clinical Trial, Phase III", "Randomized Controlled Trial"} & set(a["pubtypes"])]
    primary = (pivotal or arts or [None])[0]
    return dict(n=len(arts), primary=primary, primary_rule="earliest Phase III/RCT" if pivotal else ("earliest record" if arts else None), articles=arts)


# ---------------------------------------------------------------------------- population grouping (VIALE-A reference)
AML_RX = re.compile(r"acute myel\w* leuk|\bAML\b", re.I)
APL_ONLY_RX = re.compile(r"promyelocytic", re.I)
UNTREATED_RX = re.compile(r"previously[- ]untreated|treatment[- ]na[iï]ve|untreated|newly[- ]diagnosed|no prior (?:\w+ ){0,3}(?:therapy|treatment)", re.I)
UNFIT_WORD = r"(?:ineligible|not eligible|unfit|not (?:a )?candidates?|not suitable|unsuitable|inappropriate|unable to (?:receive|tolerate)|preclud\w*|not considered (?:fit|eligible|candidates?))"
INTENSIVE_WORD = r"(?:intensive|induction|cytarabine and (?:an )?anthracycline|7\s*\+\s*3)"
UNFIT_RX = re.compile(UNFIT_WORD + r".{0,80}?" + INTENSIVE_WORD + "|" + INTENSIVE_WORD + r".{0,40}?" + UNFIT_WORD, re.I | re.S)
# A trial that also enrols fit patients (separate intensive cohort / 7+3 arm) is a mixed population, flagged not dropped.
FIT_RX = re.compile(r"(?<![A-Za-z-])(?<!not )(?<!not considered )(?<!not a )(?:eligible|fit|candidates?|suitable) for (?:intensive|standard induction|7\s*\+\s*3)"
                    r"|\b7\s*\+\s*3\b|intensive (?:induction )?(?:chemotherapy )?cohort", re.I)
# Trials that also enrol relapsed/refractory patients mix lines of therapy; flagged not dropped.
RR_RX = re.compile(r"relapsed|refractory|\bR/R\b", re.I)
TRANSPLANT_WORD = r"(?:transplant\w*|HSCT|allogeneic|stem cell)"
TX_UNFIT_RX = re.compile(UNFIT_WORD + r"[^.*;]{0,40}?" + TRANSPLANT_WORD + "|" + TRANSPLANT_WORD + r"[^.*;]{0,20}?" + UNFIT_WORD, re.I)


def inclusion_text(t):
    c = t.get("criteria") or ""
    m = re.search(r"exclusion criteria", c, re.I)
    return c[: m.start()] if m else c


def population_group(ref_nct="NCT02993523"):
    """Deterministic rule from the VIALE-A inclusion criteria: AML (not APL-only) AND previously untreated AND ineligible for
    intensive induction. Matched on the AACT title + inclusion-criteria text only. Transplant ineligibility is a separate flag:
    VIALE-A itself does not state it."""
    aact = json.load(gzip.open(os.path.join(DATA, "aact_cache.json.gz"), "rt", encoding="utf-8"))["trials"]
    rows = []
    for nct, t in aact.items():
        head = " ".join([t.get("brief_title") or "", t.get("official_title") or "", " ".join(t.get("conditions") or [])])
        inc = inclusion_text(t)
        if not AML_RX.search(head) or (APL_ONLY_RX.search(head) and not re.search(r"\bAML\b|acute myeloid", head, re.I)):
            continue
        u = UNTREATED_RX.search(head) or UNTREATED_RX.search(inc)
        f = UNFIT_RX.search(head) or UNFIT_RX.search(inc)
        if not (u and f):
            continue
        tx = TX_UNFIT_RX.search(head) or TX_UNFIT_RX.search(inc)
        fit = FIT_RX.search(head + " " + inc)
        rows.append(dict(nct=nct, phase=t.get("phase"), status=t.get("overall_status"), enrollment=t.get("enrollment"), start=t.get("start_date"),
                         title=t.get("brief_title"), drugs=sorted({i["name"] for i in t.get("interventions", []) if i.get("intervention_type") in ("DRUG", "BIOLOGICAL")}),
                         untreated_evidence=re.sub(r"\s+", " ", u.group(0)), unfit_evidence=re.sub(r"\s+", " ", f.group(0))[:160],
                         mixed_line_rr=bool(RR_RX.search(head)),
                         mixed_fit_cohort=bool(fit), fit_evidence=re.sub(r"\s+", " ", fit.group(0))[:160] if fit else None,
                         transplant_ineligible_stated=bool(tx), transplant_evidence=re.sub(r"\s+", " ", tx.group(0))[:160] if tx else None))
    rows.sort(key=lambda r: (r["phase"] or "", r["start"] or ""), reverse=True)
    ref = aact.get(ref_nct, {})
    return dict(reference=ref_nct, rule="AML (not APL-only) AND previously-untreated term AND intensive-induction-ineligible term; AACT title + inclusion criteria",
                reference_inclusion_first_criterion=re.sub(r"\s+", " ", inclusion_text(ref).split("*")[1]).strip() if "*" in inclusion_text(ref) else None,
                reference_transplant_ineligible_stated=bool(TX_UNFIT_RX.search(inclusion_text(ref))),
                n=len(rows), n_transplant_ineligible_stated=sum(r["transplant_ineligible_stated"] for r in rows), trials=rows)


# ---------------------------------------------------------------------------- main
def run():
    drugs = sorted({d for v in PILOT.values() for d in v})
    recs = approval_records(drugs)
    trials = {nct: ctgov(nct) for nct in PILOT}
    pubs = {nct: pubmed(nct) for nct in PILOT}
    docs_seen = {}
    out = dict(generated=date.today().isoformat(), registry_start=REGISTRY_START, pairs=[], drugs={})
    for d in drugs:
        evs = events_for(recs[d]["apps"])
        for e in evs:
            e["doc_results"] = []
            for typ, urls in e["docs"].items():
                for u in (review_pdfs(urls) if typ == "Review" else urls):
                    if u not in docs_seen:
                        docs_seen[u] = fetch_doc(u, e["application"])
                    path, text, status = docs_seen[u]
                    e["doc_results"].append(dict(type=typ, url=u, local=os.path.relpath(path, os.path.dirname(ROOT)) if path else None,
                                                 status=status, chars=len(text), ncts=sorted(set(re.findall(r"NCT\d{8}", text)))))
        out["drugs"][d] = dict(source=recs[d]["source"], status=recs[d]["status"],
                               original_approval=next((dict(date=e["date"], application=e["application"], brands=e["brands"]) for e in evs if e["kind"] == "original_indication"), None),
                               events=[{k: v for k, v in e.items() if k != "docs"} for e in evs])
    for nct, ds in PILOT.items():
        t = trials[nct]
        for d in ds:
            evs = out["drugs"][d]["events"]
            hits = []
            for e in evs:
                for r in e["doc_results"]:
                    if r["type"] not in ("Label", "Review") or not r["chars"]:
                        continue
                    text = docs_seen[r["url"]][1]
                    ids = t.get("ids", [("nct", nct)])
                    h = find_ids(text, ids)
                    if h:
                        hits.append(dict(date=e["date"], application=e["application"], kind=e["kind"], doc=r["type"], local=r["local"], ids=h,
                                         role=hit_role(text, ids), snippet=section14_mentions(text, nct)))
            dates = [e["date"] for e in evs if e["date"]]
            basis = [h for h in hits if h["role"] == "efficacy_basis"]
            if basis:
                status = "anchored"
            elif hits:
                status = "confirmatory_pmr_only"
            elif not evs:
                status = "no_us_approval"
            elif dates and max(dates) < REGISTRY_START:
                status = "pre_registry_approval"
            else:
                status = "not_anchored"
            first = min(basis, key=lambda h: h["date"]) if basis else None
            prim = pubs[nct]["primary"]
            out["pairs"].append(dict(nct=nct, drug=d, acronym=t.get("acronym"), trial_ids=t.get("ids"), anchor_status=status, anchor=first,
                                     all_hits=hits, pubmed_primary=prim, pubmed_rule=pubs[nct]["primary_rule"], pubmed_n=pubs[nct]["n"],
                                     publication_vs_approval=(None if not (first and prim) else ("pre_approval" if prim["date"] <= first["date"] else "post_approval"))))
    out["pubmed"] = pubs
    json.dump(out, open(os.path.join(DATA, "approval_anchor_pilot.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    for p in out["pairs"]:
        a = p["anchor"]
        print(f"{p['nct']} {p['drug']:<15} {p['anchor_status']:<22} "
              + (f"{a['date']} {a['application']} {a['kind']} {a['doc']} {a['ids']}" if a else "")
              + (f" | PMID {p['pubmed_primary']['pmid']} {p['pubmed_primary']['date']} {p['publication_vs_approval']}" if p["pubmed_primary"] else ""))


if __name__ == "__main__":
    run()
