"""Treatment finder backend (HealthTree-style, no HemOnc): which treatments are available at which stage for a patient
situation, with efficacy commentary. Built only from the generic pipeline outputs + AACT results/eligibility in the store.
APPROVED options (verified records, excluded roles dropped; newest event per drug + regimen key + setting):
  stage       from the record setting: newly diagnosed / previously untreated -> first-line; maintenance -> maintenance;
              relapsed or refractory / previously treated -> relapsed-refractory; none stated -> any line
  population  setting, required biomarkers, chemo eligibility, pediatric (label section 1)
  efficacy    label section 14 tables tagged to this regimen (label_efficacy.json; per-column regimen when one table holds
              several) + registry primary outcomes of the lead trial (AACT results)
  timeline    every trial of the drug started before this approval, ordered: first-in-human -> phase 1 -> 1/2 -> 2 -> 3,
              each marked same regimen (an arm holds every ingredient of the regimen key) / drug alone / other combination,
              plus the pivotal readout and approval letter; checks: phase progression, same-regimen precursor (combinations),
              lead read out before approval, chronology.
INVESTIGATIONAL options (every live interventional treatment trial in the disease universe; status recruiting / not yet
recruiting / active not recruiting / enrolling by invitation):
  regimen     per experimental arm: ingredients resolved locally (DrugCentral synonyms, MeSH entry terms, cached RxNav);
              unresolved code names kept by name. Arms with the same key are one regimen; trials listed under it.
  population  tags from title + conditions + keywords + INCLUSION criteria text (closed config vocabularies: settings,
              biomarkers, eligibility; prior-therapy drugs named next to 'prior / previously treated / refractory to';
              age band from AACT eligibility flags). Text-derived tags are marked source="text".
  efficacy    own posted registry results for the experimental arm; else results of earlier completed trials with the same
              regimen key; else 'no posted readout' (with the PubMed NCT[si] query to check).
Output: <data_dir>/finder.json      Run: python tools/treatment_finder.py --disease cll
"""
import argparse, json, os, re, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disease_config, store
from common import load_outputs, norm_name, SALT, SPLIT

LIVE = {"RECRUITING", "NOT_YET_RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION"}
PRANK = {"EARLY_PHASE1": 0.5, "PHASE1": 1, "PHASE1/PHASE2": 2, "PHASE2": 3, "PHASE2/PHASE3": 3.5, "PHASE3": 4, "PHASE4": 5}
PRIOR = re.compile(r"(?:prior|previous(?:ly)?|refractory to|relapsed (?:after|on)|failure of|progress(?:ed|ion) (?:on|after)|intoleran\w+ to|exposed to)\W+(?:\w+\W+){0,6}", re.I)
EFFICACY = ("ORR", "CR", "MRD", "PFS", "EFS", "OS", "DOR", "DFS", "TTP")


def stage_of(settings):
    s = set(settings)
    if s & {"first complete remission maintenance"}:
        return "maintenance"
    if s & {"newly diagnosed", "previously untreated"} and not s & {"relapsed or refractory", "previously treated"}:
        return "first-line"
    if s & {"relapsed or refractory", "previously treated"} and not s & {"newly diagnosed", "previously untreated"}:
        return "relapsed-refractory"
    return "any line" if not s else "first-line and relapsed-refractory"


class LocalResolver:
    """Intervention name -> RxNorm ingredients, local sources only (no network)."""

    def __init__(self, xw):
        self.xw, self.mi, self.cache = xw, store.mesh_index(), {}

    def __call__(self, name):
        k = norm_name(name)
        if k not in self.cache:
            r = set(self.xw.syn_ingredients(k)) | set(self.xw.syn_ingredients(SALT.sub("", k))) | self.xw.rx_name_cached(k)
            for ui in self.mi["term"].get(k, []):
                r |= self.xw.dc_ingredients(ui)
            self.cache[k] = r
        return self.cache[k]

    def arms(self, t, pk_rx):
        """experimental arms -> (ingredient rxcuis, unresolved names)."""
        out = []
        by_arm = defaultdict(list)
        for iv in t.get("interventions", []):
            if iv["type"] not in ("DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT", "GENETIC"):
                continue
            names = [iv["name"]] + list(iv.get("other_names") or [])
            if any(re.search(r"(?<![a-z])(?:placebo|sham|vehicle)(?![a-z])", n, re.I) for n in names):
                continue
            rx = set()
            for n in names:
                for p in [n] + [x.strip() for x in SPLIT.split(n) if x and len(x.strip()) >= 3]:
                    rx |= self(p)
            for lab in iv.get("arms") or ["(all arms)"]:
                by_arm[lab].append((rx - pk_rx, None if rx else norm_name(iv["name"])))
        types = {a["label"]: a["type"] for a in t.get("arms", [])}
        for lab, items in by_arm.items():
            if types.get(lab) in ("PLACEBO_COMPARATOR", "SHAM_COMPARATOR", "NO_INTERVENTION"):
                continue
            rx = set().union(*[i[0] for i in items]) if items else set()
            un = sorted({i[1] for i in items if i[1]})
            if rx or un:
                out.append(dict(arm=lab, arm_type=types.get(lab), rx=sorted(rx, key=int), unresolved=un))
        return out


def regimen_key(rx, un):
    return "+".join(sorted(rx, key=int)) + ("|N:" + "+".join(un) if un else "")


def results_for(ncts, group_hint=None):
    """Registry efficacy results (AACT) for trials: per endpoint the experimental-group values (group title hint first)."""
    if not ncts:
        return {}
    rows = store.con().execute(
        "SELECT o.nct, o.type, o.endpoint, o.title, o.time_frame, m.group_title, m.param_type, m.value, m.units, m.lo, m.hi "
        "FROM aact_outcome o JOIN aact_measure m ON m.outcome_id = o.outcome_id WHERE o.nct IN (SELECT unnest(?)) "
        "AND o.endpoint IN (SELECT unnest(?)) ORDER BY o.nct, o.type DESC, o.outcome_id", [sorted(ncts), list(EFFICACY)]).fetchall()
    an = store.con().execute("SELECT a.nct, o.endpoint, a.param_type, a.value, a.ci_lo, a.ci_hi, a.p_value FROM aact_analysis a "
                             "JOIN aact_outcome o ON o.outcome_id = a.outcome_id WHERE a.nct IN (SELECT unnest(?)) AND o.endpoint IN (SELECT unnest(?))",
                             [sorted(ncts), list(EFFICACY)]).fetchall()
    out = defaultdict(list)
    for nct, typ, ep, title, tf, grp, pt, val, units, lo, hi in rows:
        if len(out[nct]) >= 12:
            continue
        out[nct].append(dict(type=typ, endpoint=ep, title=(title or "")[:120], time_frame=(tf or "")[:80], group=(grp or "")[:80],
                             param=pt, value=val, units=units, ci=[lo, hi] if lo or hi else None))
    for nct, ep, pt, val, lo, hi, p in an:
        if pt and re.search(r"hazard|odds", pt, re.I):
            out[nct].append(dict(type="ANALYSIS", endpoint=ep, param=pt, value=val, ci=[lo, hi], p_value=p))
    return dict(out)


def tags_from_text(text, cfg, vocab_rx):
    t = text or ""
    tags = dict(setting=sorted(k for k, r in cfg["_settings"] if r.search(t)),
                biomarker=sorted({b for b in cfg.get("biomarkers", []) if re.search(r"(?<![\w-])" + re.escape(b) + r"(?![A-Za-z0-9])", t)}),
                eligibility=("ineligible_for_intensive_chemo" if cfg["_inelig"].search(t) else
                             "eligible_for_intensive_chemo" if cfg["_elig"].search(t) else None))
    prior = set()
    for m in PRIOR.finditer(t):
        prior |= {x.group(1).lower() for x in vocab_rx.finditer(t[m.start():m.end() + 60])}
    tags["prior_therapy"] = sorted(prior)[:8]
    return tags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--disease", required=True)
    a = ap.parse_args()
    cfg = disease_config.load(a.disease)
    R, V, L, E, U = load_outputs(a.disease, "indication_records.json", "regimen_verification.json", "development_lineage.json",
                                 "label_efficacy.json", "mesh_universe.json.gz")
    T = U["trials"]
    from regimen_key import Crosswalk
    xw = Crosswalk()
    res = LocalResolver(xw)
    pk_rx = set()
    for n in cfg["pk_probes"]:
        pk_rx |= res(n)
    from indication_records import PartnerVocab
    vocab = PartnerVocab(xw)
    ver = {v["record_id"]: v for v in V["results"]}
    rec = {r["record_id"]: r for r in R["records"]}
    in_name = {}

    def iname(r):
        if r not in in_name:
            in_name[r] = xw.in_name(r) if r in res.xw.__dict__.get("_x", {}) or True else r
        return in_name[r]

    # ---------- approved
    approved, approved_keys = {}, set()
    for rid, v in ver.items():
        if not v["status"].startswith("verified"):
            continue
        r = rec[rid]
        base = r["drug"].split(" @")[0].split(" [")[0]
        lin = L["drugs"].get(base, {})
        ev = next((e for e in lin.get("events", []) if e["record_id"] == rid), {})
        for ph, keys in r["regimen_keys"].items():
            for k in keys:
                rx = [x for x in k.split("|")[0].split("+") if x]
                key = (base, k, tuple(sorted(r["indication"]["setting"])))
                date = r["approval"]["fda"].get("date") or ""
                if key in approved and approved[key]["approval_date"] >= date:
                    continue
                tabs = []
                for tb in E.get(rid, {}).get("tables", []):
                    if any(x["key"] == k for x in tb["regimens"]):
                        cols = [c for c, kk in tb["column_regimen"].items() if kk == k]
                        tabs.append(dict(caption=tb["caption"], nct=tb["nct"], columns=[tb["columns"][0]] + [tb["columns"][int(c)] for c in cols] if cols else tb["columns"],
                                         rows=[dict(endpoint=x["endpoint"], label=x["label"], cells=[x["cells"][int(c) - 1] for c in cols if int(c) - 1 < len(x["cells"])] if cols else x["cells"])
                                               for x in tb["efficacy_rows"]], hazard_ratios=tb["hazard_ratios"]))
                approved[key] = dict(drug=base, regimen_key=k, phase=ph, ingredients=[xw.in_name(x) for x in rx], approval_date=date,
                                     application=r["approval"]["fda"].get("application"), stage=stage_of(r["indication"]["setting"]),
                                     population=dict(setting=r["indication"]["setting"], biomarker=r["indication"]["biomarker"],
                                                     eligibility=r["indication"]["chemo_eligibility"]["value"],
                                                     pediatric="pediatric" in r["indication"]["setting"]),
                                     indication=r["indication"]["text"][:500], lead=ev.get("lead"), link=v["status"],
                                     label_efficacy=tabs, letter=ev.get("letter"), record_id=rid)
                approved_keys.add(k.split("|")[0])
    # registry results of leads + pre-approval timeline
    leads = {o["lead"] for o in approved.values() if o["lead"]}
    lead_res = results_for(leads)
    arm_cache = {}

    def arms_of(n):
        if n not in arm_cache:
            arm_cache[n] = res.arms(T[n], pk_rx) if n in T else []
        return arm_cache[n]

    for o in approved.values():
        o["registry_results"] = [x for x in lead_res.get(o["lead"], []) if x["type"] in ("PRIMARY", "ANALYSIS")][:6]
        lin = L["drugs"].get(o["drug"], {})
        need = set(x for x in o["regimen_key"].split("|")[0].split("+") if x)
        drug_rx = set(need)
        tl = []
        for t in sorted(lin.get("trials", []), key=lambda t: t["start"] or "9999"):
            if (t["start"] or "9999") >= o["approval_date"]:
                continue
            arms = arms_of(t["nct"])
            rel = "same regimen" if any(need <= set(x["rx"]) for x in arms) else ("drug in other regimen" if arms else "unresolved arms")
            tl.append(dict(nct=t["nct"], phase=t["phase"], start=t["start"], primary_completion=t["primary_completion"],
                           results_posted=t["results_posted"], status=t["status"], role=t["role"], relation=rel, acronym=t["acronym"]))
        fih = (lin.get("first_in_human") or {}).get("first")
        lead_t = T.get(o["lead"], {})
        lp = PRANK.get("/".join(lead_t.get("phases") or []), 0)
        art = ((lin.get("readouts") or {}).get(o["lead"]) or {}).get("primary_article") or {}
        pub = (art.get("pubdate") or "").replace("/", "-")[:10]
        earlier_phase = [x for x in tl if PRANK.get(x["phase"], 0) < lp and (x["start"] or "9999") < (lead_t.get("start") or "")]
        if fih and fih["start"] < (lead_t.get("start") or "") and lp > 1:
            earlier_phase.append(dict(nct=fih["nct"]))
        chk = dict(first_in_human_before=bool(fih and fih["start"] < o["approval_date"]),
                   # only meaningful for a phase 2/3+ lead; registry first-in-human (any disease) counts as an earlier phase
                   phase_progression=lp < 3 or bool(earlier_phase),
                   same_regimen_precursor=(len(need) < 2) or any(x["relation"] == "same regimen" and x["nct"] != o["lead"]
                                                                 and (x["start"] or "9999") < (lead_t.get("start") or "") for x in tl),
                   # registry primary completion is often the final-analysis date; posted results or the primary
                   # publication before the approval also show the readout came first
                   lead_readout_before_approval=bool((lead_t.get("primary_completion") or "9999") <= o["approval_date"] or
                                                     (lead_t.get("results_first_posted") or "9999") <= o["approval_date"] or
                                                     (pub and pub <= o["approval_date"])),
                   chronology=bool(lead_t.get("start") and lead_t["start"] < o["approval_date"]))
        o["timeline"] = dict(first_in_human=fih, trials=tl, checks=chk, failed=[k for k, v in chk.items() if not v])
    # ---------- investigational
    purpose = dict(store.con().execute("SELECT nct, primary_purpose FROM aact_trial WHERE nct IN (SELECT unnest(?))", [sorted(T)]).fetchall())
    elig = {n: (c, ad, ch, old) for n, c, ad, ch, old in store.con().execute(
        "SELECT nct, criteria, adult, child, older_adult FROM aact_elig WHERE nct IN (SELECT unnest(?))", [sorted(T)]).fetchall()}
    kw = {n: json.loads(k or "[]") for n, k in store.con().execute("SELECT nct, keywords FROM aact_trial WHERE nct IN (SELECT unnest(?))", [sorted(T)]).fetchall()}
    # Live-enrolling regimens (inv) AND regimens with NO live arm left but a
    # trial that has posted results (recent) - previously that case was only
    # ever used as borrowed efficacy evidence for a *different* live regimen
    # sharing the same key (completed_by_key); a regimen whose every trial
    # already completed/terminated-with-results had no representation of its
    # own. "recent" surfaces those directly - the development-landscape view
    # of what just read out, not only what's still enrolling.
    inv, recent = {}, {}
    for n, t in T.items():
        if t["study_type"] != "INTERVENTIONAL" or (purpose.get(n) or "").upper() not in ("TREATMENT", ""):
            continue
        is_live = t["status"] in LIVE
        read_out = not is_live and bool(t.get("results_first_posted"))
        if not is_live and not read_out:
            continue
        crit = (elig.get(n) or ("",))[0] or ""
        inclusion = re.split(r"exclusion criteria", crit, flags=re.I)[0]
        text = " ".join([t.get("brief_title") or "", t.get("official_title") or "", " ".join(t.get("conditions", [])),
                         " ".join(kw.get(n, [])), inclusion])
        tg = tags_from_text(text, cfg, vocab.rx)
        e = elig.get(n)
        tg["age"] = [x for x, f in (("adult", e and e[1]), ("child", e and e[2]), ("older adult", e and e[3])) if f]
        for arm in arms_of(n):
            k = regimen_key(arm["rx"], arm["unresolved"])
            bucket = inv if is_live else recent
            g = bucket.setdefault(k, dict(regimen_key=k, ingredients=[xw.in_name(x) for x in arm["rx"]] + arm["unresolved"],
                                          contains_approved=sorted({xw.in_name(x) for x in arm["rx"] if x in {y for kk in approved_keys for y in kk.split("+")}}),
                                          is_approved_regimen=k.split("|")[0] in approved_keys and not arm["unresolved"], trials=[]))
            if not any(x["nct"] == n for x in g["trials"]):
                g["trials"].append(dict(nct=n, phase="/".join(t.get("phases") or []), status=t["status"], start=t["start"],
                                        primary_completion=t.get("primary_completion"), results_posted=t.get("results_first_posted"),
                                        why_stopped=t.get("why_stopped"), enrollment=t["enrollment"],
                                        title=(t.get("brief_title") or "")[:140], arm=arm["arm"][:100],
                                        population=dict(tg, source="text: title, conditions, keywords, inclusion criteria")))
    all_live = sorted({x["nct"] for g in inv.values() for x in g["trials"]})
    all_recent = sorted({x["nct"] for g in recent.values() for x in g["trials"]})
    own = results_for(all_live)
    recent_res = results_for(all_recent)

    def finalize(groups, res_map, source_label):
        for g in groups.values():
            g["trials"].sort(key=lambda x: (-PRANK.get(x["phase"], 0), x["start"] or ""))
            g["top_phase"] = g["trials"][0]["phase"]
            own_r = {x["nct"]: res_map[x["nct"]][:6] for x in g["trials"] if res_map.get(x["nct"])}
            g["efficacy"] = (dict(source=source_label, results=own_r) if own_r else
                             dict(source="no posted readout", pubmed_queries=[f"{x['nct']}[si]" for x in g["trials"][:5]]))
            pops = [x["population"] for x in g["trials"]]
            g["population_union"] = {f: sorted({v for p in pops for v in (p.get(f) or [])}) for f in ("setting", "biomarker", "prior_therapy", "age")}
            g["stage"] = stage_of(g["population_union"]["setting"])

    finalize(recent, recent_res, "posted registry results of this recent readout")
    for k, g in inv.items():
        g["trials"].sort(key=lambda x: (-PRANK.get(x["phase"], 0), x["start"] or ""))
        g["top_phase"] = g["trials"][0]["phase"]
        own_r = {x["nct"]: own[x["nct"]][:6] for x in g["trials"] if own.get(x["nct"])}
        prev = (recent.get(k) or {}).get("efficacy", {}).get("results")
        g["efficacy"] = (dict(source="posted registry results of these live trials", results=own_r) if own_r else
                         dict(source="posted registry results of earlier completed trials of the same regimen", results=prev) if prev else
                         dict(source="no posted readout", pubmed_queries=[f"{x['nct']}[si]" for x in g["trials"][:5]]))
        pops = [x["population"] for x in g["trials"]]
        g["population_union"] = {f: sorted({v for p in pops for v in (p.get(f) or [])}) for f in ("setting", "biomarker", "prior_therapy", "age")}
        g["stage"] = stage_of(g["population_union"]["setting"])
    inv_list = sorted(inv.values(), key=lambda g: (-PRANK.get(g["top_phase"], 0), -len(g["trials"])))
    recent_list = sorted(recent.values(),
                         key=lambda g: max((x["results_posted"] or x["primary_completion"] or "") for x in g["trials"]),
                         reverse=True)
    out = dict(disease=a.disease, questions=dict(settings=list(cfg["settings"]), biomarkers=cfg.get("biomarkers", []),
                                                 eligibility=["eligible_for_intensive_chemo", "ineligible_for_intensive_chemo"],
                                                 age=["adult", "older adult", "child"]),
               approved=sorted(approved.values(), key=lambda o: (o["stage"], o["approval_date"]), reverse=False),
               investigational=inv_list,
               recent_read_out=recent_list,
               counts=dict(approved_options=len(approved), investigational_regimens=len(inv_list), live_trials=len(all_live),
                           recent_readouts=len(recent_list), recent_readout_trials=len(all_recent),
                           live_with_own_results=sum(1 for g in inv_list if g["efficacy"]["source"].startswith("posted registry results of these")),
                           live_with_prior_results=sum(1 for g in inv_list if "earlier" in g["efficacy"]["source"]),
                           timeline_checks_failed={k: sum(1 for o in approved.values() if k in o["timeline"]["failed"]) for k in
                                                   ("first_in_human_before", "phase_progression", "same_regimen_precursor", "lead_readout_before_approval", "chronology")}))
    p = os.path.join(disease_config.data_dir(a.disease), "finder.json")
    json.dump(out, open(p, "w", encoding="utf-8"), separators=(",", ":"))
    print(json.dumps(out["counts"]), f"-> {p} ({os.path.getsize(p) // 1024} KB)")


if __name__ == "__main__":
    main()
