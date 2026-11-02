"""Development lineage of approved drugs: first-in-human -> phase 1/2 -> pivotal readout -> approval letter, per drug.
A separate layer; it reads the outputs of the earlier tools and changes none of them.
Inputs (<data_dir>): regimen_verification.json, indication_records.json, mesh_universe.json.gz, scope_labels.json (optional).
Per drug (records with a verified_* status, including approval-history records '<DRUG> @<date>'):
  events      one per verified record, by approval date: application, submission, setting, biomarker, regimen keys,
              approval letter (Drugs@FDA application_docs type Letter for that submission date).
  lead        per event, among the verified basis trials: highest phase (3 > 2/3 > 2 > 1/2 > 1), then randomized, then
              largest enrollment. The other basis trials are supporting.
  trials      every interventional trial of the drug in the disease universe (labeled drug by intervention MeSH id, or by
              intervention name -> the label's RxCUI through DrugCentral synonyms), tagged by role:
                basis_lead / basis_supporting      named by the approval (label or FDA package) and verified
                precursor                          started before the lead of the first approval it precedes, lower phase
                phase3_not_yet_basis               phase 3 that no verified approval names (ongoing, or a later approval
                                                   whose label is not read yet)
                post_approval                      started after the drug's last verified approval
              plus regimen relation from trial-level intervention MeSH: monotherapy / with label partners / other combination.
  first_in_human  earliest-start phase 1 (or early phase 1) trial of the drug in ALL of ClinicalTrials.gov (any condition),
              by intervention MeSH id; by intervention name when the drug has no MeSH id (flagged).
  readout     lead's primary completion and results-posted dates, and its primary PubMed article (NCT[si]; earliest article
              typed Clinical Trial, Phase III / Randomized Controlled Trial, else earliest Clinical Trial*).
Coverage: every NCI-listed drug (scope_labels.json nci) with its outcome at each stage, so nothing drops silently.
Output: <data_dir>/development_lineage.json     Run: python tools/development_lineage.py --disease cll
"""
import argparse, gzip, json, os, re, sys, time, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disease_config
from regimen_key import Crosswalk
from verify_regimens import index_for, lab_mesh_ids, names_reach, package_search

PHASE_RANK = {"PHASE3": 4, "PHASE2/PHASE3": 3.5, "PHASE2": 3, "PHASE1/PHASE2": 2, "PHASE1": 1, "EARLY_PHASE1": 0.5}
CTG = "https://clinicaltrials.gov/api/v2/studies"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
REF = os.path.join(disease_config.DATA, "references")


def prank(t):
    return PHASE_RANK.get("/".join(t.get("phases") or []), 0)


def get_json(url, fn, pause=0.35):
    if os.path.exists(fn):
        return json.load(open(fn, encoding="utf-8"))
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "clinical-tracker"}), timeout=90) as r:
                d = json.load(r)
            break
        except Exception:
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))
    os.makedirs(os.path.dirname(fn), exist_ok=True)
    json.dump(d, open(fn, "w", encoding="utf-8"))
    time.sleep(pause)
    return d


def first_in_human(mesh_ids, name):
    """Earliest-start phase 1 / early phase 1 trial of the drug anywhere on ClinicalTrials.gov."""
    if mesh_ids:
        expr = "(" + " OR ".join(f"AREA[InterventionMeshId]{m}" for m in sorted(mesh_ids)) + ") AND (AREA[Phase]PHASE1 OR AREA[Phase]EARLY_PHASE1)"
        params, route = {"filter.advanced": expr}, "intervention MeSH id"
    else:
        params, route = {"query.intr": name, "filter.advanced": "AREA[Phase]PHASE1 OR AREA[Phase]EARLY_PHASE1"}, "intervention name (drug has no MeSH id)"
    params.update({"fields": "NCTId,BriefTitle,StartDate,Phase,Condition,OverallStatus", "pageSize": 1000, "format": "json"})
    key = re.sub(r"[^A-Za-z0-9]", "_", json.dumps(params, sort_keys=True))[:150]
    d = get_json(CTG + "?" + urllib.parse.urlencode(params), os.path.join(REF, "ctgov_fih", key + ".json"))
    rows = []
    for s in d.get("studies", []):
        ps = s["protocolSection"]
        st = (ps.get("statusModule", {}).get("startDateStruct") or {}).get("date")
        if st:
            rows.append(dict(nct=ps["identificationModule"]["nctId"], start=st, title=ps["identificationModule"].get("briefTitle"),
                             conditions=ps.get("conditionsModule", {}).get("conditions", [])[:4]))
    rows.sort(key=lambda r: r["start"])
    return dict(route=route, n_phase1=len(rows), first=rows[0] if rows else None, next=rows[1:3])


def pubmed(nct):
    fn = os.path.join(REF, "pubmed", f"{nct}.json")
    if not os.path.exists(fn):
        es = get_json(f"{EUTILS}/esearch.fcgi?db=pubmed&retmode=json&retmax=200&term={nct}[si]", fn + ".search")
        ids = es.get("esearchresult", {}).get("idlist", [])
        sm = get_json(f"{EUTILS}/esummary.fcgi?db=pubmed&retmode=json&id={','.join(ids)}", fn + ".summary") if ids else {"result": {"uids": []}}
        json.dump(dict(esearch=es, esummary=sm), open(fn, "w", encoding="utf-8"))
    d = json.load(open(fn, encoding="utf-8"))
    res = d.get("esummary", {}).get("result", {})
    arts = sorted(([dict(pmid=u, pubdate=res[u].get("sortpubdate", res[u].get("pubdate")), journal=res[u].get("source"),
                         pubtype=res[u].get("pubtype", []), title=res[u].get("title")) for u in res.get("uids", [])]),
                  key=lambda a: a["pubdate"] or "")
    return next((a for a in arts if {"Clinical Trial, Phase III", "Randomized Controlled Trial"} & set(a["pubtype"])), None) \
        or next((a for a in arts if any(t.startswith("Clinical Trial") for t in a["pubtype"])), None)


def letter(app, date):
    """Approval letter URL of one Drugs@FDA submission (bulk tables)."""
    import store
    if not app or not date:
        return None
    d = date.replace("-", "")
    return next((x["url"] for s in store.drugsfda_submissions(app) if s.get("submission_status_date") == d
                 for x in s.get("application_docs", []) if x.get("type") == "Letter"), None)


def main():
    if hasattr(sys.stdout, "reconfigure"):          # record text holds non-cp1252 characters (e.g. U+2265) on Windows consoles
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--disease", required=True)
    a = ap.parse_args()
    ddir = disease_config.data_dir(a.disease)
    U = json.load(gzip.open(os.path.join(ddir, "mesh_universe.json.gz"), "rt", encoding="utf-8"))
    T = U["trials"]
    V = {r["record_id"]: r for r in json.load(open(os.path.join(ddir, "regimen_verification.json"), encoding="utf-8"))["results"]}
    R = {r["record_id"]: r for r in json.load(open(os.path.join(ddir, "indication_records.json"), encoding="utf-8"))["records"]}
    xw = Crosswalk()
    drugs = {}
    for rid, v in V.items():
        if v["status"].startswith("verified"):
            drugs.setdefault(R[rid]["drug"].split(" @")[0].split(" [")[0], []).append(rid)
    out = {}
    for drug, rids in sorted(drugs.items()):
        disease_config.log(a.disease, "lineage", f"{drug}: {len(rids)} verified records")
        recs = sorted((R[r] for r in rids), key=lambda r: r["approval"]["fda"].get("date") or "9999")
        rec0 = recs[0]
        lab_mesh, _ = lab_mesh_ids(rec0, U, xw)
        lab_rx = {r for l in rec0["labeled_ingredients"] for r in l["rxcui"]}
        partner_mesh = {m for rec in recs for d in rec["phases"].values() for alt in d["alternatives"] for p in alt for m in p.get("mesh", [])}
        basis_all = {n for r in rids for n in V[r]["basis_ncts"]}
        hits = index_for(U, xw).ncts_for(rxcuis=lab_rx, mesh=lab_mesh) | basis_all     # hash index, built once per universe
        mine = {n for n in hits if n in T and T[n]["study_type"] == "INTERVENTIONAL"}
        events, basis_role, seen, led, prev_b = [], {}, set(), set(), set()
        for rec in recs:
            v = V[rec["record_id"]]
            date = rec["approval"]["fda"].get("date") or "9999"
            b = [n for n in v["basis_ncts"] if n in T]
            new = [n for n in b if n not in prev_b]  # new versus the previous approval's basis (later labels reprint old trials)
            # lead = the basis trial this event's own FDA documents (letter / review / label of that submission) mention most
            # documents are searched for every phase 2+ trial of the drug started before the approval, not only the label's basis,
            # so a pivotal trial named only in the review (or the letter) is not missed
            cands = set(b) | {n for n in mine if prank(T[n]) >= 2 and (T[n]["start"] or "9999") < date}
            pkg = package_search(rec, cands, T) if cands else {"hits": {}}
            # trials outside the label basis count only when the documents name them by NCT or org study id (acronyms and
            # secondary ids collide with common tokens), and only a phase 3 one may lead
            mentions = {n: h["mentions"] for n, h in pkg["hits"].items() if h["role"] == "efficacy_basis"
                        and (n in b or any(i.startswith(("nct:", "org_study_id:")) for i in h["ids"]))}
            doc_named = sorted(n for n in mentions if n not in b)
            # label section 14 trials stay authoritative; a document-only phase 3 overrides them only when no label trial is
            # phase 3 and the documents mention it at least twice as often (BRUIN CLL-321 for pirtobrutinib, not Study 125
            # for idelalisib, whose review calls the label trial 'Study 116')
            best = max([m for n, m in mentions.items() if n in b] or [0])
            label_p3 = any(prank(T[n]) >= 4 for n in b)
            eligible = {n: m for n, m in mentions.items()
                        if n in b or (prank(T[n]) >= 4 and not label_p3 and m >= 2 * max(best, 1))}
            if eligible:
                top = max(eligible.values())
                pool = [n for n in eligible if eligible[n] == top]
                read = None
            else:
                read = [n for n in new if (T[n].get("primary_completion") or "9999") <= date]
                pool = read or new
            pool.sort(key=lambda n: (n not in led, prank(T[n]), T[n].get("allocation") == "RANDOMIZED", T[n].get("enrollment") or 0),
                      reverse=True)                     # ties go to the trial new at this event (RESONATE-2 over RESONATE)
            lead = pool[0] if pool else None
            label_update = bool(events) and not new and max(list(eligible.values()) or [0]) <= 2
            if label_update:                        # no new trial and documents barely name one: a label update
                lead = events[-1]["lead"]
            if events and events[-1]["date"] == rec["approval"]["fda"].get("date"):
                continue                            # same approval event under a second label (Imbruvica capsules / tablets)
            seen |= set(b)
            prev_b = set(b)
            led |= {lead} if lead else set()        # ties go to a trial that has not led an earlier approval
            b = ([lead] if lead else []) + [n for n in b if n != lead]
            for n in b:
                if basis_role.get(n) != "basis_lead":
                    basis_role[n] = "basis_lead" if n == lead else "basis_supporting"
            f = rec["approval"]["fda"]
            events.append(dict(record_id=rec["record_id"], kind="label update (no new trial)" if label_update or not lead else "new pivotal evidence",
                               lead_rule=("most mentioned in this submission's FDA documents" if eligible else
                                          "newest basis trial read out before approval" if read else
                                          "newest basis trial (no FDA documents, no readout date before approval)" if lead else None),
                               document_mentions=mentions, named_in_documents_not_label=doc_named,
                               date=f.get("date"), application=f.get("application"),
                               submission=f.get("submission"), link_level=v["status"], setting=rec["indication"]["setting"],
                               biomarker=rec["indication"]["biomarker"], regimen_keys=rec["regimen_keys"], lead=lead,
                               supporting=b[1:], letter=letter(f.get("application"), f.get("date"))))
        mine |= set(basis_role)
        first_date = events[0]["date"] or "9999"
        last_date = events[-1]["date"] or "9999"
        leads = [e["lead"] for e in events if e["lead"]]
        trials = []
        for n in sorted(mine, key=lambda n: T[n]["start"] or "9999"):
            t = T[n]
            others = set(t["int_mesh"]) - lab_mesh
            rel = "monotherapy" if not others else ("with label partner" if others & partner_mesh else "other combination")
            role = basis_role.get(n)
            if not role:
                nxt_lead = next((T[l] for e in events for l in [e["lead"]] if l and (t["start"] or "9999") < (T[l]["start"] or "")), None)
                if (t["start"] or "9999") > last_date:
                    role = "post_approval"
                elif nxt_lead and prank(t) < prank(nxt_lead):
                    role = "precursor"
                elif prank(t) >= 4:
                    role = "phase3_not_yet_basis"
                else:
                    role = "parallel_or_other"
            trials.append(dict(nct=n, phase="/".join(t.get("phases") or []), start=t["start"], primary_completion=t.get("primary_completion"),
                               results_posted=t.get("results_first_posted"), status=t["status"], enrollment=t["enrollment"],
                               randomized=t.get("allocation") == "RANDOMIZED", acronym=t.get("acronym"), role=role,
                               regimen=rel, title=(t.get("brief_title") or "")[:120]))
        fih = first_in_human({m for m in lab_mesh if m != (rec0.get("fixed_combination") or {}).get("mesh")}, drug.lower())
        readouts = {l: dict(primary_completion=T[l].get("primary_completion"), results_posted=T[l].get("results_first_posted"),
                            primary_article=pubmed(l)) for l in dict.fromkeys(leads)}
        out[drug] = dict(first_in_human=fih, events=events, readouts=readouts, trials=trials,
                         counts={r: sum(1 for x in trials if x["role"] == r) for r in sorted({x["role"] for x in trials})})
        disease_config.log(a.disease, "lineage", f"  FIH {fih['first'] and fih['first']['nct']} {fih['first'] and fih['first']['start']} | "
                                                 f"events {[(e['date'], e['lead']) for e in events]} | roles {out[drug]['counts']}")
    # coverage of the NCI list
    cov = {}
    sl_path = os.path.join(ddir, "scope_labels.json")
    if os.path.exists(sl_path):
        sl = json.load(open(sl_path, encoding="utf-8"))
        kept_by_set = {v["setid"]: k for k, v in sl["labels"].items()}
        failed_by_set = {f["setid"]: f["reason"] for f in sl.get("gate_failed", [])}
        for n, m in sl.get("nci", {}).items():
            if not m["rxcui"] and " and " in n:     # 'rituximab and hyaluronidase human': a combination product, look up its parts
                parts = [p.strip() for p in n.split(" and ")]
                m = dict(m, rxcui=sorted({r for p in parts for r in xw.rx_name(p)}),
                         setids=sorted({s for k2, m2 in sl["nci"].items() if k2 in parts for s in m2["setids"]}))
            ks = [kept_by_set[s] for s in m["setids"] if s in kept_by_set]
            rec_ids = [rid for rid, r in R.items() if r["drug"].split(" @")[0] in ks]
            st = sorted({V[r]["status"] for r in rec_ids if r in V})
            if not m["rxcui"]:
                why = "no RxNorm ingredient for the NCI name"
            elif not m["setids"]:
                why = "no DailyMed NDA/BLA (or ANDA fallback) label for the ingredient"
            elif not ks:
                why = "label(s) found, none name the disease in section 1: " + "; ".join(sorted({failed_by_set.get(s, "?") for s in m["setids"]}))[:120]
            elif any(x.startswith("verified") for x in st):
                why = "verified"
            else:
                why = "records built, not verified: " + ", ".join(st)
            cov[n] = dict(rxcui=m["rxcui"], labels=ks, records=len(rec_ids), statuses=st, outcome=why)
    json.dump(dict(disease=a.disease, drugs=out, nci_coverage=cov), open(os.path.join(ddir, "development_lineage.json"), "w", encoding="utf-8"), indent=1)
    print(f"lineage for {len(out)} approved drugs -> {os.path.join(ddir, 'development_lineage.json')}")
    for d, x in out.items():
        f = x["first_in_human"]["first"] or {}
        print(f"\n{d}: FIH {f.get('nct')} ({f.get('start')}) [{x['first_in_human']['route']}]  roles {x['counts']}")
        for e in x["events"]:
            ro = x["readouts"].get(e["lead"], {})
            pa = ro.get("primary_article") or {}
            print(f"   {e['date']} {e['application']} {e['submission']}: lead {e['lead']} supp {e['supporting'][:3]} | readout "
                  f"{ro.get('primary_completion')} PMID {pa.get('pmid')} | letter {'yes' if e['letter'] else 'no'}")
        for t in x["trials"]:
            if t["role"] in ("precursor", "phase3_not_yet_basis"):
                print(f"      {t['role']:22s} {t['nct']} {t['phase']:14s} {t['start']} {t['regimen']:18s} {t['title'][:60]}")
    if cov:
        print("\nNCI coverage:")
        for n, c in cov.items():
            print(f"   {n:36s} labels={len(c['labels'])} records={c['records']:2d}  {c['outcome']}")


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
    INP = [os.path.join(D, f) for f in ("regimen_verification.json", "indication_records.json", "mesh_universe.json.gz", "scope_labels.json")]
    OUT = [_os.path.join(D, x) for x in ["development_lineage.json"]]
    CODE = ["development_lineage.py", "verify_regimens.py", "regimen_key.py", "common.py", "store.py", "disease_config.py"]
    if a.trace or "show" in _sys.argv or "pull" in _sys.argv or "--help" in _sys.argv:
        main()
    elif not common.stage_current(a.disease, "lineage", INP, OUT, CODE, a.force):
        main()
        common.stage_done(a.disease, "lineage", INP, OUT, CODE)
