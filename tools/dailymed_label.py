"""Approval-basis label evidence from DailyMed: the SPL version in effect at the central trial's approval.

For every drug with a central trial in data/match.json:
  1. Approval basis = the data/central_anchor.json anchor whose NCT is the central trial (application, approval date).
  2. SPL set id for that application = data/dailymed_s14.json (DailyMed v2 spls.json lookup).
  3. Version = the first SPL version published on or after the approval date (v2 spls/{setid}/history.json).
     The v2 API serves only the current XML, so the version is fetched as the archived zip
     (dailymed/getFile.cfm?setid=..&type=zip&version=N), which holds the SPL XML and its figure images.
     If that version does not name the central NCT in section 14, later versions are tried in order and the
     first that names it is used (rule recorded in "version_rule").
  4. Section 14 (LOINC 34092-7) and section 6 (34084-4) are walked in document order. Text sets the current
     study context (NCT ids, study names); each table and figure inherits it. Subsections for other diseases
     (CLL, mastocytosis, MDS, ...) are skipped.
  5. Tables and figures in the central trial's context are tagged "central"; others keep their own NCT ids.

Output: data/label_evidence.json. XML and images are cached in data/dailymed/versions/.
`python tools/dailymed_label.py attach` writes the evidence onto data/tree.json without rebuilding it.
"""
import io, json, os, re, sys, zipfile
from datetime import date, datetime
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dailymed_s14 import get, API

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
VCACHE = os.path.join(DATA, "dailymed", "versions")
OUT = os.path.join(DATA, "label_evidence.json")
ZIP_URL = "https://dailymed.nlm.nih.gov/dailymed/getFile.cfm?setid={setid}&type=zip&version={version}"
PAGE_URL = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}"
V = "{urn:hl7-org:v3}"
CODES = {"34092-7": "14", "34084-4": "6"}
NCT = re.compile(r"NCT\d{8}")
AML = re.compile(r"acute myeloid leukemia|\bAML\b", re.I)
OTHER = re.compile(r"chronic lymphocytic|\bCLL\b|\bSLL\b|mastocytosis|\bSM\b|myelodysplastic|\bMDS\b|\bCMML\b|"
                   r"cholangiocarcinoma|lymphoma|BPDCN|GIST", re.I)
STUDY = re.compile(r"\b(?:Study|Trial)\s+([A-Z][\w-]*\d[\w-]*|\d+)\b|\(([A-Z][A-Z0-9-]{2,}(?: [A-Z0-9-]+)*)\)")
KIND = [("baseline", re.compile(r"baseline|demographic|characteristic|disposition", re.I)),
        ("efficacy", re.compile(r"efficacy|response|survival|remission|\bCR\b|transfusion|outcome|results", re.I)),
        ("safety", re.compile(r"adverse|laborator|abnormalit|reaction", re.I))]
RESULT = re.compile(r"median|hazard ratio|\bHR\b|complete remission|\bCR\b|overall survival|event-free|"
                    r"response rate|transfusion", re.I)
HR = re.compile(r"(\d+\.\d+)\s*\(\s*(\d+\.\d+)\s*[,\-–]\s*(\d+\.\d+)\s*\)")


def txt(el):
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def pubdate(s):
    return datetime.strptime(s, "%b %d, %Y").date()


# ---- DailyMed version fetch -------------------------------------------------------------------------------

def history(setid):
    path = os.path.join(VCACHE, f"{setid}_history.json")
    if not os.path.exists(path):
        os.makedirs(VCACHE, exist_ok=True)
        json.dump(get(f"{API}/spls/{setid}/history.json"), open(path, "w", encoding="utf-8"))
    h = json.load(open(path, encoding="utf-8"))["data"]["history"]
    return sorted(({"version": int(x["spl_version"]), "published": pubdate(x["published_date"])} for x in h),
                  key=lambda x: x["version"])


def version_xml(setid, version):
    """Archived SPL version: XML path plus its image files (extracted next to it)."""
    base = os.path.join(VCACHE, f"{setid}_v{version}")
    xml = base + ".xml"
    if not os.path.exists(xml):
        os.makedirs(base, exist_ok=True)
        z = zipfile.ZipFile(io.BytesIO(get(ZIP_URL.format(setid=setid, version=version), binary=True)))
        names = [n for n in z.namelist() if n.lower().endswith(".xml")]
        if not names:
            raise RuntimeError(f"no XML in DailyMed zip {setid} v{version}")
        open(xml, "wb").write(z.read(names[0]))
        for n in z.namelist():
            if n.lower().endswith((".jpg", ".jpeg", ".png", ".gif")):
                open(os.path.join(base, os.path.basename(n)), "wb").write(z.read(n))
    return xml


# ---- SPL parse --------------------------------------------------------------------------------------------

def grid(part):
    """Rows of a thead/tbody/tfoot with colspan and rowspan expanded, so columns line up."""
    out, carry = [], {}
    for tr in (part.iter(V + "tr") if part is not None else []):
        row, col = [], 0
        cells = [c for c in tr if c.tag in (V + "td", V + "th")]
        for c in cells:
            while col in carry:
                row.append(carry[col][0])
                carry[col][1] -= 1
                if not carry[col][1]:
                    del carry[col]
                col += 1
            t, span = txt(c), int(c.get("colspan") or 1)
            rs = int(c.get("rowspan") or 1)
            for k in range(span):
                row.append(t)
                if rs > 1:
                    carry[col + k] = [t, rs - 1]
            col += span
        while col in carry:
            row.append(carry[col][0])
            carry[col][1] -= 1
            if not carry[col][1]:
                del carry[col]
            col += 1
        out.append(row)
    return out


def number(cell):
    """Chartable value of a cell: the percent if one is given ('17 (19.0)', '19%'), else the leading number."""
    m = re.search(r"\((\d+(?:\.\d+)?)\s*%?\s*\)", cell) or re.search(r"(\d+(?:\.\d+)?)\s*%", cell)
    if m:
        return float(m.group(1))
    m = re.match(r"\s*[<≤>≥]?\s*(\d+(?:\.\d+)?)\b", cell)
    return float(m.group(1)) if m else None


def parse_table(t):
    head, body, foot = grid(t.find(V + "thead")), grid(t.find(V + "tbody")), grid(t.find(V + "tfoot"))
    if not head and body:                       # some labels put the header in tbody
        head, body = body[:1], body[1:]
    width = max([len(r) for r in head + body] or [0])
    columns = []
    for i in range(width):
        parts = []
        for r in head:
            if i < len(r) and r[i] and r[i] not in parts:
                parts.append(r[i])
        columns.append(" | ".join(parts))
    cap = txt(t.find(V + "caption"))
    kind = next((k for k, rx in KIND if rx.search(cap)), "other")
    series, hrs = [], []
    for r in body:
        if not r or len(set(r)) == 1:           # section-header row spanning the table
            continue
        vals = [number(c) for c in r[1:]]
        if sum(v is not None for v in vals) >= 2:
            series.append({"label": r[0], "values": vals})
        if re.search(r"hazard ratio|\bHR\b", r[0], re.I):
            for c in r[1:]:
                m = HR.search(c)
                if m:
                    hrs.append({"label": r[0], "hr": float(m.group(1)), "lo": float(m.group(2)), "hi": float(m.group(3))})
                    break
    return {"caption": cap[:300], "kind": kind, "columns": columns, "header": head, "rows": body,
            "footnotes": [r[0] for r in foot if r], "series": series, "hazard_ratios": hrs}


INDICATIONS = "34067-9"
AML_IND = re.compile(AML.pattern + r"|acute leukemia", re.I)      # Revuforj: 'acute leukemia with a KMT2A translocation'
TEXT_HR = re.compile(r"(?:hazard ratio|\bHR\b)[^0-9]{0,20}(\d+\.\d+)[^0-9]{1,20}?95%\s*CI[:\s]*(\d+\.\d+)\s*[,\-–]\s*(\d+\.\d+)", re.I)
STUDY_NAME = re.compile(r"\b(?:Study|Trial)\s+([A-Z][\w-]*\d[\w-]*|\d+)\b")
EUDRACT = re.compile(r"^\d{4}-\d{6}-\d{2}")
COUNT = re.compile(r"\b(\d{1,3}(?:,\d{3})?)\s+(?:adult\s+|randomized\s+|evaluable\s+|enrolled\s+)?(?:patients|subjects)\b", re.I)
ENDPOINT = re.compile(r"^\s*(?:CR\b|CRh\b|CRi\b|CR\s*\+|CR/|composite complete|complete remission|overall survival|"
                      r"median|event-free|hazard ratio|HR\b|p-value|transfusion|duration|overall response|ORR\b|"
                      r"remission|relapse-free|time to|DOR\b|DCR\b|EFS\b|OS\b)", re.I)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def aact_aliases(ncts):
    """Study codes, acronyms and enrollment of the central trials from AACT (cached). Codes are matched in the label
    text with punctuation ignored ('AG-221-C-001' == 'AG221-C-001', 'ALFA 0701' == 'ALFA-0701')."""
    path = os.path.join(VCACHE, "aact_aliases.json")
    cache = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    need = sorted(set(ncts) - set(cache))
    if need:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from export_disease_tree import aact_client
        ph = ",".join(["%s"] * len(need))
        for r in aact_client().run_query(
                f"select s.nct_id, s.acronym, s.enrollment, array_remove(array_agg(i.id_value), null) ids "
                f"from ctgov.studies s left join ctgov.id_information i on i.nct_id = s.nct_id "
                f"where s.nct_id in ({ph}) group by s.nct_id, s.acronym, s.enrollment", tuple(need), limit=len(need)):
            codes = [x for x in [r["acronym"]] + list(r["ids"] or []) if x]
            codes = sorted({x.strip() for x in codes if re.search(r"[A-Za-z]", x) and re.search(r"\d", x)
                            and len(norm(x)) >= 5 and not EUDRACT.match(x)})
            cache[r["nct_id"]] = {"codes": codes, "acronym": r["acronym"], "enrollment": r["enrollment"]}
        os.makedirs(VCACHE, exist_ok=True)
        json.dump(cache, open(path, "w", encoding="utf-8"), indent=1)
    return cache


def flow(el):
    """Text blocks of a section <text> in document order. Tables are yielded whole (their cell paragraphs are not
    re-read as text); paragraphs and list items are yielded with any images they hold."""
    for ch in el:
        tag = ch.tag.replace(V, "")
        if tag == "table":
            yield "table", ch
        elif tag in ("paragraph", "item"):
            yield "para", ch
            for m in ch.iter(V + "renderMultiMedia"):
                yield "figure", m
        elif tag == "renderMultiMedia":
            yield "figure", ch
        else:
            yield from flow(ch)


class Doc:
    def __init__(self, root, central, img_dir, alias_info):
        self.central, self.img_dir = central, img_dir
        self.codes = {norm(c): c for c in alias_info.get("codes", [])}
        self.enrollment = alias_info.get("enrollment")
        self.media = {}
        for m in root.iter(V + "observationMedia"):
            ref = m.find(f"{V}value/{V}reference")
            self.media[m.get("ID")] = {"file": ref.get("value") if ref is not None else None, "alt": txt(m.find(V + "text"))}
        self.aliases, self.study_map = set(), {}
        self.items, self.studies, self.results, self.aml_s14 = [], {}, [], False
        self.study_results = {}

    def code_in(self, s):
        n = norm(s)
        return next((c for k, c in self.codes.items() if k in n), None)

    def context(self, s, section, ctx):
        """Update the study context from one paragraph; ctx['tier'] says how the central trial was linked."""
        found = set(NCT.findall(s))
        m = STUDY_NAME.search(s)
        name = "Study " + m.group(1) if m else None
        code = self.code_in(s)
        if found:
            ctx.update(ncts=found, study=name or code, tier="nct" if self.central in found else "other_nct")
            if name:
                self.study_map[name] = found
                if self.central in found:
                    self.aliases.add(name)
            self.studies.setdefault(",".join(sorted(found)), {"ncts": sorted(found), "study": name or code,
                                                             "section": section, "subsection": ctx["sub"],
                                                             "excerpt": s[:600], "linked_by": "NCT in label"})
        elif code:
            ctx.update(ncts={self.central}, study=name or code, tier="aact_code")
            if name:
                self.aliases.add(name)
            self.studies.setdefault(self.central, {"ncts": [self.central], "study": name or code, "section": section,
                                                   "subsection": ctx["sub"], "excerpt": s[:600],
                                                   "linked_by": f"AACT study code {code}"})
        elif name and name in self.aliases:
            ctx.update(ncts={self.central}, study=name, tier=ctx["tier"] if ctx["study"] == name and ctx["tier"] else "study_name")
        elif name and name in self.study_map:
            ctx.update(ncts=set(self.study_map[name]), study=name, tier="study_name")
        elif name and name != ctx["study"]:
            ctx.update(ncts=set(), study=name, tier=None)                 # a new, unidentified study starts
        if section == "14" and not ctx["ncts"] and self.enrollment:
            if any(int(x.replace(",", "")) == self.enrollment for x in COUNT.findall(s)):
                ctx.update(ncts={self.central}, tier="aact_enrollment")
                if ctx["study"]:
                    self.aliases.add(ctx["study"])
                self.studies.setdefault(self.central, {"ncts": [self.central], "study": ctx["study"], "section": section,
                                                       "subsection": ctx["sub"], "excerpt": s[:600],
                                                       "linked_by": f"enrollment {self.enrollment} = AACT"})

    def walk(self, sec, section, ctx):
        for ch in sec:
            tag = ch.tag.replace(V, "")
            if tag == "component":
                s = ch.find(V + "section")
                if s is None:
                    continue
                title = txt(s.find(V + "title"))
                if title and OTHER.search(title) and not AML.search(title):
                    continue                    # subsection for another disease (MDS, CLL, ...)
                if section == "14" and re.match(r"14\.\d", title) and not AML.search(title):
                    continue
                if section == "14" and AML.search(title):
                    self.aml_s14 = True
                self.walk(s, section, dict(ctx, sub=title or ctx["sub"]))
            elif tag == "text":
                for kind, el in flow(ch):
                    central = self.central in ctx["ncts"]
                    base = {"section": section, "subsection": ctx["sub"], "ncts": sorted(ctx["ncts"]),
                            "study": ctx["study"], "central": central, "link": ctx["tier"] if central else None}
                    if kind == "table":
                        self.items.append(dict(parse_table(el), type="table", **base))
                    elif kind == "figure":
                        m = self.media.get(el.get("referencedObject"), {})
                        self.items.append(dict(base, type="figure", file=m.get("file"), img_dir=self.img_dir,
                                               caption=ctx["last_fig"] or m.get("alt", "")))
                    else:
                        s = txt(el)
                        if s.lower().startswith("figure"):
                            ctx["last_fig"] = s[:200]
                        self.context(s, section, ctx)
                        if section == "14" and ctx["ncts"] and RESULT.search(s) and re.search(r"\d", s):
                            found = [x for x in re.split(r"(?<=[.;])\s+(?=[A-Z])", s)
                                     if RESULT.search(x) and re.search(r"\d", x)]
                            for n in ctx["ncts"]:
                                self.study_results.setdefault(n, []).extend(found)
                            if self.central in ctx["ncts"]:
                                self.results.extend(found)

    def run(self, el):
        """Walk each coded section once; a coded section nested in another is covered by its parent."""
        for ch in el:
            if ch.tag == V + "section":
                c = ch.find(V + "code")
                section = CODES.get(c.get("code") if c is not None else None)
                if section:
                    self.walk(ch, section, {"ncts": set(), "study": None, "tier": None, "last_fig": "", "sub": section})
                    continue
            self.run(ch)


def indications(root):
    for sec in root.iter(V + "section"):
        c = sec.find(V + "code")
        if c is not None and c.get("code") == INDICATIONS:
            return txt(sec)
    return ""


def parse(xml, central, alias_info):
    root = ET.parse(xml).getroot()
    et = root.find(V + "effectiveTime")
    img_dir = os.path.relpath(os.path.splitext(xml)[0], VCACHE).replace(os.sep, "/")
    doc = Doc(root, central, img_dir, alias_info)
    doc.run(root)                               # pass 1 learns the central study's names ("Study 1")
    aliases, first = set(doc.aliases), doc.studies
    doc = Doc(root, central, img_dir, alias_info)
    doc.aliases = set(aliases)
    doc.run(root)                               # pass 2 tags with those names known from the start
    for k, v in first.items():                  # how pass 1 linked a name that pass 2 then knew up front
        doc.studies.setdefault(k, v)
    s14_ncts = sorted({n for s in doc.studies.values() if s["section"] == "14" for n in s["ncts"]})
    tiers = sorted({x["link"] for x in doc.items if x["central"] and x["section"] == "14" and x["link"]})
    # section 6 tables with no study context belong to the central trial only when it is the label's only AML study
    if s14_ncts == [central]:
        for x in doc.items:
            if x["section"] == "6" and not x["central"] and not x["ncts"] and not x["study"]:
                x["central_inferred"] = True
                x["link"] = "only_aml_study_in_label"
    aml_ind = [s.strip() for s in re.split(r"(?<=[.;])\s+", indications(root)) if AML_IND.search(s)]
    return {"effective_time": et.get("value") if et is not None else None, "aliases": sorted(aliases),
            "aml_indicated": bool(aml_ind), "aml_indications": aml_ind[:6], "aml_section14": doc.aml_s14,
            "s14_ncts": s14_ncts, "central_named": central in s14_ncts, "central_link": tiers,
            "studies": list(doc.studies.values()), "items": doc.items,
            "study_results": {n: list(dict.fromkeys(v))[:15] for n, v in doc.study_results.items()}, "key_results": list(dict.fromkeys(doc.results))[:15]}


# ---- per drug ---------------------------------------------------------------------------------------------

def approval_basis(drug, nct, anchors):
    for a in anchors.get(drug, {}).get("anchors", []):
        if a["nct"] == nct:
            return {k: a.get(k) for k in ("nct", "application", "submission", "approval_date", "kind", "approved_regimen_key")}
    return None


def strength(p):
    """3 = AML indication + central trial named in AML section 14 by NCT or AACT study code; 2 = linked by a
    'Study N' name or AACT enrollment; 1 = AML indication only; 0 = no AML indication (e.g. an MDS-only version)."""
    if not p["aml_indicated"]:
        return 0
    if set(p["central_link"]) & {"nct", "aact_code"}:
        return 3
    if p["central_named"] or p["central_link"]:
        return 2
    return 1


def evidence(drug, nct, basis, labels, alias_info, window=400):
    """AML approval package: the first SPL version, on or after the AML approval date, whose indications name AML and
    whose AML section 14 names the central trial. Earlier versions (e.g. Inqovi's 2020 MDS-only label) are ignored."""
    labs = [l for l in labels if l["application"] == basis["application"]]
    if not labs:
        return {"status": "no_spl"}
    setid = labs[0]["setid"]
    hist = history(setid)
    approved = date.fromisoformat(basis["approval_date"])
    before = [h for h in hist if h["published"] < approved]
    window_v = [h for h in hist if 0 <= (h["published"] - approved).days <= window]
    later = [h for h in hist if (h["published"] - approved).days > window]
    best, tried = None, []
    rule = "first version on/after the AML approval date with an AML indication and the central trial in AML section 14"
    for h in window_v:
        p = parse(version_xml(setid, h["version"]), nct, alias_info)
        st = strength(p)
        tried.append({"version": h["version"], "published": h["published"].isoformat(), "aml_indicated": p["aml_indicated"],
                      "central_link": p["central_link"]})
        if best is None or st > best[2]:
            best = (h, p, st)
        if st == 3:
            break
    if best is None or best[2] < 2:
        for h in later:                         # nothing in the approval window: first later version that names it
            p = parse(version_xml(setid, h["version"]), nct, alias_info)
            if strength(p) >= 2:
                best, rule = (h, p, strength(p)), "no approval-window version links the central trial; first later version used"
                break
    if best is None:
        h = hist[-1]
        best, rule = (h, parse(version_xml(setid, h["version"]), nct, alias_info), 0), "no version on/after approval; latest used"
    h, p, st = best
    gap = (h["published"] - approved).days
    status = ("late_version" if gap > window and st >= 2 else "verified" if st == 3 else "verified_by_study_link" if st == 2
              else "central_not_named" if st == 1 else "no_aml_indication")
    return {"status": status, "approval_basis": basis, "setid": setid, "title": labs[0].get("title"),
            "version": h["version"], "published": h["published"].isoformat(), "days_after_approval": gap,
            "version_rule": rule, "versions_tried": tried, "aact_alias": alias_info,
            "ignored_pre_approval_versions": [b["version"] for b in before],
            "earliest_version": hist[0]["version"], "earliest_published": hist[0]["published"].isoformat(),
            "zip_url": ZIP_URL.format(setid=setid, version=h["version"]), "page_url": PAGE_URL.format(setid=setid), **p}


TEXT_MEDIAN = re.compile(r"(\d+(?:\.\d+)?)\s+months\s+(?:in|for)\s+(?:the\s+)?(.+?)(?:\s+arm)?(?=\s+versus|\s+vs\.?|\s+with\b|;|,|\s+and\b|\.\s|\.$|\s+\()", re.I)
TEXT_MEDIAN2 = re.compile(r"(?:for|in)\s+(?:the\s+)?([A-Za-z+ -]+?)(?:\s+arm)?\s+was\s+(\d+(?:\.\d+)?)\s+months", re.I)
TEXT_P = re.compile(r"\bp(?:-value)?\s*(?:=|<|>|less than|greater than|of)?\s*[0-9.]*[0-9]", re.I)
COMBO = re.compile(r"\bin combination with (.+?)(?=\s+(?:in|for)\s+(?:adult\s+)?patients|\s+was\b|,|\.\s|\.$)", re.I)


def text_endpoint(s):
    """Endpoint name for a section 14 result sentence."""
    low = s.lower()
    name = ("Overall survival" if re.search(r"overall survival|\bOS\b", s) else
            "Event-free survival" if re.search(r"event-free|\bEFS\b", s) else
            "Relapse-free survival" if "relapse-free" in low else "Hazard ratio")
    if "subgroup" in low or re.match(r"(?:Of\s+)?\d+/\d+", s):
        return name + " (exploratory subgroup)"
    return name + (" (exploratory)" if "exploratory" in low else "")


def efficacy(e):
    """Efficacy read for the approved-drug pathway, traced to the approval-package label version. Endpoint rows
    come from the central trial's section 14 tables. A hazard ratio that the label gives only in the result text
    becomes its own endpoint row, so every hazard ratio sits in the endpoint table. A text hazard ratio that a
    table already holds is not repeated; the table row is marked as confirmed by the text."""
    src = {k: e.get(k) for k in ("setid", "version", "published", "zip_url", "status")}
    tables = [x for x in e.get("items", []) if x["type"] == "table" and x["section"] == "14" and x["central"]
              and x["kind"] != "baseline"]
    endpoints, hrs = [], []
    for t in tables:
        for r in t["rows"]:
            if r and any(c.strip() for c in r[1:]) and len(set(r)) > 1 and ENDPOINT.search(r[0]):
                endpoints.append({"endpoint": r[0], "values": dict(zip(t["columns"][1:], r[1:])), "table": t["caption"][:160],
                                  "subsection": t["subsection"], "link": t["link"], "from": "section 14 table"})
        hrs += [dict(h, table=t["caption"][:160], **{"from": "section 14 table"}) for h in t["hazard_ratios"]]
    text_rows(e.get("key_results", []), endpoints, hrs)
    return {"source": src, "endpoints": endpoints[:40], "hazard_ratios": hrs, "key_results": e.get("key_results", []),
            "tables": len(tables)}


def text_rows(sentences, endpoints, hrs):
    """Turn section 14 result sentences that give a hazard ratio into endpoint rows, unless a table row has it."""
    for s in sentences:
        for m in TEXT_HR.finditer(s):
            hr, lo, hi = float(m.group(1)), float(m.group(2)), float(m.group(3))
            same = [h for h in hrs if h.get("from") == "section 14 table" and (h["hr"], h["lo"], h["hi"]) == (hr, lo, hi)]
            if same:
                for h in same:
                    h["text_check"] = "matches section 14 text"
                continue
            vals = {arm.strip(): f"{med} months" for med, arm in TEXT_MEDIAN.findall(s)}
            if not vals:                        # medians in a neighbouring sentence about the same endpoint
                key = "OS" if re.search(r"overall survival|\bOS\b", s) else "EFS" if re.search(r"event-free|\bEFS\b", s) else None
                for o in sentences:
                    if key and o is not s and re.search(r"median", o, re.I) and re.search(rf"\b{key}\b", o):
                        vals = ({arm.strip(): f"{med} months" for med, arm in TEXT_MEDIAN.findall(o)}
                                or {arm.strip(): f"{med} months" for arm, med in TEXT_MEDIAN2.findall(o)})
                        if vals:
                            break
            vals["HR (95% CI)"] = f"{m.group(1)} ({m.group(2)}–{m.group(3)})"
            pv = TEXT_P.search(s)
            if pv:
                vals["p-value"] = pv.group(0)
            name = text_endpoint(s)
            endpoints.append({"endpoint": name, "values": vals, "table": "section 14 text (no table in label)",
                              "subsection": None, "link": "central", "from": "section 14 text", "sentence": s[:300]})
            hrs.append({"label": name, "hr": hr, "lo": lo, "hi": hi, "table": "section 14 text (no table in label)",
                        "from": "section 14 text", "sentence": s[:300]})


STUDY_CODE = re.compile(r"\b(VIALE-[A-Z]|[A-Z]{1,4}\d{2,3}-\d{3,4}|[A-Z]{3,5}\d{3,4}|[A-Z]+-\d{2,4}|QuANTUM-First|ADMIRAL|RATIFY|AGILE)\b")


def study_name(st):
    """The label's name for a study: its own tag, else the first study code in the excerpt."""
    if st.get("study"):
        return st["study"]
    m = STUDY_CODE.search(st.get("excerpt") or "")
    return m.group(1) if m else None


def study_combination(st, sentences=()):
    """Partner drugs the label names for the study, e.g. 'azacitidine (N=84) or decitabine (N=31)'.
    Falls back to the study's result sentences when the excerpt is only a heading."""
    for t in [st.get("excerpt") or "", *sentences]:
        m = COMBO.search(t)
        if m:
            return m.group(1).strip()
    return None


def study_efficacy(p, nct, name=None):
    """Section 14 efficacy table rows that the label ties to one AML study, by NCT or by the study name in the caption."""
    rows, hrs = [], []
    bare = name.replace("Study ", "") if name else None
    for t in p["items"]:
        if t["type"] != "table" or t["section"] != "14" or t["kind"] == "baseline":
            continue
        if nct not in (t.get("ncts") or []) and not (bare and bare in t["caption"] and not t.get("ncts")):
            continue
        for r in t["rows"]:
            if r and any(c.strip() for c in r[1:]) and len(set(r)) > 1 and ENDPOINT.search(r[0]):
                rows.append({"endpoint": r[0], "values": dict(zip(t["columns"][1:], r[1:])), "table": t["caption"][:160],
                             "subsection": t["subsection"], "from": "section 14 table"})
        hrs += [dict(h, table=t["caption"][:160], **{"from": "section 14 table"}) for h in t["hazard_ratios"]]
    text_rows(p.get("study_results", {}).get(nct, []), rows, hrs)
    return rows, hrs, p.get("study_results", {}).get(nct, [])


def study_block(p, nct, st):
    rows, hrs, text = study_efficacy(p, nct, study_name(st))
    return {"endpoints": rows, "hazard_ratios": hrs, "key_results": text}


def aml_studies(p):
    """AML section 14 studies of one label version, keyed by NCT."""
    out = {}
    for st in p["studies"]:
        if st["section"] == "14" and AML.search(st.get("subsection") or ""):
            for n in st["ncts"]:
                out.setdefault(n, st)
    return out


def approval_studies(e, alias_info):
    """Every AML study the label gives section 14 evidence for, not only the central trial. Studies in the approval
    version are part of the approval package. Studies that only appear in later versions (a conversion to full
    approval, a new combination) are dated to the first version that names them."""
    setid, central = e["setid"], e["central"]
    hist = history(setid)
    cur = parse(version_xml(setid, e["version"]), central, alias_info)
    rows = [{"nct": n, "study": study_name(st), "combination": study_combination(st, cur["study_results"].get(n, [])), "subsection": st.get("subsection"), "excerpt": st.get("excerpt"),
             "role": "central" if n == central else "approval package", "version": e["version"],
             "published": e["published"], **study_block(cur, n, st)}
            for n, st in aml_studies(cur).items()]
    seen = {r["nct"] for r in rows}
    post = [h for h in hist if h["version"] > e["version"]]
    if not post:
        return rows
    parsed = {}

    def at(i):
        if i not in parsed:
            parsed[i] = parse(version_xml(setid, post[i]["version"]), central, alias_info)
        return parsed[i]
    latest = aml_studies(at(len(post) - 1))
    for n in [n for n in latest if n not in seen]:
        lo, hi = 0, len(post) - 1                # first version naming it; labels keep a study once added
        while lo < hi:
            mid = (lo + hi) // 2
            if n in aml_studies(at(mid)):
                hi = mid
            else:
                lo = mid + 1
        h, pv = post[lo], at(lo)
        st = aml_studies(pv)[n]
        rows.append({"nct": n, "study": study_name(st), "combination": study_combination(st, pv["study_results"].get(n, [])), "subsection": st.get("subsection"), "excerpt": st.get("excerpt"),
                     "role": "later label version", "version": h["version"], "published": h["published"].isoformat(),
                     "zip_url": ZIP_URL.format(setid=setid, version=h["version"]), **study_block(pv, n, st)})
    return rows


def build():
    pairs = json.load(open(os.path.join(DATA, "match.json"), encoding="utf-8"))["pairs"]
    central = {p["drug"]: p["nct_id"] for p in pairs if p.get("relation") == "central"}
    anchors = json.load(open(os.path.join(DATA, "central_anchor.json"), encoding="utf-8"))["drugs"]
    # match.json can predate a new anchor (drug newly promoted by the US gate): use the earliest anchored trial then.
    for d, a in anchors.items():
        ancs = sorted(a.get("anchors") or [], key=lambda x: x.get("approval_date") or "")
        if ancs and central.get(d) not in {x["nct"] for x in ancs}:
            print(f"[central] {d}: match.json central {central.get(d)} is not anchored; using {ancs[0]['nct']}", flush=True)
            central[d] = ancs[0]["nct"]
    dm = json.load(open(os.path.join(DATA, "dailymed_s14.json"), encoding="utf-8"))["drugs"]
    aliases = aact_aliases([central[d] for d in dm if central.get(d)])
    out = {}
    for drug in sorted(dm):
        nct = central.get(drug)
        basis = approval_basis(drug, nct, anchors) if nct else None
        if not basis:
            out[drug] = {"status": "no_approval_basis", "central": nct}
            continue
        e = out[drug] = dict(evidence(drug, nct, basis, dm[drug]["labels"], aliases.get(nct, {})), central=nct)
        if e.get("items") is not None:
            e["efficacy"] = efficacy(e)
            e["approval_studies"] = approval_studies(e, aliases.get(nct, {}))
        it = e.get("items", [])
        n = lambda f: sum(1 for x in it if f(x))
        print(f"{drug[:20]:20} {e['status']:22} v{e.get('version')} {e.get('published')} (+{e.get('days_after_approval')}d) "
              f"link={e.get('central_link')} eff_tables={e.get('efficacy', {}).get('tables')} "
              f"endpoints={len(e.get('efficacy', {}).get('endpoints', []))} "
              f"saf={n(lambda x: x['type'] == 'table' and x['section'] == '6' and (x['central'] or x.get('central_inferred')))}/"
              f"{n(lambda x: x['type'] == 'table' and x['section'] == '6')} "
              f"fig={n(lambda x: x['type'] == 'figure' and x['central'])} ignored_pre={len(e.get('ignored_pre_approval_versions', []))}")
    json.dump({"source": "DailyMed v2 REST (spls/{setid}/history.json) + archived SPL versions (getFile.cfm zip), "
                         "sections 34067-9, 34092-7, 34084-4; AACT study codes and enrollment for the central trial",
               "generated": datetime.now().isoformat(timespec="seconds"), "drugs": out},
              open(OUT, "w", encoding="utf-8"), indent=1, default=str)
    return out


# ---- tree attach (no rebuild) -----------------------------------------------------------------------------

def compact(e):
    """What the viewer needs: the central trial's items (and inferred section 6 tables), plus a count of the rest."""
    keep = [x for x in e.get("items", []) if x["central"] or x.get("central_inferred")]
    return {k: e.get(k) for k in ("status", "central", "approval_basis", "setid", "title", "version", "published",
                                  "days_after_approval", "version_rule", "effective_time", "aliases", "s14_ncts",
                                  "central_named", "central_link", "aml_indications", "ignored_pre_approval_versions",
                                  "zip_url", "page_url", "key_results", "efficacy",
                                  "approval_studies")} | {
        "items": keep, "other_trial_items": len(e.get("items", [])) - len(keep),
        "studies": [s for s in e.get("studies", []) if e.get("central") in s["ncts"]]}


def attach_label_evidence(tree, path=OUT):
    """Put label evidence on every central study node and every pathway trial node of the same NCT."""
    if not os.path.exists(path):
        return 0
    by_nct, by_drug = {}, {}
    for drug, e in json.load(open(path, encoding="utf-8"))["drugs"].items():
        if e.get("approval_basis"):
            by_nct.setdefault(e["central"], compact(e))
            by_drug[drug] = e
    hits = 0

    def walk(n):
        nonlocal hits
        a = n.get("attrs") or {}
        nct = a.get("nct_id") or (n["id"].rsplit(":", 1)[-1] if n.get("kind") == "trial" else None)
        central = a.get("relation") == "central" or (n.get("kind") == "trial" and nct in by_nct)
        if central and nct in by_nct:
            a["label_evidence"] = by_nct[nct]
            n["attrs"] = a
            hits += 1
        for c in n.get("children") or []:
            walk(c)
    walk(tree)
    tag_drugs(tree, by_drug)
    return hits


HEADLINE = re.compile(r"hazard ratio|\bHR\b|median (?:OS|EFS|overall|event|survival)|CR(?:/CRh| or CRh| rate)|superior", re.I)
NOT_EFFICACY = re.compile(r"time to|follow-up|eligib|transplant|HSCT|\bSCT\b|duration|did so within", re.I)
# endpoint rows in label tables, best first: survival, then response
PRIMARY_ROWS = [re.compile(p, re.I) for p in (
    r"^median (?:OS|overall survival)", r"^median EFS|^median event", r"hazard ratio",
    r"^CR\s*\+\s*CRh|^CR/CRh|^CR or CRh", r"^CR\b(?!h)", r"^CRc|^composite")]


def short_endpoint(s):
    """Drop a footnote glued to a table row name: 'CR CR was defined as ...' -> 'CR', 'Median DORDOR was ...' -> 'Median DOR'."""
    if len(s) <= 40:
        return s
    for i in range(1, len(s)):
        if s[i].isupper() and (s[i - 1].isalnum() or s[i - 1] in " )"):
            tok = re.match(r"[A-Za-z]+", s[i:]).group()
            if tok in s[:i]:
                return s[:i].strip()
    return s


def headline_of(ef, kr):
    """Best efficacy sentence: a primary endpoint row from the label table, else a hazard ratio, else a key result."""
    rows = [dict(x, endpoint=short_endpoint(x.get("endpoint") or "")) for x in ef.get("endpoints") or []]
    rows = [x for x in rows if x.get("values") and not NOT_EFFICACY.search(x["endpoint"])]
    for p in PRIMARY_ROWS:
        x = next((x for x in rows if p.search(x["endpoint"])), None)
        if x:
            vs = list(x["values"].items())
            if len({v for _, v in vs}) == 1 and len(vs) > 1:
                vs = vs[:1]  # a between-arm statistic repeated in each arm column
            return x["endpoint"] + ": " + "; ".join(" ".join(k.split()) + ": " + v for k, v in vs)
    hr = ef.get("hazard_ratios") or []
    if hr:
        h = hr[0]
        if isinstance(h, str):
            return h
        return f"{h.get('label', 'HR')}: HR {h.get('hr')} (95% CI {h.get('lo')}, {h.get('hi')})"
    good = [s for s in kr if not NOT_EFFICACY.search(s)]
    return next((s for s in good if HEADLINE.search(s)), good[0] if good else (kr[0] if kr else None))


def tag_drugs(tree, by_drug):
    """Drug-level efficacy tag: the approval-basis trial's label endpoints and a headline result, for each approved drug."""
    def walk(n):
        a = n.get("attrs") or {}
        if n.get("kind") == "drug" and a.get("category") == "APPROVED_AT_SCOPE":
            parts = set(re.split(r"[/ ]", n["name"]))
            e = next((v for k, v in by_drug.items() if k.split()[0] in parts), None)
            if e:
                ef = e.get("efficacy") or {}
                kr = e.get("key_results") or ef.get("key_results") or []
                a["efficacy_tag"] = dict(
                    status=e.get("status"), central=e.get("central"), label=e.get("title"), version=e.get("version"),
                    published=e.get("published"), page_url=e.get("page_url"),
                    approval=e.get("approval_basis"),
                    endpoints=[short_endpoint(x.get("endpoint") or "") for x in ef.get("endpoints") or []][:8],
                    hazard_ratios=(ef.get("hazard_ratios") or [])[:4],
                    headline=headline_of(ef, kr))
            else:
                a["efficacy_tag"] = dict(status="missing")
        for c in n.get("children") or []:
            walk(c)
    walk(tree)


if __name__ == "__main__":
    if sys.argv[1:] == ["attach"]:
        tp = os.path.join(DATA, "tree.json")
        tree = json.load(open(tp, encoding="utf-8"))
        print("label_evidence attached to", attach_label_evidence(tree), "nodes")
        json.dump(tree, open(tp, "w", encoding="utf-8"), indent=1, ensure_ascii=False, default=str)
    else:
        build()
