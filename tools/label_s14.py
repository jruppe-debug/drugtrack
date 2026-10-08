"""Label section 14 (Clinical Studies) as the source of truth for which study supports which AML regimen.

For every AML approval event in data/central_anchor.json, read the event's cached label text and:
  1. Cut section 14 (the body heading, not the table of contents) up to section 15/16.
  2. Keep the AML part: the 14.x subsections whose heading names AML, or the whole section when it has no subsections
     and the label is AML-only.
  3. Split that part into study blocks. A block starts at each NCT number, 'Study <ID>' heading or curated alias
     (data/curated/approval_lineage.json) and runs to the next one.
  4. Per block: the regimen alternatives named in its 'in combination with A or B' clause (each alternative is its own
     regimen, so venetoclax + azacitidine or decitabine gives two), and population tags (newly diagnosed vs relapsed,
     unfit/age >=75, FLT3 / IDH1 / IDH2 / KMT2A / NPM1 / CD33, maintenance, consolidation, secondary AML, APL, pediatric).
  5. Lead per regimen: the block's trial is the lead for each regimen it names. A regimen named by two blocks keeps the
     block whose trial has the higher phase as lead; the other is co_basis.

Output: data/label_s14.json. Read by tools/export_disease_tree.py for the decision-tree view.
"""
import json, os, re, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from approval_anchor import DATA, ctgov, fetch_doc
from central_anchor import PHASE_RANK, SCOPE_RX

OUT = os.environ.get("LABEL_S14_OUT") or os.path.join(DATA, "label_s14.json")
LINEAGE = os.path.join(DATA, "curated", "approval_lineage.json")

S14_RX = re.compile(r"^\s*14\s+CLINICAL STUDIES\s*$", re.M | re.I)
END_RX = re.compile(r"^\s*1[56]\s+(?:REFERENCES|HOW SUPPLIED)", re.M | re.I)
# [ \t] not \s: a table cell '14.7' followed by a newline must not read as a subsection heading.
SUB_RX = re.compile(r"^[ \t]*14\.(\d+)[ \t]+(\S.*)$", re.M)
NCT_RX = re.compile(r"NCT\d{8}")
# 'Study M14-358' or a bare 'M14-358' heading line (later labels drop the word Study).
STUDY_HEAD_RX = re.compile(r"^[ \t]*(?:(?:Study|Trial)[ \t]+)?([A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+)[ \t]*$", re.M)
COMBO_RX = re.compile(r"in combination with\s+(.{3,260}?)(?=\s+(?:in|for|among)\s+(?:adult\s+)?(?:patients|subjects)|\s+(?:was|were|is|versus|vs\.?|compared)\s|\.\s|;)", re.I | re.S)

# Partner names as labels write them -> canonical regimen component.
PARTNERS = [
    (r"low[- ]dose cytarabine|\bLDAC\b", "LOW-DOSE CYTARABINE"),
    (r"azacitidine", "AZACITIDINE"), (r"decitabine", "DECITABINE"),
    (r"daunorubicin", "DAUNORUBICIN"), (r"idarubicin", "IDARUBICIN"),
    (r"high[- ]dose cytarabine|\bHiDAC\b", "HIGH-DOSE CYTARABINE"),
    (r"(?:standard|intensive)\s+(?:cytarabine and (?:daunorubicin|anthracycline)\s+)?induction(?: and consolidation)?(?: chemotherapy)?|"
     r"cytarabine and (?:daunorubicin|anthracycline)|induction (?:and consolidation )?chemotherapy", "INTENSIVE CHEMOTHERAPY"),
    (r"(?<!low dose )(?<!low-dose )cytarabine", "CYTARABINE"),
    (r"tretinoin|all-trans retinoic", "TRETINOIN"), (r"arsenic trioxide", "ARSENIC TRIOXIDE"),
]
# Bare 'chemotherapy' (no named agents) in an AML label means intensive induction chemotherapy.
BARE_CHEMO_RX = re.compile(r"\b(?:intensive\s+)?chemotherapy\b", re.I)
POP = [
    ("newly_diagnosed", r"newly[- ]diagnosed|previously untreated|de novo|treatment[- ]na[iï]ve"),
    ("relapsed_refractory", r"relapsed|refractory"),
    ("unfit_or_75plus", r"(?:≥|>=|aged? )\s*75|precluded? the use of intensive|ineligible for intensive|not (?:eligible|candidates) for intensive|comorbidit"),
    ("FLT3", r"\bFLT3\b"), ("FLT3-ITD", r"FLT3[- ]ITD"), ("IDH1", r"\bIDH1\b"), ("IDH2", r"\bIDH2\b"),
    ("KMT2A", r"\bKMT2A\b|\bMLL\b"), ("NPM1", r"\bNPM1\b"), ("CD33", r"\bCD33\b"),
    ("maintenance", r"maintenance|continued treatment following"), ("consolidation", r"consolidation"),
    ("secondary_aml", r"therapy[- ]related|t-AML|myelodysplasia[- ]related|AML-MRC|secondary AML|antecedent"),
    ("APL", r"promyelocytic|\bAPL\b"),
    ("pediatric", r"pediatric|children|aged? (?:0|1|2) (?:month|year|to)|ages? 0 to|1 month (?:of age )?and older"),
]


def section14(txt):
    """Body of section 14: from the last '14 CLINICAL STUDIES' heading (the first is the table of contents) to section 15/16."""
    heads = list(S14_RX.finditer(txt))
    if not heads:
        return ""
    start = heads[-1].end()
    end = END_RX.search(txt, start)
    return txt[start:end.start() if end else len(txt)]


def aml_part(s14, whole_label):
    subs = list(SUB_RX.finditer(s14))
    if not subs:
        # No subsections: whole section counts only when the label's indications are AML-only (else ambiguous).
        return [("14", s14)] if SCOPE_RX.search(whole_label[:6000]) else []
    out = []
    for i, m in enumerate(subs):
        body = s14[m.end():subs[i + 1].start() if i + 1 < len(subs) else len(s14)]
        if SCOPE_RX.search(m.group(2)):
            out.append((f"14.{m.group(1)} {m.group(2).strip()}", body))
    return out


def regimen_alternatives(block, drug, lead_in=""):
    """'in combination with A (N=84) or B (N=31)' -> [[drug, A], [drug, B]]. None named -> [[drug]] (monotherapy).
    lead_in: the text just before the block's first identifier ('X in combination with Y was evaluated in Study Z')."""
    # Only the lead-in's last sentence counts: earlier sentences belong to the previous study.
    tail = re.split(r"\.\s", lead_in)[-1]
    # Short form in the study's first sentences: 'evaluated the efficacy of VEN+LDAC (N=82)'.
    ab = re.search(r"\b[A-Z]{2,5}\s*\+\s*(LDAC|AZA|DEC)\b", block[:400])
    if ab and not COMBO_RX.search(tail):
        name = {"LDAC": "LOW-DOSE CYTARABINE", "AZA": "AZACITIDINE", "DEC": "DECITABINE"}[ab.group(1)]
        return [sorted({drug, name})], "combination"
    m = COMBO_RX.search(tail) or COMBO_RX.search(block[:1500])
    if not m:
        return [[drug]], "monotherapy"
    clause = re.sub(r"\(N\s*=\s*\d+\)|\s+", " ", m.group(1))
    alts = []
    for part in re.split(r"\s+or\s+|,\s*or\s+", clause):
        found = []
        for rx, name in PARTNERS:
            if re.search(rx, part, re.I) and not (name == "CYTARABINE" and any(f in found for f in ("LOW-DOSE CYTARABINE", "HIGH-DOSE CYTARABINE", "INTENSIVE CHEMOTHERAPY"))):
                found.append(name)
        # Named anthracycline + cytarabine is the 7+3 backbone: one INTENSIVE CHEMOTHERAPY component.
        if {"DAUNORUBICIN", "CYTARABINE"} <= set(found) or {"IDARUBICIN", "CYTARABINE"} <= set(found):
            found = [f for f in found if f not in ("DAUNORUBICIN", "IDARUBICIN", "CYTARABINE")] + ["INTENSIVE CHEMOTHERAPY"]
        if not found and BARE_CHEMO_RX.search(part):
            found = ["INTENSIVE CHEMOTHERAPY"]
        if found:
            alts.append(sorted({drug, *found}))
    return (alts or [[drug]]), ("combination" if alts else "monotherapy")


def blocks(part, tok2nct):
    """Split the AML part at every trial identifier. Returns [(tokens, strong, lead_in, text)].
    strong: the cluster holds an NCT number or a study heading (a bare alias mention, e.g. 'in VIALE-A' under a
    figure, is weak). lead_in: text between the previous cluster and this one, at most 600 chars."""
    marks = []
    for m in NCT_RX.finditer(part):
        marks.append((m.start(), m.group(0), True))
    for m in STUDY_HEAD_RX.finditer(part):
        marks.append((m.start(), m.group(1), True))
    for tok in tok2nct:
        if tok.startswith("NCT"):
            continue
        for m in re.finditer(rf"(?<![A-Za-z0-9]){re.escape(tok)}(?![A-Za-z0-9])", part):
            marks.append((m.start(), tok, False))
    marks.sort()
    # Marks within 300 chars are one study unless they name two different NCTs (AAML0531 next to AML-19).
    out, last = [], -10_000
    for pos, tok, strong in marks:
        n = tok if tok.startswith("NCT") else tok2nct.get(tok)
        c = out[-1] if out else None
        if c and pos - last < 300 and (n is None or not c["ncts"] or n in c["ncts"]):
            c["toks"].append(tok)
            c["strong"] |= strong
        else:
            out.append(dict(pos=pos, toks=[tok], strong=strong, ncts=set()))
        if n:
            out[-1]["ncts"].add(n)
        last = pos
    res = []
    for i, c in enumerate(out):
        end = out[i + 1]["pos"] if i + 1 < len(out) else len(part)
        prev = out[i - 1]["pos"] + 1 if i else 0
        res.append((c["toks"], c["strong"], part[max(prev, c["pos"] - 600):c["pos"]], part[c["pos"]:end]))
    return res


def tags(text):
    return [k for k, rx in POP if re.search(rx, text, re.I)]


def run():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(DATA, "central_anchor.json")   # optional: another anchor file
    anchor = json.load(open(src, encoding="utf-8"))
    cur = json.load(open(LINEAGE, encoding="utf-8"))
    aliases, lineage = cur.get("aliases", {}), cur.get("drugs", {})
    out = dict(source="FDA label section 14 at each AML approval event", drugs={})
    for drug, v in sorted(anchor["drugs"].items()):
        if v.get("status") != "anchored":
            continue
        cand = {a["nct"]: a for a in v["anchors"]}
        # Every identifier of every anchored or event-hit trial -> NCT.
        tok2nct = {}
        for ev in v.get("events", []):
            for h in ev.get("hits", []):
                cand.setdefault(h["nct"], dict(nct=h["nct"], phase=None))
        for n in cand:
            for k, val in (ctgov(n) or {}).get("ids", [("nct", n)]):
                # Skip bare numbers: '2016' is a secondary id that also matches 'the 2016 NCCN guidelines'.
                if len(val) >= 4 and not re.fullmatch(r"[\d\s./-]+", val):
                    tok2nct[val] = n
            for a in aliases.get(n, []):
                tok2nct[a] = n
        evs = []
        for ev in v.get("events", []):
            studies, regs = [], defaultdict(list)
            for d in ev["docs"]:
                if d["source"] != "label":
                    continue
                _, txt, _ = fetch_doc(d["url"], ev["application"])
                parts = aml_part(section14(txt), txt)
                for sub, part in parts:
                    seen = set()
                    for toks, strong, lead_in, text in blocks(part, tok2nct):
                        ncts = sorted({t if t.startswith("NCT") else tok2nct.get(t) for t in toks} - {None})
                        if not strong and set(ncts) <= seen:
                            continue   # repeat mention of a study already read (table, figure caption)
                        seen |= set(ncts)
                        alts, rtype = regimen_alternatives(text, drug, lead_in)
                        st = dict(label=d["url"].rsplit("/", 1)[-1], subsection=sub, tokens=toks, ncts=ncts,
                                  regimens=[" + ".join(a) for a in alts], regimen_type=rtype, population=tags(text[:2500]),
                                  excerpt=re.sub(r"\s+", " ", text[:400]))
                        if ncts and all(s["ncts"] != ncts or s["regimens"] != st["regimens"] for s in studies):
                            studies.append(st)
                if not studies and parts:
                    # Label names no trial id (midostaurin 2017): the curated lineage lead of this approval is the study.
                    ln = [n for st in lineage.get(drug, {}).get("chain", []) if st.get("approval", {}).get("date") == ev["date"]
                          for n in st.get("lead", [])]
                    text = parts[0][1]
                    alts, rtype = regimen_alternatives(text, drug)
                    if ln:
                        studies = [dict(label=d["url"].rsplit("/", 1)[-1], subsection=parts[0][0], tokens=[], ncts=ln,
                                        regimens=[" + ".join(a) for a in alts], regimen_type=rtype, population=tags(text[:2500]),
                                        excerpt=re.sub(r"\s+", " ", text[:400]), id_source="curated lineage (label names no trial id)")]
                if studies:
                    break   # first label of the event that has a section 14 AML study is the event's label
            for st in studies:
                for r in st["regimens"]:
                    regs[r].extend(st["ncts"])
            roles = {}
            for r, ncts in regs.items():
                ranked = sorted(set(ncts), key=lambda n: (-PHASE_RANK.get((cand.get(n) or {}).get("phase") or "", -1), n))
                roles[r] = dict(lead=ranked[0], co_basis=ranked[1:])
            evs.append(dict(application=ev["application"], submission=ev["submission"], date=ev["date"], studies=studies, regimen_leads=roles))
        out["drugs"][drug] = dict(events=[e for e in evs if e["studies"]], events_without_s14_study=[e["date"] for e in evs if not e["studies"]])
        print(drug, [(e["date"], {r: x["lead"] for r, x in e["regimen_leads"].items()}) for e in evs if e["studies"]],
              "no s14 study:", out["drugs"][drug]["events_without_s14_study"])
    json.dump(out, open(OUT, "w", encoding="utf-8"), indent=1)
    print("wrote", OUT)


if __name__ == "__main__":
    run()
