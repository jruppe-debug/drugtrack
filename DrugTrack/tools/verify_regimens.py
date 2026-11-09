"""Independent match of each approved-indication record (label side) to its basis trial (registry side).
Two directions must meet on one NCT:
  A. Registry waterfall (trial side): MeSH universe (mesh_universe.json.gz) -> trials carrying the labeled drug, by
     intervention MeSH id (DrugCentral or NLM MeSH id of the labeled UNII) or, where the registry never coded the drug
     (ziftomenib; Mylotarg in ALFA-0701), by intervention name -> RxCUI equal to the label's UNII -> RxCUI
     -> interventional -> started before the FDA approval. Signature filters (randomized; enrollment equals a section 14
     count) only narrow and are skipped when they would leave nothing.
  B. Approval package (label side): central_anchor.json lead NCT for the FDA event, and NCT ids printed in label section 14.
  Link level: printed_nct (section 14 prints an NCT the registry confirms) or signature (no printed NCT; the registry
  signature is one trial and it is the anchor).
Regimen gate: for each label phase, some EXPERIMENTAL arm of a confirmed trial must hold every ingredient of the regimen key,
its product token (P:) and an ingredient of each class slot (C: ATC prefix, e.g. an anthracycline); the filled class member is
reported. Trial ingredients outside the key are listed; config pk_probes (CYP probe drugs of PK sub-studies) are tagged
separately and never enter a key.
Records whose indication role is in the config excluded_roles (transplant conditioning) get status excluded_<role>.
Output: data/regimen_verification.json      Run: python tools/verify_regimens.py [--disease aml] [--trace DRUG]
"""
import argparse, gzip, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disease_config
from regimen_key import Crosswalk, arm_keys, parse_key, _pieces

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(DATA, "regimen_verification.json")


_IDX = {}
_DOCS = {}


def index_for(U, xw):
    """One UniverseIndex per universe snapshot (common.UniverseIndex): every drug -> trials lookup becomes a dict read."""
    from common import universe_index
    k = U.get("generated"), U.get("scope_id")
    if k not in _IDX:
        _IDX[k] = universe_index(U, xw)            # stored index when its inputs are unchanged
    return _IDX[k]


def lab_mesh_ids(rec, U, xw):
    idx = index_for(U, xw)
    out, how = set(), {}
    for l in rec["labeled_ingredients"]:
        if l["mesh"]:
            out |= set(l["mesh"])
            how[l["id"]] = l.get("mesh_route", "drugcentral")
        for r in l["rxcui"]:
            n = (xw.in_name(r) or "").lower()
            if n in idx.heads and not (idx.heads[n] <= out):
                out |= idx.heads[n]
                how[l["id"]] = (how.get(l["id"], "") + "+" if l["id"] in how else "") + "mesh heading == RxNorm name"
    if (rec.get("fixed_combination") or {}).get("mesh"):
        out.add(rec["fixed_combination"]["mesh"])
    return out, how


def names_reach(xw, trial, lab_rx, use_rxnav):
    """Live RxNav name check for one trial (kept for the few package trials the local index cannot resolve)."""
    for iv in trial.get("interventions", []):
        for n in [iv["name"]] + list(iv.get("other_names") or []):
            for p in [n] + _pieces(n):
                if xw.syn_ingredients(p) & lab_rx or (use_rxnav and xw.rx_name(p) & lab_rx):
                    return True
    return False


def acronym_hits(names, cands, T, U=None, xw=None):
    """Label section 14 trial names -> candidate trials by registry acronym or whole title token (index lookup)."""
    if U is not None:
        return index_for(U, xw).by_name(names, set(cands))
    out = {}
    for n in cands:
        t = T[n]
        acr = re.sub(r"[\s\-_]", "", (t.get("acronym") or "").upper())
        titles = " ".join(x for x in (t.get("brief_title"), t.get("official_title")) if x)
        hit = [nm for nm in names if (acr and acr == re.sub(r"[\s\-_]", "", nm.upper()))
               or re.search(r"(?<![A-Za-z0-9-])" + re.escape(nm) + r"(?![A-Za-z0-9-])", titles)]
        if hit:
            out[n] = hit
    return out


def event_docs(app, date, log_to=None):
    """FDA documents of one Drugs@FDA submission (bulk tables), fetched once, normalized once (common.DocText)."""
    from approval_anchor import fetch_doc, review_pdfs
    from common import DocText
    import store
    k = (app, date)
    if k in _DOCS:
        return _DOCS[k]
    urls = [d["url"] for s in store.drugsfda_submissions(app) if s.get("submission_status_date") == date
            for d in s.get("application_docs", []) if d.get("type") in ("Label", "Review", "Summary Review", "Letter")]
    docs, texts = [], []
    for u in review_pdfs(urls):
        if log_to:
            disease_config.log(log_to, "verify", f"    fetch {u}")
        try:
            path, text, status = fetch_doc(u, app)
        except Exception as e:
            path, text, status = None, "", f"error {type(e).__name__}"
        docs.append(dict(url=u, status=status, chars=len(text or "")))
        if text:
            texts.append(text)
    _DOCS[k] = (docs, DocText("\n".join(texts)) if texts else None)
    return _DOCS[k]


def package_search(rec, cands, T):
    """FDA approval package of this record's own Drugs@FDA event searched for each candidate trial's registry ids (NCT, org
    study id, secondary ids, acronym). Exact whole-token hits; a trial named only in postmarketing-requirement context is
    confirmatory_pmr, not efficacy evidence. Documents come from event_docs (normalized once per event)."""
    from approval_anchor import hit_role, id_context
    f = rec["approval"]["fda"]
    app, date = f.get("application"), (f.get("date") or "").replace("-", "")
    out = dict(application=app, date=f.get("date"), docs=[], hits={})
    if not app or not date:
        return out
    docs, doc = event_docs(app, date, rec["disease"]["config"])
    out["docs"] = docs
    if doc is None:
        return out
    for n in sorted(cands):
        t = T[n]
        ids = [("nct", n)] + ([("org_study_id", t["org_id"])] if t.get("org_id") else []) +               [("secondary_id", x) for x in t.get("secondary_ids", [])] + ([("acronym", t["acronym"])] if t.get("acronym") else [])
        hit_ids = [i for i in ids if doc.has(*i)]
        if hit_ids:
            out["hits"][n] = dict(ids=[f"{k}:{v}" for k, v in hit_ids], role=hit_role(doc.text, hit_ids),
                                  mentions=len(id_context(doc.text, hit_ids, 0)))
    return out


def verify(rec, U, xw, cfg, pk_rx):
    T = U["trials"]
    steps = []
    lab_rx = {r for l in rec["labeled_ingredients"] for r in l["rxcui"]}
    partner_rx = {r for d in rec["phases"].values() for alt in d["alternatives"] for p in alt for r in p.get("rxcui", [])}
    lab_mesh, mesh_how = lab_mesh_ids(rec, U, xw)
    date = rec["approval"]["fda"]["date"]
    s = rec["studies"]
    anchor = rec["approval"]["fda"]["anchor_nct"]
    printed = set(s["ncts"])
    package = ({anchor} if anchor else set()) | printed

    def step(name, rule, ncts, narrow_only=False, prev=None):
        if narrow_only and not ncts:
            steps.append(dict(step=name, rule=rule + "  (0 matches: filter skipped)", n=len(prev), ncts=None, skipped=True))
            return prev
        steps.append(dict(step=name, rule=rule, n=len(ncts), ncts=sorted(ncts) if len(ncts) <= 12 else None))
        return ncts

    disease_config.log(cfg["_name"], "verify", f"start {rec['record_id']}")
    c = step("universe", f"condition MeSH in {sorted(U['disease_mesh'])} (direct or ancestor)", set(T))
    idx = index_for(U, xw)
    by_mesh = idx.ncts_for(mesh=lab_mesh) & c
    by_name = (idx.ncts_for(rxcuis=lab_rx) & c) - by_mesh
    by_name |= {n for n in (package & c) - by_mesh - by_name if names_reach(xw, T[n], lab_rx, use_rxnav=True)}
    c = step("drug", f"intervention MeSH in {sorted(lab_mesh)} [{len(by_mesh)}] or intervention name -> label RxCUI "
                     f"{sorted(lab_rx)} [{len(by_name)}: DrugCentral synonyms; RxNav for package trials]", by_mesh | by_name)
    c = step("interventional", "study_type = INTERVENTIONAL", {n for n in c if T[n]["study_type"] == "INTERVENTIONAL"})
    if date:
        c = step("chronology", f"start date before FDA approval {date}", {n for n in c if (T[n]["start"] or "9999") < date})
    base = c
    if "randomized" in s["design"]:
        c = step("randomized", "section 14 says randomized -> allocation = RANDOMIZED",
                 {n for n in c if T[n]["allocation"] == "RANDOMIZED"}, True, c)
    if s["enrolled"]:
        c = step("enrollment", f"enrollment equals a section 14 count {s['enrolled']}",
                 {n for n in c if T[n]["enrollment"] in s["enrolled"]}, True, c)
    signature = c
    confirmed = package & base
    package_info = None
    gates = dict(package_names_trial=bool(package), registry_confirms_package=bool(confirmed))
    if printed:
        level = "printed_nct" if printed & base else None
    elif anchor:
        level = "signature" if len(signature) == 1 and signature == ({anchor} & base) else None
    elif acronym_hits(s.get("trial_names", []), base, T, U, xw):
        named = acronym_hits(s["trial_names"], base, T, U, xw)  # section 14 prints the trial name, the registry holds it
        level, confirmed = "printed_trial_name", set(named)
        gates["package_names_trial"] = gates["registry_confirms_package"] = True
        package_info = dict(trial_names=named)
    else:                                       # no FDA anchor yet (scope_labels input): search this event's FDA documents
        disease_config.log(cfg["_name"], "verify", f"  package search {rec['approval']['fda'].get('application')} {rec['approval']['fda'].get('date')}: {len(base)} candidate trials")
        pkg = package_search(rec, base, T)
        disease_config.log(cfg["_name"], "verify", f"  package docs={len(pkg['docs'])} hits={ {n: h['ids'] for n, h in pkg['hits'].items()} }")
        hits = {n: h for n, h in pkg["hits"].items() if h["role"] == "efficacy_basis" and n in base}
        both = set(hits) & signature
        if both:                                    # named in the package AND passes the label signature filters
            level, confirmed = "package_document+signature", both
        elif len(signature) == 1:                   # package silent on the signature trial (Rydapt: review says 'A2301')
            level, confirmed = "signature_registry_only", set(signature)
        elif hits:                                  # no unique signature: the trial(s) the package mentions most
            top = max(h["mentions"] for h in hits.values())
            level, confirmed = "package_document_most_mentioned", {n for n, h in hits.items() if h["mentions"] == top}
        else:
            level = None
        if level:
            gates["package_names_trial"] = gates["registry_confirms_package"] = True
        package_info = pkg
    gates["label_to_trial_link"] = bool(level)
    arms_by = {n: arm_keys(xw, T[n], label_rx=lab_rx | partner_rx, pk_rx=pk_rx) for n in sorted(confirmed)}
    phase_checks = {}
    for ph, keys in rec["regimen_keys"].items():
        res = []
        for k in keys:
            need = parse_key(k)
            hit = None
            for n, arms in arms_by.items():
                for a in arms:
                    if a.get("placebo") and not (parse_key(a["key"])["rx"] & lab_rx):
                        continue                        # treatment arm = holds the labeled drug; the registry arm type is not trusted
                    ak = parse_key(a["key"])
                    if not (ak["rx"] & lab_rx):
                        continue
                    if not (need["rx"] <= ak["rx"] and need["products"] <= ak["products"]):
                        continue
                    filled = {}
                    for cl in need["classes"]:
                        mem = sorted(r for r in ak["rx"] - need["rx"] if any(x.startswith(cl) for x in xw.rx_atc(r)))
                        if mem:
                            filled[cl] = {r: xw.in_name(r) for r in mem}
                    if len(filled) == len(need["classes"]):
                        hit = (n, a, filled)
                        break
                if hit:
                    break
            if hit:
                used = need["rx"] | {r for v in hit[2].values() for r in v}
                extra = sorted(parse_key(hit[1]["key"])["rx"] - used)
            res.append(dict(key=k, nct=hit[0] if hit else None, experimental_arm=hit[1]["label"] if hit else None,
                            class_filled=hit[2] if hit else None,
                            extra_in_trial={r: xw.in_name(r) for r in extra} if hit else None,
                            pk_probes_in_arm={r: xw.in_name(r) for r in hit[1]["pk_probes"]} if hit else None))
        phase_checks[ph] = res
    gates["arm_contains_label_key"] = bool(phase_checks) and all(r["nct"] for v in phase_checks.values() for r in v)
    conflicts = [l["id"] for l in rec["labeled_ingredients"] if l["agree"] == "conflict"]
    conflicts += [p["name"] for d in rec["phases"].values() for alt in d["alternatives"] for p in alt
                  if not p.get("class_word") and not p.get("rxcui")]
    gates["identity_no_conflict"] = not conflicts
    design = {}
    for n, arms in arms_by.items():
        exp = [a for a in arms if a["type"] == "EXPERIMENTAL"]
        comp = [a for a in arms if a["type"] in ("ACTIVE_COMPARATOR", "PLACEBO_COMPARATOR", "SHAM_COMPARATOR")]
        if not comp:
            design[n] = "single_arm" if len(exp) == 1 else "multi_arm_no_comparator"
        else:
            e = parse_key(exp[0]["key"])["rx"] if exp else set()
            design[n] = "add_on" if any(parse_key(a["key"])["rx"] == e - lab_rx for a in comp) else "head_to_head_or_other"
    probes = sorted({xw.in_name(r) for arms in arms_by.values() for a in arms for r in a["pk_probes"]})
    fails = [g for g, ok in gates.items() if not ok]
    disease_config.log(cfg["_name"], "verify", "  gates " + " ".join(f"{g}={'ok' if ok else 'FAIL'}" for g, ok in gates.items()))
    role = rec["indication"]["role"]
    orig = rec["approval"]["fda"].get("original_date")
    if rec["indication"].get("excluded"):
        status = f"excluded_{role}"
    elif orig and orig < cfg.get("registry_start", "2000-01-01") and fails and rec["approval"]["fda"].get("in_first_version", True):
        status = "pre_registry_backbone"          # approved before ClinicalTrials.gov: supplemental, no NCT expected
    else:
        status = f"verified_{level}" if not fails else "needs_review"
    return dict(record_id=rec["record_id"], status=status, excluded_reason=rec["indication"].get("excluded"),
                basis_ncts=sorted(confirmed), failed=fails, gates=gates, identity_routes=mesh_how, conflicts=conflicts,
                design=design, chemo_eligibility=rec["indication"]["chemo_eligibility"], setting=rec["indication"]["setting"],
                biomarker=rec["indication"]["biomarker"], regimen_source=rec.get("regimen_source"),
                pk_probes=dict(drugs=probes, phases={n: T[n]["phases"] for n in arms_by} if probes else {},
                               note=cfg["pk_probes_note"]) if probes else None,
                waterfall=steps, signature=sorted(signature) if len(signature) <= 12 else len(signature),
                package=dict(anchor_nct=anchor, anchor_ids=rec["approval"]["fda"]["anchor_ids"], label_s14_ncts=sorted(printed),
                             document_search=package_info),
                phase_checks=phase_checks, arms=arms_by)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--disease", default="aml")
    ap.add_argument("--trace")
    a = ap.parse_args()
    cfg = disease_config.load(a.disease)
    ddir = disease_config.data_dir(a.disease)
    U = json.load(gzip.open(os.path.join(ddir, "mesh_universe.json.gz"), "rt", encoding="utf-8"))
    recs = json.load(open(os.path.join(ddir, "indication_records.json"), encoding="utf-8"))["records"]
    xw = Crosswalk()
    pk_rx = set()
    for n in cfg["pk_probes"]:
        pk_rx |= xw.rx_name(n)
    from approval_anchor import fetch_doc, review_pdfs
    import common, store                            # cold runs: every event's FDA documents fetched 4 at a time first
    ev_urls = {(f["application"], u) for r in recs for f in [r["approval"]["fda"]] if f.get("application") and f.get("date")
               for s in store.drugsfda_submissions(f["application"]) if s.get("submission_status_date") == f["date"].replace("-", "")
               for u in review_pdfs([d["url"] for d in s.get("application_docs", [])
                                     if d.get("type") in ("Label", "Review", "Summary Review", "Letter")])}
    common.prefetch(lambda app, u: fetch_doc(u, app), sorted(ev_urls), 4, "verify", a.disease)
    res = []
    for r in recs:
        if a.trace and r["drug"] != a.trace:
            continue
        res.append(verify(r, U, xw, cfg, pk_rx))
        v = res[-1]
        disease_config.log(a.disease, "verify", f"done {v['record_id']}: {v['status']} basis={v['basis_ncts']} failed={v['failed']} | " + " > ".join(f"{s['step']} {s['n']}" for s in v["waterfall"]))
    if not a.trace:
        json.dump(dict(disease=a.disease, universe_generated=U["generated"], results=res), open(os.path.join(ddir, "regimen_verification.json"), "w", encoding="utf-8"), indent=1)
    for r in res:
        print(f"{r['record_id']:42s} {r['status']:32s} basis={r['basis_ncts']} failed={r['failed']}")
        if a.trace:
            print("   eligibility", r["chemo_eligibility"], "| setting", r["setting"], r["biomarker"], "| source", r["regimen_source"])
            for s in r["waterfall"]:
                print(f"   {s['step']:15s} {s['n']:5d}  {s['rule']}" + (f"  {s['ncts']}" if s["ncts"] else ""))
            print("   package", r["package"], "| design", r["design"], "| pk_probes", r["pk_probes"] and r["pk_probes"]["drugs"])
            print("   phases", json.dumps(r["phase_checks"]))
            for n, arms in r["arms"].items():
                for arm in arms[:6]:
                    print("   arm", n, arm["type"], arm["key"], "pk:", arm["pk_probes"], "placebo" if arm["placebo"] else "",
                          "unconfirmed:", arm["unconfirmed"])
    n_ok = sum(r["status"].startswith("verified") for r in res)
    n_ex = sum(r["status"].startswith("excluded") for r in res)
    print(f"verified {n_ok} / {len(res) - n_ex} in scope ({n_ex} excluded by role)")


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
    INP = [os.path.join(D, "indication_records.json"), os.path.join(D, "mesh_universe.json.gz")]
    OUT = [_os.path.join(D, x) for x in ["regimen_verification.json"]]
    CODE = ["verify_regimens.py", "regimen_key.py", "common.py", "approval_anchor.py", "store.py", "disease_config.py"]
    if a.trace or "show" in _sys.argv or "pull" in _sys.argv or "--help" in _sys.argv:
        main()
    elif not common.stage_current(a.disease, "verify", INP, OUT, CODE, a.force):
        main()
        common.stage_done(a.disease, "verify", INP, OUT, CODE)
