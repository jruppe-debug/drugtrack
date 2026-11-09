"""Approved-indication records: one row per (drug, FDA approval event, scope-disease indication), coded to identifiers.
All disease wording comes from config/diseases/<disease>.json (disease_config.load).
Per drug with an approval-time label in data/label_evidence.json (DailyMed setid + version) and an anchor in
data/central_anchor.json:
  product    SPL product data: active ingredient UNII codes, strength, form, route, proprietary name. Two or more active
             ingredients in one product = fixed-combination product, keyed by its MeSH record (exact NLM lookup of the
             proprietary name, else the supplementary record in the disease universe whose MeSH mapped-to descriptors are
             exactly the label's ingredients): Vyxeos -> C000629812, Inqovi -> C000723076.
  section 1  indication subsections naming the disease (ontology names + config label_title_extra_terms). Each gives:
             setting, biomarker, chemo eligibility (stated ineligible / stated eligible / inferred from an intensive induction
             regimen / not stated), indication role (config indication_roles; e.g. transplant conditioning is excluded).
  regimen    label partner drug = a drug other than the labeled product that the label names as part of the approved
             regimen ('in combination with A and B'). Phase words split the text; each phase lists its partners, 'or'
             gives alternatives, a config class word ('anthracycline') becomes a class slot filled later from the trial arm.
             Source = section 1; when section 1 names no partner, the section 2 dosing block for the same setting.
  section 2  dosing: dose, route, frequency, days per phase. Subsections: cross-referenced, disease-titled, or 'Recommended
             Dosage' when the label is for the scope disease only.
  section 14 NCT ids, 'Study N' names, enrolled N, design words.
  approval   Drugs@FDA event from central_anchor; EMA by INN + disease MeSH heading; PubMed by NCT[si].
Output: data/indication_records.json
Run:  python tools/indication_records.py [--disease aml] [DRUG ...]
"""
import argparse, gzip, json, os, re, sys
import xml.etree.ElementTree as ET
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fidelity as F
import disease_config
from regimen_key import Crosswalk, key, DRUG_COMBINATIONS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
BRIDGE = os.environ.get("AACT_BRIDGE_DIR", os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "AACT_bridge")))
VCACHE = os.path.join(DATA, "dailymed", "versions")
OUT = os.path.join(DATA, "indication_records.json")
NS = {"v3": "urn:hl7-org:v3"}
V = "{urn:hl7-org:v3}"
UNII_OID = "2.16.840.1.113883.4.9"
SEC = {"1": "34067-9", "2": "34068-7", "14": "34092-7"}
DOSE = re.compile(r"(\d+(?:\.\d+)?)\s*(mg/m2|mg/kg|mg|mcg|g/m2|g/m)\b", re.I)
ROUTE = re.compile(r"\b(orally|intravenously|subcutaneously|intravenous infusion)\b", re.I)
FREQ = re.compile(r"\b(once daily|twice daily|three times daily|once weekly|every \d+ (?:hours|days|weeks))\b", re.I)
DAYS = re.compile(r"\bDays?\s+(\d+)\s*(?:to|through|-|–)\s*(\d+)", re.I)
COUNT = re.compile(r"\b(\d{1,3}(?:,\d{3})?)\s+(?:adult\s+|randomized\s+|evaluable\s+|enrolled\s+)?(?:patients|subjects)\b", re.I)
NCT = re.compile(r"NCT\d{8}")
STUDY = re.compile(r"\bStudy\s+([A-Z0-9][\w-]*)\b")
REF = re.compile(r"\((\d+\.\d+)\)")
RECOMMENDED = re.compile(r"recommended dos", re.I)
TRIAL_NAME = re.compile(r"(?<![A-Za-z0-9-])([A-Z][A-Z0-9]{2,}(?:-[A-Z0-9]+)*)(?![A-Za-z0-9-])")   # SEQUOIA, ELEVATE-TN, CLL14, RESONATE-2
SALT = re.compile(r"\s+(?:phosphate|hydrochloride|dihydrochloride|sodium|disodium|potassium|mesylate|dimesylate|citrate|maleate|"
                  r"acetate|sulfate|fumarate|tartrate|besylate|tosylate|succinate|monohydrate|hydrobromide)$")


def strip_paren(t):
    """Drop short parenthetical asides: 'chronic lymphatic (lymphocytic) leukemia' -> 'chronic lymphatic leukemia'."""
    return re.sub(r"\s*\([^()]{1,30}\)", "", t)


def txt(e):
    return re.sub(r"\s+", " ", "".join(e.itertext())).strip()


def sections(root):
    out = {}
    body = root.find(".//v3:structuredBody", NS)
    for comp in body.findall("v3:component", NS):
        s = comp.find("v3:section", NS)
        if s is None:
            continue
        c = s.find("v3:code", NS)
        num = next((k for k, v in SEC.items() if c is not None and c.get("code") == v), None)
        if not num:
            continue
        subs = []
        for sc in s.findall("v3:component/v3:section", NS):
            t = sc.find("v3:title", NS)
            title = txt(t) if t is not None else ""
            m = re.match(r"\s*(\d+\.\d+)\s+(.*)", title)
            subs.append((m.group(1) if m else "", m.group(2) if m else title, txt(sc)))
        out[num] = dict(text=txt(s), subs=subs)
    return out


def product(root):
    prods = []
    for mp in root.iter(V + "manufacturedProduct"):
        inner = mp.find("v3:manufacturedProduct", NS)
        if inner is None:
            continue
        form = inner.find("v3:formCode", NS)
        name = inner.find("v3:name", NS)
        ings = []
        for ing in inner.findall("v3:ingredient", NS):
            if ing.get("classCode") not in ("ACTIB", "ACTIM", "ACTIR"):
                continue
            code = ing.find("v3:ingredientSubstance/v3:code", NS)
            nm = ing.find("v3:ingredientSubstance/v3:name", NS)
            num = ing.find("v3:quantity/v3:numerator", NS)
            if code is not None and code.get("codeSystem") == UNII_OID:
                ings.append(dict(unii=code.get("code"), name=nm.text if nm is not None else None,
                                 strength=f"{num.get('value')} {num.get('unit')}" if num is not None else None))
        for ing in inner.findall("v3:activeIngredient", NS):     # pre-2009 SPL schema
            code = ing.find("v3:activeIngredientSubstance/v3:code", NS)
            nm = ing.find("v3:activeIngredientSubstance/v3:name", NS)
            num = ing.find("v3:quantity/v3:numerator", NS)
            if code is not None and code.get("codeSystem") == UNII_OID and code.get("code") not in [i["unii"] for i in ings]:
                ings.append(dict(unii=code.get("code"), name=nm.text if nm is not None else None,
                                 strength=f"{num.get('value')} {num.get('unit')}" if num is not None else None))
        if ings:
            prods.append(dict(proprietary_name=(name.text or "").strip() if name is not None else None,
                              form=form.get("displayName") if form is not None else None,
                              form_code=form.get("code") if form is not None else None, ingredients=ings))
    if not prods:                               # kits / cell therapies: active ingredients sit under part products
        ings = []
        for ing in root.iter(V + "ingredient"):
            code = ing.find("v3:ingredientSubstance/v3:code", NS)
            nm = ing.find("v3:ingredientSubstance/v3:name", NS)
            if ing.get("classCode") in ("ACTIB", "ACTIM", "ACTIR") and code is not None and code.get("codeSystem") == UNII_OID                     and code.get("code") not in [i["unii"] for i in ings]:
                ings.append(dict(unii=code.get("code"), name=nm.text if nm is not None else None, strength=None))
        if ings:
            prods.append(dict(proprietary_name=None, form=None, form_code=None, ingredients=ings, parsed_from="whole document"))
    route = next((dict(route=r.get("displayName"), code=r.get("code")) for r in root.iter(V + "routeCode")), None)
    return prods, route


class PartnerVocab:
    """Closed vocabulary: DrugCentral preferred names of structures with an ATC L code (antineoplastic / immunomodulating)."""

    def __init__(self, xw):
        self.names = {}
        for s, r in xw.structs.items():
            if r.get("name") and any(a.startswith("L") for a in r.get("atc", [])) and len(r["name"]) >= 5:
                n = r["name"].lower()
                self.names.setdefault(n, s)
                base = SALT.sub("", n)              # label text uses the base name: 'fludarabine phosphate' -> 'fludarabine'
                if len(base) >= 5:
                    self.names.setdefault(base, s)
        self.rx = re.compile(r"(?<![A-Za-z-])(" + "|".join(re.escape(n) for n in sorted(self.names, key=len, reverse=True)) +
                             r")(?![A-Za-z])", re.I)

    def find(self, text):
        return [(m.start(), m.group(1).lower(), self.names[m.group(1).lower()]) for m in self.rx.finditer(text)]


def _groups(seg, hits):
    groups, cur, last = [], [], None
    for p, n, s in hits:
        if last is not None and re.search(r"\bor\b", seg[last:p]):
            groups.append(cur)
            cur = []
        if s not in [c[1] for c in cur]:
            cur.append((n, s))
        last = p + len(n)
    return groups + ([cur] if cur else [])


def _partners(seg, vocab, self_structs, cfg):
    hits = [h for h in vocab.find(seg) if h[2] not in self_structs]
    classes = [(m.start(), m.group(1).lower(), "class:" + m.group(1).lower()) for m in cfg["_classes"].finditer(seg)]
    return _groups(seg, sorted(hits + classes))


def phase_regimens(text, vocab, self_structs, cfg):
    marks = [(m.start(), m.end(), m.group(1).lower().replace("re-induction", "reinduction"))
             for m in cfg["_phases"].finditer(text) if not cfg["_not_phase"].search(text[max(0, m.start() - 40):m.start()])]
    if not marks:
        m = re.search(r"in combination with([^.]*)", text, re.I)
        groups = _partners(m.group(1), vocab, self_structs, cfg) if m else []
        return {"treatment": dict(alternatives=groups, days=[f"{a}-{b}" for a, b in DAYS.findall(text)][:1], monotherapy=not groups)}
    out = {}
    for i, (p, e, ph) in enumerate(marks):
        nxt = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        prv = marks[i - 1][1] if i > 0 else 0
        after = re.split(r"\.\s|\[", text[e:nxt])[0]
        before = re.split(r"\.\s", text[prv:p])[-1]
        combo = re.search(r"in combination with(.*)", after, re.I)
        seg = combo.group(1) if combo else (after if re.match(r"\s*(?:with|of)\b", after) else before)
        groups = _partners(seg, vocab, self_structs, cfg)
        if ph not in out or (groups and not out[ph]["alternatives"]):
            out[ph] = dict(alternatives=groups, days=[f"{a}-{b}" for a, b in DAYS.findall(before)][-1:], monotherapy=not groups)
    return out


def code_partners(xw, ph, cfg):
    coded = {}
    for phase, d in ph.items():
        alts = []
        for g in d["alternatives"]:
            alt = []
            for n, s in g:
                if s.startswith("class:"):
                    alt.append(dict(name=n, class_word=n, atc=cfg["class_words"][n]))
                    continue
                ing = set()
                for r in xw.structs[s].get("RXNORM", []):
                    ing |= xw.to_in(r)
                rx = set()
                for u in xw.structs[s].get("UNII", []):
                    rx |= xw.rx_unii(u)
                alt.append(dict(name=n, dc_struct=s, mesh=xw.mesh_of({s}), rxcui=sorted(ing | rx),
                                agree="agree" if ing and ing == rx else ("rxnav_only" if rx and not ing else "check")))
            alts.append(alt)
        coded[phase] = dict(alternatives=alts, days=d["days"], monotherapy=d.get("monotherapy", False))
    return coded


def setting_blocks(text, cfg):
    """Split a dosing text at each setting phrase (config settings); returns [(settings, block)]."""
    cuts = sorted({m.start() for _, r in cfg["_settings"] for m in r.finditer(text)})
    if len(cuts) < 2:
        return [(None, text)]
    cuts = [0] + cuts if cuts[0] > 0 else cuts
    blocks = []
    for i, c in enumerate(cuts):
        b = text[c:cuts[i + 1] if i + 1 < len(cuts) else len(text)]
        blocks.append(({k for k, r in cfg["_settings"] if r.search(b[:160])}, b))
    return blocks


def eligibility(text, phases, xw, cfg):
    m = cfg["_inelig"].search(text)
    if m:
        return dict(value="ineligible_for_intensive_chemo", basis="label text", evidence=m.group(0))
    m = cfg["_elig"].search(text)
    if m:
        return dict(value="eligible_for_intensive_chemo", basis="label text", evidence=m.group(0))
    rule = cfg["eligibility"].get("intensive_regimen")
    ph = phases.get(rule["phase"]) if rule else None
    if ph:
        for alt in ph["alternatives"]:
            atcs = []
            for p in alt:
                atcs += p.get("atc", []) if p.get("class_word") else [a for r in p.get("rxcui", []) for a in xw.rx_atc(r)]
            if alt and all(any(a.startswith(pre) for a in atcs for pre in need) for need in rule["atc_all"]):
                return dict(value="eligible_for_intensive_chemo", basis="inferred: " + rule["meaning"])
    return dict(value="not_stated", basis="label names no eligibility clause and no intensive induction regimen")


def fixed_product(xw, prods, labeled, universe):
    multi = [p for p in prods if len({i["unii"] for i in p["ingredients"]}) >= 2]
    if not multi:
        return None
    p = multi[0]
    rec = xw.mesh_lookup(p["proprietary_name"]) if p.get("proprietary_name") else None
    route = "exact MeSH lookup of proprietary name"
    if not rec:
        lab_desc = {m for l in labeled for m in l["mesh"] if m.startswith("D")}
        cands = {m for t in universe.values() if lab_desc <= set(t["int_mesh"]) for m in t["int_mesh"] if m.startswith("C")}
        mapped = xw.mesh_mapped(sorted(cands))
        hits = [c for c, m in mapped.items() if set(m) - {DRUG_COMBINATIONS} == lab_desc]
        rec = hits[0] if len(hits) == 1 else None
        route = "supplementary record whose MeSH mapped-to descriptors equal the label ingredients"
    return dict(proprietary_name=p.get("proprietary_name"), mesh=rec, route=route if rec else "not found",
                ingredients=sorted({i["unii"] for i in p["ingredients"]}))


def pubmed(nct):
    p = os.path.join(DATA, "references", "pubmed", f"{nct}.json")
    if not os.path.exists(p):
        return dict(status="not_cached")
    d = json.load(open(p, encoding="utf-8"))
    res = d.get("esummary", {}).get("result", {})
    arts = [dict(pmid=u, pubdate=res[u].get("sortpubdate", res[u].get("pubdate")), journal=res[u].get("source"),
                 pubtype=res[u].get("pubtype", []), title=res[u].get("title")) for u in res.get("uids", [])]
    arts.sort(key=lambda x: x["pubdate"] or "")
    prim = next((x for x in arts if {"Clinical Trial, Phase III", "Randomized Controlled Trial"} & set(x["pubtype"])), None) \
        or next((x for x in arts if any(t.startswith("Clinical Trial") for t in x["pubtype"])), None)
    return dict(status="ok", query=f"{nct}[si]", n=len(arts), primary=prim,
                rule="earliest article typed Clinical Trial, Phase III or Randomized Controlled Trial, else earliest Clinical Trial*")


def ema_row(ema, inn, heading):
    for r in ema:
        if (r.get("international_non_proprietary_name_common_name") or "").strip().lower() == (inn or "").lower() \
                and r.get("category") == "Human" and heading in (r.get("therapeutic_area_mesh") or "").split(";"):
            return {k: r.get(k) for k in ("name_of_medicine", "ema_product_number", "medicine_status",
                                          "marketing_authorisation_date", "therapeutic_area_mesh", "atc_code_human",
                                          "therapeutic_indication", "medicine_url")}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--disease", default="aml")
    ap.add_argument("--source", choices=["legacy", "scope_labels"])
    ap.add_argument("drugs", nargs="*")
    a = ap.parse_args()
    cfg = disease_config.load(a.disease)
    xw = Crosswalk()
    vocab = PartnerVocab(xw)
    ddir = disease_config.data_dir(a.disease)
    U = json.load(gzip.open(os.path.join(ddir, "mesh_universe.json.gz"), "rt", encoding="utf-8"))
    assert U["scope_id"] == cfg["scope_id"], "mesh_universe.json.gz was built for another scope; rerun mesh_universe.py"
    heading = U["mesh_headings"].get(cfg["primary_mesh"])
    scope_rx, _, _ = F.scope_term_regex(os.path.join(BRIDGE, "inputs", "opentargets", "disease.parquet"), cfg["scope_id"])
    scope = re.compile(scope_rx + "".join(r"|(?<![A-Za-z])" + re.escape(t) + r"(?![A-Za-z])" for t in cfg["label_title_extra_terms"]), re.I)
    ema = json.load(open(os.path.join(BRIDGE, "inputs", "ema", "medicines.json"), encoding="utf-8"))["data"]
    # generic path for every disease (reproduced the legacy AML results 17/17 on 2026-10-26); legacy only on request
    src = a.source or "scope_labels"
    entries = []                                   # (drug, label version, FDA anchor)
    if src == "legacy":                            # AML: label_evidence.json + central_anchor.json (FDA document search)
        le = json.load(open(os.path.join(DATA, "label_evidence.json"), encoding="utf-8"))["drugs"]
        ca = json.load(open(os.path.join(DATA, "central_anchor.json"), encoding="utf-8"))["drugs"]
        for drug, ev in sorted(le.items()):
            if drug in ca:
                anchors = [x for x in ca[drug].get("anchors", []) if x.get("role") == "lead"]
                entries.append((drug, ev, anchors[0] if anchors else {}))
    else:                                          # any disease: scope_labels.json (no FDA document search yet)
        sl = json.load(open(os.path.join(ddir, "scope_labels.json"), encoding="utf-8"))["labels"]
        for drug, v in sorted(sl.items()):
            for i_, ev_ in enumerate([dict(version=v["version"], published=v["published"], approval=v["approval"])] + v.get("history", [])):
                ap_ = ev_["approval"]
                entries.append((drug if i_ == 0 else f"{drug} @{ap_['date']}",
                                dict(setid=v["setid"], version=ev_["version"], published=ev_["published"]),
                                dict(application=ap_["application"], submission=ap_["submission"], approval_date=ap_["date"],
                                     kind="first disease approval" if i_ == 0 else "efficacy supplement (approval history)",
                                     nct=None, ids=[], sources=[], original_date=ap_.get("original_date"),
                                     in_first_version=ap_.get("in_first_version"), history_of=drug if i_ else None)))
    sub = cfg.get("excluded_subtypes", {})
    subtype_rx = re.compile("|".join(r"(?<![A-Za-z])" + re.escape(t) + r"(?![A-Za-z])" for t in sub), re.I) if sub else None
    records, seen_app = [], set()
    for i_, (drug, ev, anchor) in enumerate(entries, 1):
        disease_config.log(a.disease, "indication_records", f"record {i_}/{len(entries)} {drug} setid={ev['setid']} v{ev['version']}")
        if a.drugs and drug not in a.drugs:
            continue
        xml = os.path.join(VCACHE, f"{ev['setid']}_v{ev['version']}.xml")
        if not os.path.exists(xml):
            continue
        app_key = (anchor.get("application"), anchor.get("submission"), ev["setid"], ev["version"])
        if app_key in seen_app:                    # one label = one record set (Vyxeos is listed under both ingredients)
            continue
        seen_app.add(app_key)
        from common import label_parsed, sections_for
        S = sections_for(xml)                       # parsed once per label version, kept in the store
        prods, route = label_parsed(xml)[1:]
        uniis = sorted({i["unii"] for p in prods for i in p["ingredients"]})
        labeled = [xw.ingredient(u, name_hint=next((i["name"] for p in prods for i in p["ingredients"] if i["unii"] == u), None))
                   for u in uniis]
        self_structs = {s for l in labeled for s in l["dc_structs"]}
        combo = fixed_product(xw, prods, labeled, U["trials"])
        inn = xw.in_name(labeled[0]["rxcui"][0]) if labeled and labeled[0]["rxcui"] else drug.lower()
        subs1 = S.get("1", {}).get("subs", [])
        s1 = [(n, t, x) for n, t, x in subs1 if scope.search(strip_paren(t))] or \
             ([("1", "INDICATIONS AND USAGE", S["1"]["text"])] if "1" in S and scope.search(strip_paren(S["1"]["text"])) else [])
        single = bool(s1) and all(scope.search(strip_paren(t)) for n, t, _ in subs1 if re.match(r"\d", n or ""))
        for num, title, text in s1:
            refs = REF.findall(text)
            s2 = [(n, t, x) for n, t, x in S.get("2", {}).get("subs", [])
                  if n in refs or scope.search(t) or (single and RECOMMENDED.search(t))]
            s14 = [(n, t, x) for n, t, x in S.get("14", {}).get("subs", []) if n in refs or scope.search(strip_paren(t))
                   or scope.search(x[:400])]          # body check: Breyanzi titles 14.2 'Chronic Lymphocytic Lymphoma' (label typo)
            if not s14 and "14" in S and single:
                s14 = [("14", "CLINICAL STUDIES", S["14"]["text"])]
            if not s2 and "2" in S and single:
                s2 = [("2", "DOSAGE AND ADMINISTRATION", S["2"]["text"])]
            dose_text = " ".join(x for n, t, x in s2 if DOSE.search(x))
            s14_text = " ".join(x for _, _, x in s14)
            settings = [k for k, r in cfg["_settings"] if r.search(text)]
            blocks = setting_blocks(dose_text, cfg) if dose_text else []
            dose_block = " ".join(b for st, b in blocks if st is None or not settings or st & set(settings)) if blocks else ""
            ind_ph = code_partners(xw, phase_regimens(text, vocab, self_structs, cfg), cfg)
            dose_ph = code_partners(xw, phase_regimens(dose_block, vocab, self_structs, cfg), cfg) if dose_block else {}
            source = "section 1"
            if all(not d["alternatives"] for d in ind_ph.values()) and any(d["alternatives"] for d in dose_ph.values()):
                ind_ph, source = dict(dose_ph), "section 2 dosing block for the same setting (section 1 names no partner)"
                if cfg["_mono"].search(dose_block) and "treatment" not in ind_ph:
                    ind_ph["treatment"] = dict(alternatives=[], days=[], monotherapy=True)
            base = sorted({r for l in labeled for r in l["rxcui"]})
            prod_tok = [combo["mesh"]] if combo and combo.get("mesh") else []
            keys = {}
            for ph, d in ind_ph.items():
                alts = d["alternatives"] or [[]]
                keys[ph] = [key(base + [r for p in alt for r in p.get("rxcui", [])], prod_tok,
                                [x for p in alt for x in p.get("atc", [])]) for alt in alts]
            role = next((k for k, r in cfg["_roles"] if r.search(text)), "disease_treatment")
            if subtype_rx and subtype_rx.search(title if num != "1" else text[:300]):
                role = "excluded_subtype"           # curated: a MONDO descendant handled as its own disease (APL for AML)
            dm = DOSE.search(dose_block or dose_text)
            records.append(dict(
                record_id=f"{drug}:{anchor.get('application')}:{anchor.get('submission')}:{num}",
                drug=drug, disease=dict(mondo=cfg["scope_id"], mesh=cfg["primary_mesh"], heading=heading, config=a.disease),
                labeled_ingredients=labeled, fixed_combination=combo, product=prods, route=route,
                indication=dict(section=num, title=title, text=text, setting=settings,
                                biomarker=[b for b in cfg["biomarkers"] if re.search(r"(?<![\w-])" + re.escape(b) + r"(?![A-Za-z0-9])", text)],
                                chemo_eligibility=eligibility(text, ind_ph, xw, cfg), role=role,
                                excluded=cfg["excluded_roles"].get(role), cross_refs=refs),
                phases=ind_ph, regimen_source=source, regimen_keys=keys,
                dosing=dict(sections=[f"{n} {t}" for n, t, _ in s2], text=(dose_block or dose_text)[:1500],
                            dose=f"{dm.group(1)} {dm.group(2)}" if dm else None,
                            route=(ROUTE.search(dose_block) or [None])[0] if dose_block else None,
                            frequency=(FREQ.search(dose_block) or [None])[0] if dose_block else None,
                            phases=dose_ph),
                studies=dict(sections=[f"{n} {t}" for n, t, _ in s14], ncts=sorted(set(NCT.findall(s14_text))),
                             study_names=sorted(set(STUDY.findall(s14_text))),
                             trial_names=sorted({t for t in TRIAL_NAME.findall(s14_text)
                                                 if (len(t) >= 5 or re.search(r"[0-9]", t)) and t not in cfg["label_title_extra_terms"]}),
                             enrolled=[int(c.replace(",", "")) for c in COUNT.findall(s14_text)][:3],
                             design=[w for w in cfg["design_words"] if w in s14_text.lower()]),
                label=dict(setid=ev["setid"], version=ev["version"], published=ev.get("published"),
                           effective_time=ev.get("effective_time"), zip_url=ev.get("zip_url")),
                approval=dict(fda=dict(application=anchor.get("application"), submission=anchor.get("submission"),
                                       date=anchor.get("approval_date"), original_date=anchor.get("original_date"),
                                       in_first_version=anchor.get("in_first_version"), history_of=anchor.get("history_of"),
                                       kind=anchor.get("kind"), anchor_nct=anchor.get("nct"),
                                       anchor_ids=anchor.get("ids", []), anchor_sources=anchor.get("sources", [])),
                              ema=ema_row(ema, inn, heading)),
                pubmed=pubmed(anchor["nct"]) if anchor.get("nct") else None))
    # follow-on products: same regimen key as an earlier-approved product (biosimilar, 505(b)(2) reformulation, second NDA
    # of the same ingredient). The earliest original approval is the reference; the rest are supplemental.
    groups = {}
    for r in records:
        if r["indication"]["excluded"]:
            continue
        k = (tuple(sorted(x for l in r["labeled_ingredients"] for x in l["rxcui"])), (r.get("fixed_combination") or {}).get("mesh"),
             tuple(sorted(r["indication"]["setting"])))
        groups.setdefault(k, []).append(r)
    for k, rs in groups.items():
        if len(rs) < 2:
            continue
        rs.sort(key=lambda r: (r["approval"]["fda"].get("original_date") or r["approval"]["fda"].get("date") or "9999"))
        for r in rs[1:]:
            if r["approval"]["fda"]["application"] == rs[0]["approval"]["fda"]["application"]:
                continue                            # same application = approval history of the reference, not a follow-on
            r["indication"]["role"] = "follow_on_product"
            r["indication"]["excluded"] = cfg["excluded_roles"].get("follow_on_product")
            r["indication"]["reference_record"] = rs[0]["record_id"]
    json.dump(dict(disease=a.disease, source=src, generated_from=["label_evidence.json + central_anchor.json" if src == "legacy" else "scope_labels.json", "id_crosswalk.json.gz",
                                                      "rxnav + mesh caches", "EMA medicines.json", "pubmed cache",
                                                      f"config/diseases/{a.disease}.json"], records=records),
              open(os.path.join(ddir, "indication_records.json"), "w", encoding="utf-8"), indent=1)
    print(f"{len(records)} indication records -> {os.path.join(ddir, 'indication_records.json')}")
    for r in records:
        i = r["indication"]
        print(f"  {r['record_id']:40s} {i['role'][:12]:12s} {i['chemo_eligibility']['value'][:16]:16s} {i['setting']} {i['biomarker']} {r['regimen_keys']}")


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
    INP = [os.path.join(D, "scope_labels.json"), os.path.join(D, "mesh_universe.json.gz"), os.path.join(R, "data", "id_crosswalk.json.gz"), os.path.join(B, "inputs", "ema", "medicines.json")] + ([os.path.join(R, "data", "label_evidence.json"), os.path.join(R, "data", "central_anchor.json")] if a.disease == "aml" else [])
    OUT = [_os.path.join(D, x) for x in ["indication_records.json"]]
    CODE = ["indication_records.py", "regimen_key.py", "common.py", "disease_config.py", "fidelity.py"]
    if a.trace or "show" in _sys.argv or "pull" in _sys.argv or "--help" in _sys.argv:
        main()
    elif not common.stage_current(a.disease, "indication_records", INP, OUT, CODE, a.force):
        main()
        common.stage_done(a.disease, "indication_records", INP, OUT, CODE)
