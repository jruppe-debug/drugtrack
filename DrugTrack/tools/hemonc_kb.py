"""HemOncKB (OMOP stage files + additional tables) as a logic and validation layer for the AML treatment finder.

Reads data/references/hemonc/ (not committed: HemOncKB is CC BY-NC-SA). Stdlib csv only.

What it does, per finder regimen (tree["finder"] from tools/aml_situations.py):
  1. Parses the regimen name into HemOnc Component sets (shorthand like 7+3, HMA, CPX-351 is expanded; '+/-' gives
     both sets). Non-drug steps (SCT, clinical trial, response assessment) get no match.
  2. Matches a HemOnc Regimen by component set among regimens linked to AML (accepted use, current or historical
     in adult, or studied in an AML study). Exact set first, then the smallest superset; ties broken by context.
  3. Attaches HemOnc contexts, studies (with NCT IDs) and study_results rows for AML.
  4. Flags each component as previously FDA-approved (in AML or any disease) before this regimen's FDA approval
     date. These are the drugs shown with an asterisk: an approved drug re-tested in a new combination.
  5. Validates finder statuses and REGIMEN_RULES against HemOnc FDA AML indications and returns an issue list.

CLI: python tools/hemonc_kb.py   (reads data/tree.json, prints the validation report, writes data/hemonc_validation.json)
"""
import csv, glob, json, os, re, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
HEMONC = os.path.join(DATA, "references", "hemonc")
TABLES = os.path.join(HEMONC, "additional", "Tables")
OUT = os.path.join(DATA, "hemonc_validation.json")
AML = "552"
OLD = "1900-01-01"          # 'Uncertain date' FDA rows: old drugs approved before HemOnc tracks dates
TODAY = "9999-12-31"        # comparison date for regimens with no FDA approval
# 'Novel drug' = first FDA approval in any disease on or after this date, or never FDA-approved. 2015 keeps venetoclax
# (CLL 2016) and the 2017+ AML targeted agents; gemtuzumab ozogamicin (2000, withdrawn 2010, re-approved 2017) counts as
# established. New formulations that HemOnc lists as separate components (oral azacitidine, decitabine/cedazuridine) count as novel.
NOVEL_SINCE = "2015-01-01"

COMPONENT_RELS = {"Has cytotoxic chemo", "Has targeted therapy", "Has antineoplastic", "Has immunotherapy",
                  "Has AB-drug cjgt", "Has endocrine therapy", "Has radioconjugate", "Has CAR T-cell tx",
                  "Has supportive med", "Has growth factor"}
KEEP_RELS = COMPONENT_RELS | {"Has context", "Has accepted use", "Is current in adult", "Is historical in adult",
                              "Was studied in", "Has intent", "Has setting"}
# Cytoreduction given before some regimens (hydroxyurea in 7+3d and Midostaurin); not counted as a regimen difference.
IGNORE_EXTRA = {"hydroxyurea"}
ANTHRACYCLINES = {"daunorubicin", "idarubicin", "doxorubicin", "mitoxantrone"}
AML_LINK = {"Has accepted use", "Is current in adult", "Is historical in adult"}

# Finder shorthand -> component names (alternatives separated by '|' give one set each).
SHORTHAND = [
    (r"\b7\s*\+\s*3\b(?:\s*\([^)]*\))?", "cytarabine,daunorubicin|cytarabine,idarubicin"),
    (r"\bHMA\b", "azacitidine|decitabine"),
    (r"\bCPX-351\b(?:\s*\([^)]*\))?", "cytarabine and daunorubicin liposomal"),
    (r"\boral azacitidine\b(?:\s*\([^)]*\))?|\bCC-486\b", "azacitidine oral"),
    (r"\boral decitabine/cedazuridine\b|\bdecitabine/cedazuridine\b", "decitabine and cedazuridine"),
    (r"\bFLAG-IDA\b", "fludarabine,cytarabine,idarubicin,filgrastim"),
    (r"\bMEC\b(?:\s*\([^)]*\))?", "mitoxantrone,etoposide,cytarabine"),
    (r"\b(?:low|high)-dose cytarabine\b", "cytarabine"),
    (r"\bFLT3 inhibitor\b", "class:FLT3 inhibitor"),
]
NON_SACT = re.compile(r"^(?:allogeneic sct|clinical trial|refractory:|consolidation chemotherapy$|continue the same)", re.I)

# Finder phase context -> words expected in HemOnc 'Has context' names (scored by overlap).
CONTEXT_WORDS = [
    (r"intensive induction", ["first-line induction", "induction"]),
    (r"lower-intensity", ["older or unfit", "non-curative first-line", "first-line"]),
    (r"continued first-line", ["older or unfit", "non-curative first-line", "first-line"]),
    (r"consolidation", ["consolidation"]),
    (r"maintenance", ["maintenance"]),
    (r"subsequent|relapsed|refractory", ["subsequent line", "relapsed", "refractory", "salvage"]),
    (r"hsct|conditioning", ["conditioning", "allogeneic"]),
    (r"response assessment", ["mrd", "maintenance"]),
]
# HemOnc FDA biomarker text -> finder marker keys it allows.
BIOMARKERS = [(r"FLT3 ITD", {"flt3_itd"}), (r"FLT3 mutated", {"flt3_itd", "flt3_tkd"}), (r"IDH1", {"idh1"}),
              (r"IDH2", {"idh2"}), (r"NPM1", {"npm1"}), (r"KMT2A", {"kmt2a"}), (r"CD33", {"cd33"})]


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def stage(pat):
    f = glob.glob(os.path.join(HEMONC, pat))
    if not f:
        raise SystemExit(f"missing HemOnc file {pat} in {HEMONC}")
    return f[0]


def table(name):
    with open(os.path.join(TABLES, name + ".csv"), encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


class KB:
    def __init__(self):
        with open(stage("*_concept_stage.csv"), encoding="utf-8", newline="") as fh:
            self.c = {r["concept_code"]: (r["concept_name"], r["concept_class_id"])
                      for r in csv.DictReader(fh) if r["vocabulary_id"] == "HemOnc"}
        self.rel = defaultdict(list)                       # code -> [(relationship_id, code2)]
        with open(stage("*_concept_relationship_stage.csv"), encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                if r["relationship_id"] in KEEP_RELS and not r["invalid_reason"]:
                    self.rel[r["concept_code_1"]].append((r["relationship_id"], r["concept_code_2"]))
        # Component lexicon: names and synonyms (>= 4 chars, to keep short codes from matching words).
        self.lex = {}
        for k, (n, cls) in self.c.items():
            if cls == "Component":
                self.lex.setdefault(norm(n), k)
        with open(stage("*_concept_synonym_stage.csv"), encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                k = r["synonym_concept_code"]
                if self.c.get(k, ("", ""))[1] == "Component" and len(r["synonym_name"]) >= 4 and not r["invalid_reason"]:
                    self.lex.setdefault(norm(r["synonym_name"]), k)
        self.studies = {r["study_cui"]: r for r in table("studies")}
        self.drugs = {r["drug_CUI"]: r for r in table("drugs")}
        self.results = defaultdict(list)
        for r in table("study_results"):
            if r["condition_cui"] == AML:
                self.results[r["regimen_cui"]].append(r)
        self.fda = [r for r in table("indications") if r["regulator"] == "FDA"]
        self.first_fda = {}                                 # component code -> earliest FDA date, any disease
        self.first_fda_aml = {}
        for r in self.fda:
            d = OLD if not re.match(r"\d{4}-", r["date"]) else r["date"]
            k = r["component_cui"]
            self.first_fda[k] = min(self.first_fda.get(k, TODAY), d)
            if r["condition_cui"] == AML:
                self.first_fda_aml[k] = min(self.first_fda_aml.get(k, TODAY), d)
        self.aml_fda = [r for r in self.fda if r["condition_cui"] == AML]
        self.aml_regimens = self._aml_regimens()

    def is_novel(self, k):
        return self.first_fda.get(k, TODAY) >= NOVEL_SINCE

    def name(self, k):
        return self.c.get(k, (k, ""))[0]

    def components(self, k):
        return frozenset(b for rel, b in self.rel.get(k, []) if rel in COMPONENT_RELS and self.c.get(b, ("", ""))[1] == "Component")

    def _aml_regimens(self):
        aml_studies = {k for k, s in self.studies.items() if s["condition_cui"] == AML}
        out = {}
        for k, (n, cls) in self.c.items():
            if cls not in ("Regimen", "Synthetic Regimen"):
                continue
            rs = self.rel.get(k, [])
            if any(rel in AML_LINK and b == AML for rel, b in rs) or any(rel == "Was studied in" and b in aml_studies for rel, b in rs):
                comps = self.components(k)
                if comps:
                    out[k] = comps
        return out

    # ---- finder name -> component sets ----
    def parse(self, name):
        """Regimen name -> (list of component-code sets, unresolved terms). '+/-' X gives sets with and without X."""
        s = re.sub(r"^(?:continue|consolidation|maintenance|mrd-positive cr):\s*", "", name, flags=re.I)
        if NON_SACT.search(s):
            return [], []
        alts, unresolved, optional = [set()], [], []
        for pat, exp in SHORTHAND:
            m = re.search(pat, s, re.I)
            if not m:
                continue
            opt = bool(re.search(r"\+/-\s*$", s[:m.start()]))
            s = s[:m.start()] + " ; " + s[m.end():]
            sets = []
            for alt in exp.split("|"):
                codes = set()
                for t in alt.split(","):
                    if t.startswith("class:"):
                        unresolved.append(t)
                    elif norm(t) in self.lex:
                        codes.add(self.lex[norm(t)])
                    else:
                        unresolved.append(t)
                sets.append(codes)
            if opt:
                optional.append(sets[0])
            else:
                alts = [a | b for a in alts for b in sets]
        s = re.sub(r"\([^)]*\)", " ", s)               # parentheses only restate components
        words = norm(s).split()
        i = 0
        while i < len(words):                          # longest lexicon match from each position
            for j in range(min(len(words), i + 5), i, -1):
                k = self.lex.get(" ".join(words[i:j]))
                if k:
                    opt = bool(re.search(r"\+/-\s*" + re.escape(words[i]), s, re.I))
                    (optional.append({k}) if opt else [a.add(k) for a in alts])
                    i = j
                    break
            else:
                i += 1
        for o in optional:
            alts = alts + [a | o for a in alts]
        return [frozenset(a) for a in alts if a], unresolved

    def context_score(self, k, finder_ctx):
        names = [self.name(b).lower() for rel, b in self.rel.get(k, []) if rel == "Has context"]
        words = next((w for p, w in CONTEXT_WORDS if re.search(p, finder_ctx or "", re.I)), [])
        return sum(any(w in n for n in names) for w in words), names

    def match(self, sets, finder_ctx):
        """Best HemOnc regimen for any of the component sets. Exact set beats superset, then the larger finder set
        ('+/- X' prefers the set with X), then context, then fewer extras. A one-drug set must match exactly."""
        best = None
        for s in sets:
            for k, comps in self.aml_regimens.items():
                if not s <= comps:
                    continue
                extra = {c for c in comps - s if norm(self.name(c)) not in IGNORE_EXTRA}
                if len(s) == 1 and extra:
                    continue
                score, ctx = self.context_score(k, finder_ctx)
                key = (not extra, len(s), score, -len(extra), -int(k) if k.isdigit() else 0)
                if best is None or key > best[0]:
                    best = (key, k, s, ctx)
        if not best:
            return None
        (exact, _, score, extra, _), k, s, ctx = best
        return dict(code=k, name=self.name(k), match="exact" if exact else f"superset (+{-extra} components)",
                    context_score=score, contexts=ctx, components=sorted(s, key=self.name))

    def regimen_studies(self, k):
        out = []
        for rel, b in self.rel.get(k, []):
            if rel == "Was studied in" and b in self.studies:
                st = self.studies[b]
                if st["condition_cui"] == AML:
                    out.append(dict(study=st["study"], nct=st["trial_id"] or None, phase=st["phase"], sponsor=st["sponsor"]))
        return out

    def regimen_results(self, k, limit=8):
        rows = [r for r in self.results.get(k, []) if r["endpoint"] and r["statistic"] != "Not applicable"]
        rank = {"OS": 0, "EFS": 1, "CR rate": 2}
        rows.sort(key=lambda r: (rank.get(r["endpoint"], 5), r["study"]))
        nct = {s["study_cui"]: s["trial_id"] for s in self.studies.values()}
        return [dict(study=r["study"], nct=nct.get(r["study_cui"]) or None, endpoint=r["endpoint"], comparator=r["comparator"],
                     metric=r["metric"], this_arm=r["metric_num_this_arm"], that_arm=r["metric_num_that_arm"], unit=r["metric_unit"],
                     statistic=r["statistic"], estimate=r["estimate"], lo=r["est_lb"], hi=r["est_ub"], p=r["p_value"],
                     context=r["context"]) for r in rows[:limit]]

    def fda_for(self, k, comps):
        """FDA AML indication rows for this regimen: named by regimen_cui; a monotherapy row of its single component; or a
        combination row ('with' = 'Cytarabine AND Anthracycline', regimen_cui 'Hybrid regimen CBD'/'TBA') whose partners
        are all in the regimen."""
        names = {norm(self.name(c)) for c in comps}
        def partners_ok(w):
            if re.search(r"other approved", w, re.I):
                return False
            for part in re.split(r"\s+AND\s+", w):
                alts = [norm(x) for x in part.split("|")]
                if not any(a in names or (a == "anthracycline" and names & ANTHRACYCLINES) or (a == "lodac" and "cytarabine" in names)
                           for a in alts):
                    return False
            return True
        out = []
        for r in self.aml_fda:
            w = r["with"].strip()
            if k in r["regimen_cui"].split("|"):
                out.append(r)
            elif r["component_cui"] in comps and not r["regimen_cui"].split("|")[0].isdigit():
                if (len(comps) == 1 and w in ("", "0")) or (len(comps) > 1 and w not in ("", "0") and partners_ok(w)):
                    out.append(r)
        return out


def fda_row(r):
    return dict(component=r["component"], date=r["date"], with_=r["with"], regimen=r["regimen"], context=r["context"],
                biomarker=r["biomarker"], accelerated=r["accelerated"] == "TRUE", withdrawn=r["withdrawn"] not in ("FALSE", ""),
                first_in_class=r["first_in_class"] == "TRUE", age=r["age"])


def annotate(fd, kb=None):
    """Adds r['hemonc'] to every finder regimen, fd['drug_census'] and fd['hemonc_validation']. Returns the issue list."""
    kb = kb or KB()
    from aml_situations import REGIMEN_RULES, GROUP_RULES
    issues, census = [], defaultdict(lambda: dict(regimens=set(), populations=set(), appearances=0, new_in=set(), asterisk_in=set(), dagger_in=set()))
    cache = {}
    for pk, pop in fd["populations"].items():
        for gname, g in pop["groups"].items():
            for ph in g["phases"]:
                for r in ph["regimens"]:
                    ck = (r["name"], ph["context"])
                    if ck not in cache:
                        cache[ck] = map_regimen(kb, r, ph["context"])
                    h = r["hemonc"] = cache[ck]
                    for c in h.get("component_flags", []):
                        cs = census[c["name"]]
                        cs["regimens"].add(r["name"]); cs["populations"].add(pk); cs["appearances"] += 1
                        cs[{"*": "asterisk_in", "†": "dagger_in"}.get(c["mark"], "new_in")].add(r["name"])
                    issues += validate(kb, r, h, pk, gname, ph, GROUP_RULES, REGIMEN_RULES)
    seen, uniq = set(), []
    for i in issues:
        key = (i["regimen"], i["check"], i["detail"])
        if key not in seen:
            seen.add(key); uniq.append(i)
    rows = []
    for name, cs in census.items():
        code = kb.lex.get(norm(name))
        rows.append(dict(name=name, code=code, investigational=(kb.drugs.get(code) or {}).get("investigational") == "TRUE",
                         first_fda_any=kb.first_fda.get(code), first_fda_aml=kb.first_fda_aml.get(code),
                         fda_aml_indications=sum(1 for x in kb.aml_fda if x["component_cui"] == code),
                         finder_regimens=len(cs["regimens"]), populations=len(cs["populations"]), appearances=cs["appearances"],
                         new_in=sorted(cs["new_in"]), asterisk_in=sorted(cs["asterisk_in"]), dagger_in=sorted(cs["dagger_in"]),
                         novel=kb.is_novel(code)))
    rows.sort(key=lambda x: (-x["appearances"], x["name"]))
    fd["drug_census"] = rows
    fd["hemonc_validation"] = uniq
    fd["logic"]["hemonc_import"] = (f"HemOncKB OMOP stage + additional tables ({os.path.basename(stage('*_concept_stage.csv'))[:10]}); "
                                    f"{len(kb.aml_regimens)} AML-linked regimens; matched by component set + context; CC BY-NC-SA, local only")
    return uniq


def map_regimen(kb, r, ctx):
    sets, unresolved = kb.parse(r["name"])
    if not sets:
        return dict(match="none", reason="not a drug regimen" if not unresolved else "unresolved: " + ", ".join(unresolved))
    m = kb.match(sets, ctx)
    comps = set(m["components"]) if m else set(sets[0])
    fda = kb.fda_for(m["code"], comps) if m else []
    approved_on = min((x["date"] for x in fda if re.match(r"\d{4}-", x["date"])), default=None)
    ref = approved_on or TODAY
    flags = []
    for c in sorted(comps, key=kb.name):
        # '*' = FDA-approved in AML before this regimen's first FDA AML approval (or at all, for an unapproved regimen):
        # an existing AML drug re-used in a new combination. '+' = approved before only in another disease.
        prev_aml = kb.first_fda_aml.get(c, TODAY) < ref
        prev_any = kb.first_fda.get(c, TODAY) < ref
        flags.append(dict(name=kb.name(c).upper(), code=c, previously_approved=prev_aml,
                          mark="*" if prev_aml else ("†" if prev_any else ""),
                          previously_approved_in="AML" if prev_aml else ("other disease" if prev_any else None),
                          first_fda_any=kb.first_fda.get(c), first_fda_aml=kb.first_fda_aml.get(c), novel=kb.is_novel(c),
                          investigational=(kb.drugs.get(c) or {}).get("investigational") == "TRUE"))
    out = dict(match="unmatched", parsed=[sorted(kb.name(c) for c in s) for s in sets], unresolved=unresolved,
               component_flags=flags, fda=[fda_row(x) for x in fda], fda_approved_on=approved_on)
    if m:
        out.update(m, components=[kb.name(c) for c in m["components"]], studies=kb.regimen_studies(m["code"]),
                   results=kb.regimen_results(m["code"]))
    return out


def validate(kb, r, h, pk, gname, ph, group_rules, regimen_rules):
    iss = []
    def add(check, detail, sev="review"):
        iss.append(dict(regimen=r["name"], population=pk, group=gname, phase=ph["phase"], status=r["status"],
                        check=check, detail=detail, severity=sev))
    if h["match"] == "none":
        return iss
    if h["match"] == "unmatched":
        add("hemonc_match", f"no AML-linked HemOnc regimen contains {h['parsed']}" + (f"; unresolved {h['unresolved']}" if h["unresolved"] else ""))
    fda = h.get("fda") or []
    current = [x for x in fda if not x["withdrawn"]]
    if r["status"] == "approved" and not current:
        add("fda_status", "finder says approved; HemOnc has no current FDA AML indication for this regimen "
                          "(KB snapshot may predate the approval, or the label is outside the AML condition)", "high")
    if r["status"] != "approved" and current and not r["name"].lower().startswith(("mrd-positive",)):
        add("fda_status", f"finder says {r['status']}; HemOnc lists FDA AML approval "
                          f"{sorted({x['date'] for x in current})} for {h.get('name')}", "high")
    if any(x["accelerated"] for x in current) and r["status"] == "approved":
        if not any(not x["accelerated"] for x in current):
            add("fda_status", "approval is accelerated only in HemOnc", "info")
    allowed = set()
    for x in current:
        for pat, keys in BIOMARKERS:
            if re.search(pat, x["biomarker"] or "", re.I):
                allowed |= keys
    if allowed and r["status"] == "approved":
        markers = set((group_rules.get(gname) or {}).get("any", [])) | set((r.get("requires") or {}).get("any", []))
        req = r.get("requires") or {}
        if "cd33" in allowed and not req.get("cd33_not"):
            add("biomarker", "FDA indication requires CD33-positive; finder has no CD33 rule", "high")
        mk = allowed - {"cd33"}
        if mk and not markers:
            add("biomarker", f"FDA indication requires {sorted(mk)}; finder shows it without a marker rule (group '{gname}')", "high")
        elif mk and not markers <= mk:
            add("biomarker", f"finder markers {sorted(markers)} wider than FDA {sorted(mk)}", "high")
    return iss


def main():
    tree = json.load(open(os.path.join(DATA, "tree.json"), encoding="utf-8"))
    if "finder" not in tree:
        raise SystemExit("no finder in data/tree.json; run reattach first")
    fd = tree["finder"]
    issues = annotate(fd)
    json.dump(dict(issues=issues, drug_census=fd["drug_census"]), open(OUT, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    regs = {}
    for p in fd["populations"].values():
        for g in p["groups"].values():
            for ph in g["phases"]:
                for r in ph["regimens"]:
                    regs[(r["name"], ph["context"])] = r["hemonc"]
    print(f"{len(regs)} regimen/context pairs")
    for (n, ctx), h in regs.items():
        flags = " ".join(c["name"].lower() + ("*" if c["previously_approved"] else "") for c in h.get("component_flags", []))
        print(f"  {h['match'][:22]:22} {n[:44]:44} -> {h.get('name', '')[:34]:34} [{flags}] ctx={ctx[:28]}")
    print(f"\n{len(issues)} validation issues")
    for i in issues:
        print(f"  [{i['severity']}] {i['check']}: {i['regimen']} ({i['population']} / {i['group'][:24]}): {i['detail']}")
    print("\ndrug census (appearances, finder regimens, populations, FDA AML indications, new-in / asterisk-in):")
    for d in fd["drug_census"]:
        print(f"  {d['name'][:30]:30} {d['appearances']:3} {d['finder_regimens']:3} {d['populations']:3} {d['fda_aml_indications']:3} "
              f"novel={d['novel']} new_in={len(d['new_in'])} asterisk_in={len(d['asterisk_in'])} dagger_in={len(d['dagger_in'])}")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
