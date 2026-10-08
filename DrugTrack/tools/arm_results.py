"""Arm-level efficacy and safety from AACT posted results (pilot).

Pilot scope: two approved AML combinations whose pivotal trials posted results on ClinicalTrials.gov.
  VIALE-A  NCT02993523  venetoclax + azacitidine vs placebo + azacitidine
  RATIFY   NCT00651261  midostaurin + daunorubicin/cytarabine vs placebo + daunorubicin/cytarabine
Both are the semantic-V2 central studies for their drug under the pivotal-with-results rule (human rule 2026-10-06).

Everything is deterministic. Each output row keeps the AACT table and row id it came from.

  ../.venv/Scripts/python tools/arm_results.py pull    # AACT -> data/aact_results_pilot.json.gz (network)
  ../.venv/Scripts/python tools/arm_results.py labels  # openFDA drug labels -> data/fda_labels_pilot.json (network, no key)
  ../.venv/Scripts/python tools/arm_results.py build   # caches -> data/arm_results_pilot.json

Reporting rules (human decisions 2026-10-06):
  - Only posted values. No derived composites; a composite outcome carries its posted definition instead.
  - Outcomes without posted results are left out.
  - Comparisons use randomized on-treatment groups. Regional/open-label cohorts are supportive. Follow-up groups are excluded.
  - Deaths totals are information only; mortality comes from OS.
  - The AACT 'serious' category is checked against the FDA label. When it does not behave like a true SAE category,
    the trial is flagged and must not be compared with true-SAE trials.
"""
import gzip
import json
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
CACHE = os.path.join(DATA, "aact_results_pilot.json.gz")
OUT = os.path.join(DATA, "arm_results_pilot.json")
LABELS = os.path.join(DATA, "fda_labels_pilot.json")

PILOT = {
    "NCT02993523": dict(name="VIALE-A", combo="VENETOCLAX + AZACITIDINE", drug="VENETOCLAX", pivotal=True,
                        label_search='openfda.generic_name:"venetoclax"', label_section="VENCLEXTA in Combination with Azacitidine",
                        label_arm="VEN+AZA"),
    "NCT00651261": dict(name="RATIFY", combo="MIDOSTAURIN + DAUNORUBICIN + CYTARABINE", drug="MIDOSTAURIN", pivotal=True,
                        label_search='openfda.brand_name:"rydapt"', label_section="newly diagnosed FLT3", label_arm="RYDAPT"),
}

QUERIES = {
    "studies": "SELECT nct_id, brief_title, phase, overall_status, enrollment, results_first_posted_date FROM ctgov.studies WHERE nct_id = ANY(%s)",
    "design_groups": "SELECT nct_id, id, group_type, title, description FROM ctgov.design_groups WHERE nct_id = ANY(%s)",
    "interventions": "SELECT nct_id, id, intervention_type, name FROM ctgov.interventions WHERE nct_id = ANY(%s)",
    "design_group_interventions": "SELECT nct_id, design_group_id, intervention_id FROM ctgov.design_group_interventions WHERE nct_id = ANY(%s)",
    "result_groups": "SELECT nct_id, id, ctgov_group_code, result_type, title, description FROM ctgov.result_groups WHERE nct_id = ANY(%s)",
    "outcomes": """SELECT nct_id, id, outcome_type, title, description, time_frame, population, units, param_type, dispersion_type
                   FROM ctgov.outcomes WHERE nct_id = ANY(%s)""",
    "outcome_counts": "SELECT nct_id, outcome_id, result_group_id, ctgov_group_code, scope, units, count FROM ctgov.outcome_counts WHERE nct_id = ANY(%s)",
    "outcome_measurements": """SELECT nct_id, id, outcome_id, result_group_id, ctgov_group_code, classification, category, title,
                                      param_type, param_value, param_value_num, dispersion_type, dispersion_value,
                                      dispersion_lower_limit, dispersion_upper_limit, units
                               FROM ctgov.outcome_measurements WHERE nct_id = ANY(%s)""",
    "outcome_analyses": """SELECT nct_id, id, outcome_id, non_inferiority_type, param_type, param_value, ci_percent,
                                  ci_lower_limit, ci_upper_limit, p_value, method, groups_description
                           FROM ctgov.outcome_analyses WHERE nct_id = ANY(%s)""",
    "outcome_analysis_groups": "SELECT nct_id, outcome_analysis_id, result_group_id, ctgov_group_code FROM ctgov.outcome_analysis_groups WHERE nct_id = ANY(%s)",
    "reported_events": """SELECT nct_id, id, result_group_id, ctgov_group_code, event_type, adverse_event_term, organ_system,
                                 subjects_affected, subjects_at_risk, event_count, vocab, assessment, default_vocab, default_assessment,
                                 time_frame, description, frequency_threshold
                          FROM ctgov.reported_events WHERE nct_id = ANY(%s)""",
    "study_references": "SELECT nct_id, pmid, reference_type, citation FROM ctgov.study_references WHERE nct_id = ANY(%s)",
    "reported_event_totals": """SELECT nct_id, ctgov_group_code, event_type, classification, subjects_affected, subjects_at_risk
                                FROM ctgov.reported_event_totals WHERE nct_id = ANY(%s)""",
    "baseline_counts": "SELECT nct_id, result_group_id, ctgov_group_code, units, scope, count FROM ctgov.baseline_counts WHERE nct_id = ANY(%s)",
}


def pull():
    sys.path.insert(0, HERE)
    import match_pipeline as mp
    aact = mp.pooled_client()
    ncts = sorted(PILOT)
    raw = {}
    for key, sql in QUERIES.items():
        raw[key] = mp.run_query(aact, sql, (ncts,), limit=500000)
        print(f"[pull] {key}: rows={len(raw[key])}", flush=True)
    if aact._state["conn"] is not None:
        aact._state["conn"].close()
    with gzip.open(CACHE, "wt", encoding="utf-8") as f:
        json.dump(raw, f, default=str)
    print(f"[pull] wrote {CACHE}")


def labels():
    """openFDA label text for each pilot drug: the AML part of the adverse-reactions section, kept verbatim with its set id.
    No API key is sent (anonymous quota is enough for two calls)."""
    import requests
    out = {}
    for nct, info in PILOT.items():
        d = requests.get("https://api.fda.gov/drug/label.json", params={"search": info["label_search"], "limit": 1}, timeout=30).json()["results"][0]
        ar = " ".join(d.get("adverse_reactions") or [])
        i = ar.find(info["label_section"])
        # The AML subsection runs to the next 'Table' heading after its laboratory table, capped to keep the cache small.
        out[nct] = dict(set_id=d.get("set_id"), effective_time=d.get("effective_time"), brand=(d.get("openfda") or {}).get("brand_name"),
                        url=f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={d.get('set_id')}",
                        section_found=i >= 0, text=ar[i:i + 6000] if i >= 0 else "")
        print(f"[labels] {nct} set_id={d.get('set_id')} effective={d.get('effective_time')} section_found={i >= 0}", flush=True)
    json.dump(out, open(LABELS, "w", encoding="utf-8"), indent=1)
    print(f"[labels] wrote {LABELS}")


# ============================================================================ rules (each one is a reviewable decision)
PLACEBO_RX = re.compile(r"\bplacebo\b", re.I)
TOTAL_RX = re.compile(r"^\s*total\s*$", re.I)
OPEN_COHORT_RX = re.compile(r"open[- ]label|safety cohort|japan only|china", re.I)
POST_TX_RX = re.compile(r"post-treatment|survival follow-up|other participants", re.I)

# Endpoint classes, first match wins. Order matters: composite remission before plain CR.
ENDPOINTS = [
    ("PK", r"pharmac?okinetic|\bPK\b|\bAUC|Cmax|metabolite"),
    ("QOL", r"quality of life|QoL|fatigue|global health|PGIC"),
    ("TRANSFUSION_INDEPENDENCE", r"transfusion independence"),
    ("OS_CENSORED_AT_HCT", r"overall survival.*censor"),
    ("OS", r"overall survival|\bOS\b"),
    ("EFS", r"event[- ]+free survival|\bEFS\b"),
    ("DFS_RFS", r"disease[- ]free survival|relapse[- ]free survival|\bDFS\b|\bRFS\b"),
    ("CIR", r"cumulative incidence of relapse|\bCIR\b"),
    ("CR_CRH", r"\bCRh\b|partial hematologic recovery"),
    ("CR_CRI", r"\bCRi\b|incomplete (marrow|blood count|hematologic) recovery|composite complete remission"),
    ("CR", r"complete (remission|response)|\bCR\b"),
    ("MRD", r"minimal residual|measurable residual|\bMRD\b"),
    ("SAFETY_EVENTS", r"safety events|adverse event|dose[- ]limiting"),
    ("DEATHS", r"all collected deaths|\bdeaths\b"),
]
EFFICACY = {"OS", "OS_CENSORED_AT_HCT", "EFS", "DFS_RFS", "CIR", "CR", "CR_CRI", "CR_CRH", "MRD", "TRANSFUSION_INDEPENDENCE"}

# A 'serious' category is suspect when CTCAE laboratory terms reach this rate in it: lab decreases are graded, not serious events.
LAB_TERM_RX = re.compile(r"(count|ha?emoglobin)\b.*decreased", re.I)
LAB_TERM_RATE = 20.0
# A posted single-term serious rate may sit below a grouped label rate, never this many points above it, in a true SAE category.
LABEL_TOLERANCE = 5.0

# AML-relevant adverse-event classes. A class rate is the highest single-term rate in that class, never a sum,
# because one participant can have several terms and AACT gives no participant-level union.
AE_CLASSES = [
    ("febrile_neutropenia", r"febrile neutropeni"),
    ("neutropenia", r"^neutropeni|neutrophil count decreased"),
    ("thrombocytopenia", r"thrombocytopeni|platelet count decreased"),
    ("anaemia", r"an(a)?emia|ha?emoglobin decreased"),
    ("sepsis", r"sepsis|septic"),
    ("pneumonia", r"pneumonia|lung infection"),
    ("differentiation_syndrome", r"differentiation syndrome"),
    ("tumour_lysis", r"tumou?r lysis"),
    ("qt_prolonged", r"\bQTc?\b.*prolong"),
    ("haemorrhage", r"ha?emorrhage|bleeding"),
    ("nausea", r"^nausea"),
    ("rash", r"^rash"),
]


def endpoint_class(title):
    for k, rx in ENDPOINTS:
        if re.search(rx, title or "", re.I):
            return k
    return "OTHER"


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def norm_unit(u, param_type):
    u = (u or "").lower()
    if "month" in u:
        return "months"
    if "percent" in u or "%" in u:
        return "percent"
    if "participant" in u and (param_type or "").upper() == "COUNT_OF_PARTICIPANTS":
        return "count"
    return u or None


def arm_regimens(raw, nct, pilot_drug):
    """Registered arms -> drugs. A drug in every non-placebo arm is backbone; the pilot drug or a drug only in some arms is investigational."""
    iv = {i["id"]: i for i in raw["interventions"] if i["nct_id"] == nct}
    arms = {g["id"]: dict(g, drugs=[]) for g in raw["design_groups"] if g["nct_id"] == nct}
    for l in raw["design_group_interventions"]:
        if l["nct_id"] == nct and l["design_group_id"] in arms:
            arms[l["design_group_id"]]["drugs"].append(iv[l["intervention_id"]]["name"])
    for a in arms.values():
        a["placebo"] = any(PLACEBO_RX.search(d) for d in a["drugs"])
        a["active"] = sorted({d.upper() for d in a["drugs"] if not PLACEBO_RX.search(d)})
    common = set.intersection(*(set(a["active"]) for a in arms.values())) if arms else set()
    for a in arms.values():
        a["backbone"] = sorted(common)
        a["investigational"] = sorted(set(a["active"]) - common)
        a["role"] = "experimental" if pilot_drug in a["investigational"] or (pilot_drug in a["active"] and not a["placebo"]) else (
            "control" if a["placebo"] or a["group_type"] in ("PLACEBO_COMPARATOR", "ACTIVE_COMPARATOR") else "other")
        if OPEN_COHORT_RX.search(a["title"] or ""):
            a["role"] = "supportive_cohort"
    return arms


def map_result_group(g, arms, pilot_drug):
    """Posted-results group -> role and registered arm. The role comes from drug words in the group title; the arm is the
    registered arm with the same role and the most shared title words. Returns (role, arm_id, rule)."""
    t = g["title"] or ""
    if TOTAL_RX.search(t):
        return "total", None, "title is 'Total'"
    if POST_TX_RX.search(t) or (g["description"] and POST_TX_RX.search(g["title"])):
        return "follow_up_only", None, "post-treatment or follow-up group"
    if OPEN_COHORT_RX.search(t):
        role, rule = "supportive_cohort", "open-label / regional cohort in title"
    elif PLACEBO_RX.search(t):
        role, rule = "control", "placebo in title"
    elif pilot_drug.lower() in t.lower():
        role, rule = "experimental", f"{pilot_drug.lower()} in title"
    else:
        return "unmapped", None, "no drug or placebo word in title"
    words = set(re.findall(r"[a-z]{4,}", t.lower()))
    cands = [a for a in arms.values() if a["role"] == role] or list(arms.values())
    best = max(cands, key=lambda a: len(words & set(re.findall(r"[a-z]{4,}", (a["title"] or "").lower()))), default=None)
    return role, best["id"] if best else None, rule


COMPARE_ROLES = {"experimental", "control"}


def ae_class(term):
    return next((cls for cls, rx in AE_CLASSES if re.search(rx, term.strip(), re.I)), None)


def label_serious(text):
    """Serious-reaction figures the FDA label states for the experimental arm, read verbatim: the overall rate and the
    '(term) (N%)' pairs in sentences that name serious adverse reactions. Fatal-reaction sentences are skipped."""
    overall = re.search(r"Serious adverse reactions were reported in (\d+(?:\.\d+)?)% of patients", text)
    terms = {}
    for sent in re.split(r"(?<=\.)\s+(?=[A-Z])", text):
        if not re.search(r"serious adverse reaction", sent, re.I) or re.search(r"fatal", sent, re.I):
            continue
        for term, pct in re.findall(r"([A-Za-z][A-Za-z \-]*?) \((?:[^()%]*?; )?(\d+(?:\.\d+)?)%\)", sent):
            term = re.sub(r"^(?:and|being|was|were|or)\s+", "", term.strip(), flags=re.I)
            cls = ae_class(term)
            if cls and cls not in terms:
                terms[cls] = dict(term=term, rate=float(pct), sentence=sent.strip())
    graded = re.search(r"(?:For [^.]{0,60}?, )?only Grades? 3 and 4 were collected[^.]*\.", text)
    return dict(overall=float(overall.group(1)) if overall else None, overall_sentence=overall.group(0) if overall else None,
                terms=terms, grade_collection_note=graded.group(0).strip() if graded else None)


def serious_check(safety, label):
    """Is the AACT 'serious' category a true SAE category? Compared with the FDA label for the experimental arm, plus a
    lab-term signal inside the category. Verdict: true_sae, severity_graded, non_standard, or unverified."""
    exp = [r for r in safety if r["arm_role"] == "experimental"]
    ev = dict(comparisons=[], lab_terms=[], grade_collection_note=None, label=None)
    for r in safety:
        for t in r["serious_lab_terms"]:
            ev["lab_terms"].append(dict(group=r["group"], **t))
    mismatch = False
    if label and label.get("section_found") and exp:
        ls = label_serious(label["text"])
        ev["label"] = dict(set_id=label["set_id"], effective_time=label["effective_time"], url=label["url"])
        ev["grade_collection_note"] = ls["grade_collection_note"]
        x = exp[0]
        tot = (x["totals"].get("serious") or {}).get("rate")
        if ls["overall"] is not None and tot is not None:
            bad = abs(tot - ls["overall"]) > LABEL_TOLERANCE
            mismatch |= bad
            ev["comparisons"].append(dict(what="any serious event", aact_group=x["group"], aact_rate=tot, label_rate=ls["overall"],
                                          mismatch=bad, label_text=ls["overall_sentence"]))
        for cls, lt in ls["terms"].items():
            a = (x["classes"].get(cls) or {}).get("serious")
            if a:
                bad = a["rate"] - lt["rate"] > LABEL_TOLERANCE
                mismatch |= bad
                ev["comparisons"].append(dict(what=cls, aact_group=x["group"], aact_term=a["term"], aact_rate=a["rate"],
                                              label_term=lt["term"], label_rate=lt["rate"], mismatch=bad, label_text=lt["sentence"]))
    if ev["lab_terms"] or (mismatch and ev["grade_collection_note"]):
        verdict = "severity_graded"
    elif mismatch:
        verdict = "non_standard"
    elif ev["comparisons"]:
        verdict = "true_sae"
    else:
        verdict = "unverified"
    note = {"true_sae": "Posted serious rates agree with the FDA label within the tolerance. Comparable with other true-SAE trials.",
            "severity_graded": "The 'serious' category holds graded events (CTCAE lab terms or a grade-only collection note). Not comparable with true-SAE trials.",
            "non_standard": "Posted serious rates exceed the FDA label's serious rates. Not comparable with true-SAE trials.",
            "unverified": "No label figure to check against."}[verdict]
    return dict(verdict=verdict, comparable=verdict == "true_sae", note=note,
                rules=dict(lab_term_rx=LAB_TERM_RX.pattern, lab_term_rate=LAB_TERM_RATE, label_tolerance_points=LABEL_TOLERANCE),
                evidence=ev)


def build():
    raw = json.load(gzip.open(CACHE, "rt", encoding="utf-8"))
    lab = json.load(open(LABELS, encoding="utf-8")) if os.path.exists(LABELS) else {}
    out = dict(meta=dict(source="AACT ctgov schema; FDA label via openFDA", cache=os.path.basename(CACHE),
                         labels=os.path.basename(LABELS) if lab else None, compare_roles=sorted(COMPARE_ROLES),
                         endpoint_rules=ENDPOINTS, ae_classes=AE_CLASSES), trials=[])
    for nct, info in PILOT.items():
        st = next(s for s in raw["studies"] if s["nct_id"] == nct)
        arms = arm_regimens(raw, nct, info["drug"])
        groups = {g["id"]: g for g in raw["result_groups"] if g["nct_id"] == nct}
        gmap = {gid: map_result_group(g, arms, info["drug"]) for gid, g in groups.items()}
        counts = {(c["outcome_id"], c["result_group_id"]): c["count"] for c in raw["outcome_counts"]
                  if c["nct_id"] == nct and (c["scope"] or "").lower() == "overall"}
        counts.update({(c["outcome_id"], c["result_group_id"]): c["count"] for c in raw["outcome_counts"]
                       if c["nct_id"] == nct and (c["outcome_id"], c["result_group_id"]) not in counts})

        # Efficacy: one row per (outcome, result group, category).
        # Outcomes without posted measurements are skipped. Follow-up-only groups are never reported.
        efficacy, other_outcomes, composite_specs = [], [], []
        for o in (o for o in raw["outcomes"] if o["nct_id"] == nct):
            ep = endpoint_class(o["title"])
            ms = [m for m in raw["outcome_measurements"] if m["outcome_id"] == o["id"] and gmap[m["result_group_id"]][0] != "follow_up_only"]
            if not ms:
                continue
            if ep not in EFFICACY:
                other_outcomes.append(dict(outcome_id=o["id"], title=o["title"], endpoint=ep, rows=len(ms)))
                continue
            if ep in ("CR_CRI", "CR_CRH"):
                # Composite remission: report the posted components and the posted definition. Never sum the components.
                composite_specs.append(dict(outcome_id=o["id"], endpoint=ep, title=o["title"], outcome_type=o["outcome_type"],
                                            components_posted=sorted({m["classification"] or m["category"] or "(single value)" for m in ms}),
                                            definition=o["description"], time_frame=o["time_frame"], population=o["population"],
                                            note="Only posted rows are shown. No composite value is computed from components."))
            for m in ms:
                role, arm_id, rule = gmap[m["result_group_id"]]
                v = num(m["param_value"])
                efficacy.append(dict(
                    outcome_id=o["id"], measurement_id=m["id"], outcome_type=o["outcome_type"], endpoint=ep,
                    component=m["classification"] or m["category"] or None, title=o["title"], time_frame=o["time_frame"],
                    group=groups[m["result_group_id"]]["title"], group_code=m["ctgov_group_code"], arm_role=role, arm_id=arm_id, map_rule=rule,
                    comparable=role in COMPARE_ROLES, supportive=role == "supportive_cohort",
                    n=num(counts.get((o["id"], m["result_group_id"]))),
                    stat=(m["param_type"] or o["param_type"] or "").upper(), value=v, value_raw=m["param_value"],
                    unit=norm_unit(m["units"] or o["units"], m["param_type"] or o["param_type"]),
                    ci=[num(m["dispersion_lower_limit"]), num(m["dispersion_upper_limit"])], ci_type=o["dispersion_type"],
                    not_reached=v is None and (m["param_value"] or "").upper() == "NA"))

        # Between-arm analyses with direction from the analysis group order.
        analyses = []
        for a in (a for a in raw["outcome_analyses"] if a["nct_id"] == nct):
            o = next(o for o in raw["outcomes"] if o["id"] == a["outcome_id"])
            ag = [gmap[g["result_group_id"]][0] for g in raw["outcome_analysis_groups"] if g["outcome_analysis_id"] == a["id"]]
            analyses.append(dict(analysis_id=a["id"], outcome_id=o["id"], endpoint=endpoint_class(o["title"]), title=o["title"],
                                 stat=a["param_type"], value=num(a["param_value"]), ci=[num(a["ci_lower_limit"]), num(a["ci_upper_limit"])],
                                 ci_percent=num(a["ci_percent"]), p_value=num(a["p_value"]), method=a["method"], groups=ag,
                                 groups_description=a.get("groups_description"),
                                 # AACT lists the compared groups but does not encode which is numerator, so direction is never assumed.
                                 direction="not encoded in AACT; read from ratio description" if set(ag) >= {"experimental", "control"} else "unclear"))

        # Safety: totals per group, then AML-relevant classes as the highest single-term rate.
        rg_by_code = {g["ctgov_group_code"]: g for g in groups.values() if g["result_type"] == "Reported Event"}
        safety = []
        for code, g in sorted(rg_by_code.items()):
            role, arm_id, rule = gmap[g["id"]]
            if role == "follow_up_only":
                continue
            tot = {t["event_type"]: t for t in raw["reported_event_totals"] if t["nct_id"] == nct and t["ctgov_group_code"] == code}
            row = dict(group=g["title"], group_code=code, arm_role=role, arm_id=arm_id, map_rule=rule,
                       comparable=role in COMPARE_ROLES, supportive=role == "supportive_cohort", totals={}, classes={})
            for et, t in tot.items():
                a_, r_ = t["subjects_affected"], t["subjects_at_risk"]
                row["totals"][et] = dict(affected=a_, at_risk=r_, rate=round(100 * a_ / r_, 1) if a_ is not None and r_ else None)
                if et == "deaths":
                    row["totals"][et]["info_only"] = "All-cause deaths over the AE time frame. Not a mortality endpoint; use OS."
            evs = [e for e in raw["reported_events"] if e["nct_id"] == nct and e["result_group_id"] == g["id"]]
            row["serious_lab_terms"] = [dict(term=e["adverse_event_term"], rate=round(100 * e["subjects_affected"] / e["subjects_at_risk"], 1), event_id=e["id"])
                                        for e in evs if e["event_type"] == "serious" and e["subjects_at_risk"]
                                        and LAB_TERM_RX.search(e["adverse_event_term"] or "")
                                        and 100 * e["subjects_affected"] / e["subjects_at_risk"] >= LAB_TERM_RATE]
            for cls, rx in AE_CLASSES:
                for et in ("serious", "other"):
                    hits = [e for e in evs if e["event_type"] == et and re.search(rx, e["adverse_event_term"] or "", re.I) and e["subjects_at_risk"]]
                    if hits:
                        top = max(hits, key=lambda e: e["subjects_affected"] / e["subjects_at_risk"])
                        row["classes"].setdefault(cls, {})[et] = dict(
                            term=top["adverse_event_term"], affected=top["subjects_affected"], at_risk=top["subjects_at_risk"],
                            rate=round(100 * top["subjects_affected"] / top["subjects_at_risk"], 1), terms_matched=len(hits), event_id=top["id"])
            safety.append(row)
        tev = [e for e in raw["reported_events"] if e["nct_id"] == nct]
        ae_spec = dict(vocab=sorted({e["vocab"] or e["default_vocab"] for e in tev if e["vocab"] or e["default_vocab"]}),
                       assessment=sorted({e["assessment"] or e["default_assessment"] for e in tev if e["assessment"] or e["default_assessment"]}),
                       frequency_threshold_pct=sorted({e["frequency_threshold"] for e in tev if e["frequency_threshold"] is not None}),
                       time_frame=sorted({e["time_frame"] for e in tev if e["time_frame"]}),
                       description=sorted({e["description"] for e in tev if e["description"]}),
                       serious_category=serious_check(safety, lab.get(nct)))
        refs = [dict(pmid=r_["pmid"], type=r_["reference_type"], citation=r_["citation"]) for r_ in raw["study_references"] if r_["nct_id"] == nct]

        out["trials"].append(dict(
            nct_id=nct, **info, title=st["brief_title"], phase=st["phase"], status=st["overall_status"], enrollment=st["enrollment"],
            results_posted=st["results_first_posted_date"],
            arms=[dict(id=a["id"], title=a["title"], group_type=a["group_type"], role=a["role"], drugs=a["drugs"],
                       backbone=a["backbone"], investigational=a["investigational"], placebo=a["placebo"]) for a in arms.values()],
            result_groups=[dict(id=gid, code=g["ctgov_group_code"], type=g["result_type"], title=g["title"],
                                role=gmap[gid][0], arm_id=gmap[gid][1], rule=gmap[gid][2]) for gid, g in groups.items()],
            efficacy=efficacy, composite_specs=composite_specs, analyses=analyses, safety=safety, ae_spec=ae_spec,
            non_efficacy_outcomes=other_outcomes, references=refs))
    json.dump(out, open(OUT, "w", encoding="utf-8"), indent=1, default=str)
    for t in out["trials"]:
        unm = sum(g["role"] == "unmapped" for g in t["result_groups"])
        print(f"{t['nct_id']} {t['name']}: arms={len(t['arms'])} result_groups={len(t['result_groups'])} unmapped={unm} "
              f"efficacy_rows={len(t['efficacy'])} composites={len(t['composite_specs'])} analyses={len(t['analyses'])} "
              f"safety_groups={len(t['safety'])} serious={t['ae_spec']['serious_category']['verdict']} refs={len(t['references'])}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    {"pull": pull, "labels": labels, "build": build}[sys.argv[1]]()
