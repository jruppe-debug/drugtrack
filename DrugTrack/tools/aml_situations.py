"""Situation -> treatment options backend for the AML pathway (the 'Treatment finder').

A clinical situation is: disease status (newly diagnosed / relapsed-refractory), age band, fitness for intensive
chemotherapy (ELN 2022 fitness; label wording 'age >=75 or comorbidities'), the molecular markers present, and
CD33 status. resolve() maps a situation to a population and the genetic subgroups of the pathway branch built by
tools/aml_pathways.py, then lists every regimen per treatment phase with the FDA label efficacy of its drugs
(efficacy_tag from tools/dailymed_label.py).

Logic layer: phases carry a HemOnc-style context (hemonc.org regimen context names: first-line, consolidation,
maintenance, subsequent lines, conditioning). When HemOncKB tables are present in data/references/hemonc/
(Harvard Dataverse doi:10.7910/DVN/9CY9C6 CC BY subset, or doi:10.7910/DVN/FPO4HB full KB, CC BY-NC-SA),
regimen names are matched to HemOncKB regimen concepts by exact normalized name; nothing is matched otherwise.

The rules are data (RULES below) and are written to the output, so the viewer evaluates the same rules.
Outputs: tree["finder"] and data/aml_options.json. CLI: python tools/aml_situations.py --status nd --age 75+ --markers flt3_itd
"""
import argparse, csv, json, os, re, sys

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
OUT = os.path.join(DATA, "aml_options.json")
HEMONC = os.path.join(DATA, "references", "hemonc")

QUESTIONS = [
    dict(key="status", label="Disease status", type="one", options=[
        ("nd", "Newly diagnosed"), ("rr", "Relapsed or refractory")]),
    dict(key="age", label="Age", type="one", show_if=dict(status="nd"), options=[
        ("lt60", "<60"), ("60_74", "60-74"), ("75+", ">=75")]),
    dict(key="fit", label="Fit for intensive chemotherapy (ELN 2022 fitness)", type="one", show_if=dict(status="nd"),
         hint="Unfit: ECOG >=3, or severe cardiac, pulmonary, renal or hepatic comorbidity (Ferrara criteria).",
         options=[("fit", "Fit"), ("unfit", "Unfit")]),
    dict(key="markers", label="Molecular and cytogenetic findings", type="many", options=[
        ("flt3_itd", "FLT3-ITD"), ("flt3_tkd", "FLT3-TKD"), ("npm1", "NPM1 mutation"), ("idh1", "IDH1 mutation"),
        ("idh2", "IDH2 mutation"), ("kmt2a", "KMT2A rearrangement"), ("cbf", "Core-binding factor: t(8;21) or inv(16)"),
        ("tp53", "TP53 mutation"), ("aml_mr", "Therapy-related AML or AML myelodysplasia-related")]),
    dict(key="cd33", label="CD33 expression", type="one", options=[
        ("pos", "Positive"), ("neg", "Negative"), ("unk", "Unknown")]),
]

# Population: first rule that matches. Rules: {"key": value or [values]}; every key must match.
POPULATION_RULES = [
    ("rr", dict(status="rr")),
    ("nd_unfit", dict(status="nd", age="75+")),
    ("nd_unfit", dict(status="nd", fit="unfit")),
    ("nd_lt60", dict(status="nd", age="lt60", fit="fit")),
    ("nd_60_75", dict(status="nd", age="60_74", fit="fit")),
]

# Genetic subgroup (pathway group name) -> marker rule. any: one of these present; none: none of these present.
# 'No targetable lesion' is the default group: primary when no other group matches, and its approved
# regimens are listed as mutation-agnostic options otherwise (their US labels do not require a marker).
GROUP_RULES = {
    "Core-binding factor (t(8;21), inv(16)) or NPM1-mutated, FLT3 wild-type": dict(any=["cbf", "npm1"], none=["flt3_itd", "flt3_tkd"]),
    "FLT3-mutated": dict(any=["flt3_itd", "flt3_tkd"]),
    "AML-MR or therapy-related AML": dict(any=["aml_mr"]),
    "TP53-mutated": dict(any=["tp53"]),
    "IDH1-mutated": dict(any=["idh1"]),
    "IDH2-mutated": dict(any=["idh2"]),
    "KMT2A-rearranged": dict(any=["kmt2a"]),
    "NPM1-mutated": dict(any=["npm1"]),
}
DEFAULT_GROUP = "No targetable lesion"

# Regimen-level label restrictions (regimen name regex -> requirement). Hidden when the requirement fails.
REGIMEN_RULES = [
    (r"quizartinib", dict(any=["flt3_itd"], why="quizartinib label: FLT3-ITD positive only")),
    (r"gemtuzumab", dict(cd33_not="neg", why="gemtuzumab ozogamicin label: CD33-positive AML")),
]

# ELN 2022 genetic risk from the markers asked here. Partial: karyotype beyond CBF is not asked.
ELN2022 = [
    ("adverse", dict(any=["tp53", "aml_mr"]), "TP53 mutation or myelodysplasia-related gene mutation"),
    ("adverse", dict(any=["kmt2a"]), "KMT2A rearrangement (t(9;11) KMT2A::MLLT3 is intermediate; not asked)"),
    ("intermediate", dict(any=["flt3_itd"]), "FLT3-ITD (with or without NPM1)"),
    ("favorable", dict(any=["cbf"]), "t(8;21) or inv(16)"),
    ("favorable", dict(any=["npm1"], none=["flt3_itd"]), "NPM1 mutation without FLT3-ITD"),
    ("intermediate", dict(), "no favorable or adverse marker selected (cytogenetics not asked)"),
]

# Pathway phase -> HemOnc-style regimen context and display order.
CONTEXT = {
    "Induction": (1, "First-line therapy: intensive induction"),
    "Lower-intensity induction": (1, "First-line therapy: lower-intensity"),
    "Response assessment": (2, "Response assessment"),
    "Post-remission: favorable risk": (3, "Consolidation after upfront therapy"),
    "Post-remission: not transplant eligible": (3, "Consolidation and maintenance after upfront therapy"),
    "Post-remission": (3, "Continued first-line therapy after response"),
    "Maintenance": (4, "Maintenance after upfront therapy"),
    "Post-remission: transplant eligible": (5, "Allogeneic HSCT and conditioning"),
    "Salvage": (1, "Subsequent lines of therapy (relapsed or refractory)"),
    "After remission": (5, "Allogeneic HSCT and conditioning"),
}
STATUS_ORDER = {"approved": 0, "standard": 1, "investigational": 2}


def match(rule, ans):
    """Population rule: every key equals (or is in) the answer."""
    return all((ans.get(k) in v) if isinstance(v, list) else ans.get(k) == v for k, v in rule.items())


def marker_match(rule, markers, cd33=None):
    if rule.get("any") and not set(rule["any"]) & markers:
        return False
    if set(rule.get("none", [])) & markers:
        return False
    if rule.get("cd33_not") and cd33 == rule["cd33_not"]:
        return False
    return True


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def attach_hemonc(fd):
    """HemOncKB match, component marks, drug census and validation (tools/hemonc_kb.py) when the local KB exists."""
    if not os.path.isdir(HEMONC):
        fd["logic"]["hemonc_import"] = "absent: unzip HemOncKB (dataverse_files.zip) into data/references/hemonc/"
        return None
    import hemonc_kb
    return hemonc_kb.annotate(fd)


def drug_index(tree):
    out = {}
    def walk(n):
        if n["kind"] == "drug":
            out.setdefault(n["id"], n)
        for c in n.get("children") or []:
            walk(c)
    walk(tree)
    return out


def drug_entry(n, trial_ncts):
    a = n.get("attrs") or {}
    e = a.get("efficacy_tag") or {}
    ap = e.get("approval") or {}
    key = ap.get("approved_regimen_key") or ""
    # The efficacy shown belongs to this regimen only when its trial is one of the regimen's label trials.
    same = e.get("central") in trial_ncts
    return dict(id=n["id"], name=n["name"], brand=a.get("brand_display") or "",
                efficacy=dict(headline=e.get("headline"), endpoints=e.get("endpoints") or [],
                              hazard_ratios=[{k: h.get(k) for k in ("label", "hr", "lo", "hi")} for h in e.get("hazard_ratios") or []],
                              trial=e.get("central"), status=e.get("status"), label=e.get("label"), version=e.get("version"),
                              page_url=e.get("page_url"), approval_date=ap.get("approval_date"), approved_regimen=key,
                              applies_to_this_regimen=same) if e else None)


def build(tree):
    path = next((c for c in tree["children"] if c["id"] == "branch:pathway"), None)
    if not path:
        raise SystemExit("no branch:pathway in tree; run aml_pathways.attach_pathways first")
    drugs = drug_index(tree)
    pops = {}
    for pop in path["children"]:
        groups = {}
        for g in pop["children"]:
            phases = []
            for ph in g["children"]:
                order, ctx = CONTEXT.get(ph["name"], (9, ph["name"]))
                regs = []
                for r in ph["children"]:
                    a = r["attrs"]
                    trials = [dict(nct=t["attrs"].get("nct_id"), role="lead" if t["name"].startswith("LEAD") else "co-basis",
                                   title=t["attrs"].get("title", ""), phase=t["attrs"].get("phase")) for t in r["children"]]
                    rule = next((dict(rr) for pat, rr in REGIMEN_RULES if re.search(pat, r["name"], re.I)), None)
                    regs.append(dict(name=r["name"], status=a["pathway_status"], note=a.get("note", ""),
                                     label_regimens=a.get("label_regimens", []), requires=rule, trials=trials,
                                     drugs=[drug_entry(drugs[i], {t["nct"] for t in trials}) for i in a.get("tree_nodes", []) if i in drugs],
                                     hemonc=None))
                regs.sort(key=lambda x: STATUS_ORDER.get(x["status"], 9))
                phases.append(dict(phase=ph["name"], context=ctx, order=order, regimens=regs))
            phases.sort(key=lambda x: x["order"])
            groups[g["name"]] = dict(rule=GROUP_RULES.get(g["name"]), default=g["name"] == DEFAULT_GROUP, phases=phases)
        key = pop["id"].split(":", 1)[1]
        pops[key] = dict(title=pop["name"], source=(pop.get("attrs") or {}).get("source"), groups=groups)
    return dict(
        version=1, scope="AML (non-APL), US FDA labels",
        logic=dict(population="ELN 2022 fitness + age bands of PMC11966364 figs 2-5",
                   contexts="HemOnc-style regimen contexts (hemonc.org section names); HemOncKB concept IDs only when imported",
                   hemonc_import=None,
                   efficacy="FDA label section 14 via DailyMed (efficacy_tag); applies_to_this_regimen=false means the "
                            "headline is from the drug's approval-basis trial of another regimen"),
        questions=[dict(q, options=[dict(value=v, label=l) for v, l in q["options"]]) for q in QUESTIONS],
        population_rules=[dict(population=p, when=w) for p, w in POPULATION_RULES],
        eln2022=[dict(risk=r, rule=w, basis=b) for r, w, b in ELN2022],
        populations=pops)


def resolve(fd, ans):
    """answers -> dict(population, eln2022, primary=[group...], agnostic=group or None, notes)."""
    pop = next((p["population"] for p in fd["population_rules"] if match(p["when"], ans)), None)
    if not pop:
        return dict(population=None, notes=["answer status, age and fitness"])
    m = set(ans.get("markers") or [])
    eln = next(dict(risk=e["risk"], basis=e["basis"]) for e in fd["eln2022"] if marker_match(e["rule"], m))
    P = fd["populations"][pop]
    def regimens_ok(g):
        out = []
        for ph in g["phases"]:
            regs = [r for r in ph["regimens"] if not r["requires"] or marker_match(r["requires"], m, ans.get("cd33"))]
            hidden = [r["name"] + ": " + r["requires"]["why"] for r in ph["regimens"] if r not in regs]
            out.append(dict(ph, regimens=regs, hidden=hidden))
        return out
    primary = [dict(group=n, phases=regimens_ok(g)) for n, g in P["groups"].items() if g["rule"] and marker_match(g["rule"], m)]
    dflt = next(((n, g) for n, g in P["groups"].items() if g["default"]), None)
    agnostic = None
    if dflt and not primary:
        primary = [dict(group=dflt[0], phases=regimens_ok(dflt[1]))]
    elif dflt:
        seen = {r["name"] for g in primary for ph in g["phases"] for r in ph["regimens"]}
        agnostic = dict(group=dflt[0] + " (mutation-agnostic approved options)",
                        phases=[dict(ph, regimens=[r for r in ph["regimens"] if r["status"] == "approved" and r["name"] not in seen])
                                for ph in regimens_ok(dflt[1])])
        agnostic["phases"] = [ph for ph in agnostic["phases"] if ph["regimens"]]
        agnostic = agnostic if agnostic["phases"] else None
    return dict(population=pop, title=P["title"], eln2022=eln, primary=primary, agnostic=agnostic)


def attach_finder(tree, out=OUT):
    fd = build(tree)
    attach_hemonc(fd)
    import sponsor_links  # brand HCP / sponsor pipeline checks; no-op until tools/sponsor_links.py has been run
    sponsor_links.attach(fd)
    tree["finder"] = fd
    json.dump(fd, open(out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    n = sum(len(ph["regimens"]) for p in fd["populations"].values() for g in p["groups"].values() for ph in g["phases"])
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--status", choices=["nd", "rr"], required=True)
    ap.add_argument("--age", choices=["lt60", "60_74", "75+"])
    ap.add_argument("--fit", choices=["fit", "unfit"])
    ap.add_argument("--markers", default="", help="comma list: " + ",".join(v for v, _ in QUESTIONS[3]["options"]))
    ap.add_argument("--cd33", choices=["pos", "neg", "unk"], default="unk")
    ap.add_argument("--tree", default=os.path.join(DATA, "tree.json"))
    x = ap.parse_args()
    fd = build(json.load(open(x.tree, encoding="utf-8")))
    ans = dict(status=x.status, age=x.age, fit=x.fit, cd33=x.cd33, markers=[m for m in x.markers.split(",") if m])
    r = resolve(fd, ans)
    if not r["population"]:
        sys.exit(r["notes"][0])
    print(r["title"], "| ELN 2022:", r["eln2022"]["risk"], "-", r["eln2022"]["basis"])
    for g in r["primary"] + ([r["agnostic"]] if r["agnostic"] else []):
        print("\n##", g["group"])
        for ph in g["phases"]:
            print("  [" + ph["context"] + "]", ph["phase"])
            for reg in ph["regimens"]:
                print("    -", reg["status"].upper(), reg["name"], ("(" + reg["note"] + ")") if reg["note"] else "")
                for d in reg["drugs"]:
                    e = d["efficacy"]
                    if e:
                        print("        ", d["name"], "|", e["trial"], "|", (e["headline"] or "")[:110],
                              "" if e["applies_to_this_regimen"] else "[from " + e["approved_regimen"] + "]")
            for h in ph.get("hidden", []):
                print("    x hidden:", h)


if __name__ == "__main__":
    main()
