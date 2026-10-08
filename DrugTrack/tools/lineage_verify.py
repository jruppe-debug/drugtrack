"""Verify the human-entered approval lineage (data/curated/approval_lineage.json) against primary sources.

Every chain step is one FDA approval event credited to one or more trials. A step is 'verified' only when every gate passes:
  registry    each lead / co-basis trial exists on ClinicalTrials.gov, lists the drug (or a code name) as an intervention, and its
              registered phase matches the entered phase
  event       the entered (application, date, ORIG/SUPPL) matches a Drugs@FDA approval event
  named       a Label or Review at that event names the trial (NCT, org study ID, secondary ID, acronym or curated alias)
              outside postmarketing-requirement context (approval_anchor.hit_role)
  novel       no Label or Review at an EARLIER event of the same application names the trial in an efficacy role
              (otherwise the step is not where the trial changed the label)
  type        the approval letter's accelerated-approval wording agrees with the entered accelerated / regular type
Any failed gate makes the step 'needs_review' with the reasons listed; nothing is dropped or auto-corrected.

Precursors (earlier-phase trials behind a lead trial) are checked for registry presence and an earlier start date, and tagged
'doc_supported' when a Label or Review at or before the step's event names them, else 'registry_only'.

Reads the cached documents listed in data/approval_anchor_pilot.json (run tools/approval_anchor.py first). No network calls
except ClinicalTrials.gov records missing from data/references/ctgov/. Output: data/approval_lineage_verified.json
"""
import json, os, re, sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from approval_anchor import DATA, REF, ROOT, ctgov, find_ids, hit_role

CURATED = os.path.join(DATA, "curated", "approval_lineage.json")
# Accelerated approval: 21 CFR 314.510 (NDA, subpart H) / 601.41 (BLA, subpart E). Letters for a regular approval that converts
# an earlier accelerated one can mention it too, so the type gate reports the matched sentence for review.
ACCEL_RX = re.compile(r"[^.]{0,200}(?:314\.510|601\.41|accelerated approval)[^.]{0,200}", re.I)
# A letter that fulfils an earlier Subpart H / E requirement converts that accelerated approval to regular approval.
CONVERT_RX = re.compile(r"[^.]{0,200}(?:subpart [HE] fulfilled|fulfill\w* [^.]{0,120}(?:subpart [HE]|314\.510|601\.41)|to receive regular approval)[^.]{0,200}", re.I)


def registry(nct):
    rec = ctgov(nct)
    p = os.path.join(REF, "ctgov", nct + ".json")
    if not rec or not os.path.exists(p):
        return None
    ps = json.load(open(p, encoding="utf-8"))["protocolSection"]
    return dict(ids=rec["ids"], acronym=rec.get("acronym"), sponsor=rec.get("sponsor"), title=ps["identificationModule"].get("briefTitle"),
                phases=ps.get("designModule", {}).get("phases") or [], start=ps.get("statusModule", {}).get("startDateStruct", {}).get("date"),
                status=ps.get("statusModule", {}).get("overallStatus"),
                interventions=[i["name"] for i in ps.get("armsInterventionsModule", {}).get("interventions", [])])


def phase_key(phases):
    """['PHASE1', 'PHASE2'] -> 'PHASE1_2' (the tree's phase vocabulary)."""
    return "PHASE" + "_".join(sorted(p.replace("PHASE", "") for p in phases)) if phases else None


def doc_text(r):
    path = os.path.join(os.path.dirname(ROOT), r["local"]) + ".txt" if r.get("local") else None
    return open(path, encoding="utf-8").read() if path and os.path.exists(path) else ""


def mentions(events, ids, upto=None, before=None):
    """Label/Review hits of a trial's identifiers across events (optionally only events on/before `upto` or strictly before `before`)."""
    out = []
    for e in events:
        if (upto and e["date"] > upto) or (before and e["date"] >= before):
            continue
        for r in e["doc_results"]:
            if r["type"] not in ("Label", "Review") or not r["chars"]:
                continue
            text = doc_text(r)
            h = find_ids(text, ids)
            if h:
                out.append(dict(date=e["date"], submission=e["submission"], doc=r["type"], file=os.path.basename(r["local"]), ids=h, role=hit_role(text, ids)))
    return out


def check_trial(nct, names, phase_entered=None):
    reg = registry(nct)
    if not reg:
        return None, [f"{nct}: not found on ClinicalTrials.gov"]
    why = []
    if not any(n.lower() in i.lower() for n in names for i in reg["interventions"]):
        why.append(f"{nct}: drug not in registered interventions {reg['interventions']}")
    if phase_entered and phase_key(reg["phases"]) != phase_entered:
        why.append(f"{nct}: entered {phase_entered}, registry says {phase_key(reg['phases'])}")
    return reg, why


def verify_step(drug, d, step, events):
    ap = step["approval"]
    why, trials = [], {}
    names = d.get("intervention_names", [drug.lower()])
    for role, ncts in (("lead", step["lead"]), ("co_basis", step.get("co_basis", []))):
        for nct in ncts:
            reg, w = check_trial(nct, names, step.get("phase_entered") if role == "lead" else None)
            why += w
            trials[nct] = dict(role=role, registry=reg)
    ev = next((e for e in events if e["application"] == d["application"] and e["date"] == ap["date"]
               and (e["submission"] == "ORIG") == (ap["submission"] == "ORIG")), None)
    if not ev:
        why.append(f"no Drugs@FDA event {d['application']} {ap['submission']} {ap['date']}")
    else:
        if ev["kind"] != ap["kind"]:
            why.append(f"event kind is {ev['kind']}, entered {ap['kind']}")
        for nct, t in trials.items():
            if not t["registry"]:
                continue
            ids = t["registry"]["ids"]
            at = [h for h in mentions([ev], ids) if h["role"] == "efficacy_basis"]
            earlier = [h for h in mentions(events, ids, before=ap["date"]) if h["role"] == "efficacy_basis"]
            t.update(named_at_event=at, named_earlier=earlier)
            if not at:
                why.append(f"{nct}: not named in an efficacy role in any Label/Review at {ap['date']}")
            if earlier and t["role"] == "lead":
                first = min(earlier, key=lambda h: h["date"])
                why.append(f"{nct}: already named at {first['date']} ({first['doc']} {first['file']} {first['ids']})")
        letters = [r for r in ev["doc_results"] if r["type"] == "Letter" and r["chars"]]
        conv = next((m for m in (CONVERT_RX.search(doc_text(r)) for r in letters) if m), None)
        acc = next((m for m in (ACCEL_RX.search(doc_text(r)) for r in letters) if m), None)
        found = "regular" if conv else ("accelerated" if acc else ("regular" if letters else None))
        m = conv or acc
        step_type = dict(entered=ap["type"], letter=found, converts_accelerated=bool(conv),
                         evidence=re.sub(r"\s+", " ", m.group(0)).strip()[:400] if m else None)
        if found is None:
            why.append("no approval letter text to check accelerated/regular")
        elif found != ap["type"]:
            why.append(f"letter reads {found}, entered {ap['type']}")
    lead_start = min((t["registry"]["start"] for t in trials.values() if t["registry"] and t["role"] == "lead"), default=None)
    precs = []
    for p in step.get("precursors", []):
        reg, w = check_trial(p["nct"], names)
        if reg and lead_start and reg["start"] and reg["start"] >= lead_start:
            w.append(f"{p['nct']}: starts {reg['start']}, not before lead start {lead_start}")
        docs = [h for h in mentions(events, reg["ids"], upto=ap["date"])] if reg else []
        precs.append(dict(p, registry=reg, basis="doc_supported" if docs else "registry_only", named_in=docs[:3], issues=w))
    # Human sign-off: a reason the reviewer accepted (listed by prefix in the step's "accept") no longer blocks the step.
    accepted = [w for w in why if any(w.startswith(a) for a in step.get("accept", []))]
    why = [w for w in why if w not in accepted]
    return dict(step=step["step"], disease=step["disease"], indication=step["indication"], phase_entered=step.get("phase_entered"),
                approval=ap, entered_by=step.get("entered_by"), status="needs_review" if why else ("verified_with_override" if accepted else "verified"), reasons=why, accepted=accepted,
                trials=trials, precursors=precs, type_check=step_type if ev else None)


def run():
    cur = json.load(open(CURATED, encoding="utf-8"))
    pilot = json.load(open(os.path.join(DATA, "approval_anchor_pilot.json"), encoding="utf-8"))
    out = dict(generated=date.today().isoformat(), source=os.path.relpath(CURATED, ROOT), drugs={})
    for drug, d in cur["drugs"].items():
        events = pilot["drugs"].get(drug, {}).get("events")
        if events is None:
            out["drugs"][drug] = dict(error="drug not in approval_anchor_pilot.json; add it to PILOT and rerun approval_anchor.py")
            continue
        steps = [verify_step(drug, d, s, events) for s in d["chain"]]
        out["drugs"][drug] = dict(chembl=d.get("chembl"), mechanism=d.get("mechanism"), application=d["application"],
                                  diseases=sorted({s["disease"] for s in steps}), chain=steps)
        for s in steps:
            print(f"{drug:<11} {s['approval']['date']} {s['step']:<8} {s['disease']:<4} {s['status']:<12} " + "; ".join(s["reasons"]))
            for p in s["precursors"]:
                print(f"{'':>24}precursor {p['nct']} {p['basis']} " + "; ".join(p["issues"]))
    json.dump(out, open(os.path.join(DATA, "approval_lineage_verified.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)


LINEAGE_RULE = ("15. Approval lineage is human-entered (data/curated/approval_lineage.json): each step is one FDA approval event and the trial(s) "
                "credited for it, across diseases. tools/lineage_verify.py checks registry presence, drug and phase, that a label or review at that event "
                "names the trial outside postmarketing-requirement context, that no earlier label or review did, and the accelerated/regular wording of the "
                "approval letter. Unverified steps are shown as needs_review with reasons, never dropped. Precursors are doc_supported or registry_only.")


def nct_of(node):
    a = node.get("attrs") or {}
    v = a.get("nct_id") or a.get("nct")
    if v:
        return v
    m = re.search(r":t:(NCT\d{8})$", node.get("id", ""))
    return m.group(1) if m else None


def attach_lineage(tree, verified=None):
    """Put the verified lineage on each drug node (attrs.approval_lineage) and a back-reference on every trial/study node of that drug
    whose NCT is in the lineage (attrs.lineage_ref), so the viewer can show the chain from any linked trial."""
    p = os.path.join(DATA, "approval_lineage_verified.json")
    if verified is None:
        if not os.path.exists(p):
            return 0
        verified = json.load(open(p, encoding="utf-8"))
    drugs = {f"drug:{d['chembl']}": (name, d) for name, d in verified["drugs"].items() if d.get("chembl") and "chain" in d}
    n_attached = 0

    def collect(node, out):
        v = nct_of(node)
        (node.get("attrs") or {}).pop("lineage_ref", None)
        if v and node.get("kind") in ("trial", "study"):
            out.setdefault(v, []).append(node)
        for c in node.get("children") or []:
            collect(c, out)

    def visit(node):
        nonlocal n_attached
        if node.get("kind") == "drug" and node["id"] in drugs:
            name, d = drugs[node["id"]]
            by_nct = {}
            collect(node, by_nct)

            def trial(nct, role, reg, extra=None):
                return dict(dict(nct=nct, role=role, phase=phase_key(reg["phases"]) if reg else None,
                                 start=reg and reg["start"], title=reg and reg["title"], sponsor=reg and reg["sponsor"],
                                 node_ids=[n["id"] for n in by_nct.get(nct, [])]), **(extra or {}))

            steps = []
            for s in d["chain"]:
                trials = [trial(nct, t["role"], t["registry"]) for nct, t in s["trials"].items()]
                trials += [trial(pr["nct"], "precursor", pr["registry"], dict(basis=pr["basis"], relation=pr.get("relation"), issues=pr["issues"]))
                           for pr in s["precursors"]]
                steps.append(dict(step=s["step"], disease=s["disease"], indication=s["indication"], date=s["approval"]["date"],
                                  submission=s["approval"]["submission"], type=s["approval"]["type"], kind=s["approval"]["kind"],
                                  status=s["status"], reasons=s["reasons"], accepted=s.get("accepted", []), entered_by=s.get("entered_by"), trials=trials))
                for t in trials:
                    for n in by_nct.get(t["nct"], []):
                        ref = n.setdefault("attrs", {}).setdefault("lineage_ref", dict(drug_node=node["id"], drug=name, steps=[]))
                        ref["steps"].append(dict(step=s["step"], role=t["role"], date=s["approval"]["date"], status=s["status"]))
            node.setdefault("attrs", {})["approval_lineage"] = dict(drug=name, mechanism=d.get("mechanism"), application=d["application"],
                                                                     diseases=d["diseases"], source="data/approval_lineage_verified.json", steps=steps)
            n_attached += 1
        for c in node.get("children") or []:
            visit(c)

    visit(tree)
    meta = tree.setdefault("meta", {})
    if LINEAGE_RULE not in meta.setdefault("rules", []):
        meta["rules"] = [r for r in meta["rules"] if not r.startswith("15. Approval lineage")] + [LINEAGE_RULE]
    meta.setdefault("counts", {})["approval_lineage_drugs"] = n_attached
    return n_attached


if __name__ == "__main__":
    run()
    if "--attach" in sys.argv:
        # Patch data/tree.json in place (no full rebuild). A full export_disease_tree.py run attaches the same way.
        out = os.path.join(DATA, "tree.json")
        tree = json.load(open(out, encoding="utf-8"))
        print("lineage attached to", attach_lineage(tree), "drug nodes")
        json.dump(tree, open(out, "w", encoding="utf-8"), indent=1, ensure_ascii=False, default=str)
