"""Step 1 of the deterministic path: the disease's trial universe by MeSH ID, with no disease-name text matching.
  1. Disease MeSH set = the MESH: cross-references of the MONDO scope disease and every MONDO descendant
     (Open Targets disease.parquet dbXRefs). Descendants with no MeSH cross-reference are listed, not guessed.
  2. ClinicalTrials.gov v2 API, filter.advanced = AREA[ConditionMeshId]<id> OR AREA[ConditionAncestorId]<id> for every id
     in the set. These are the MeSH codes ClinicalTrials.gov derives from each record's conditions and keywords (the same
     derivation AACT copies into browse_conditions / browse_interventions). Used because the AACT login is failing;
     the AACT tables give the same codes and can replace this pull with no logic change.
  3. Per trial keep: condition MeSH ids (direct + ancestor), intervention MeSH ids, phase, status, start date, enrollment,
     design (allocation, masking), org / secondary study ids, acronym, arms with their intervention names.
  4. Compare with the text-matched universe in data/aact_cache.json.gz (ID match vs text match): both / text_only / mesh_only.
Raw API pages are cached gzipped under data/references/ctgov_mesh/<scope>/. Output: data/mesh_universe.json.gz
Run:  python tools/mesh_universe.py [--scope-id MONDO_0018874] [--refresh]
"""
import argparse, gzip, json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone
import pyarrow.parquet as pq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
BRIDGE = os.environ.get("AACT_BRIDGE_DIR", r"C:\Users\jwrup\Clinical Tracker\AACT_bridge")
DISEASE_PARQUET = os.path.join(BRIDGE, "inputs", "opentargets", "disease.parquet")
API = "https://clinicaltrials.gov/api/v2/studies"
FIELDS = ",".join([
    "protocolSection.identificationModule", "protocolSection.statusModule", "protocolSection.designModule",
    "protocolSection.armsInterventionsModule", "protocolSection.conditionsModule",
    "derivedSection.conditionBrowseModule", "derivedSection.interventionBrowseModule"])
OUT = os.path.join(DATA, "mesh_universe.json.gz")


def disease_mesh(scope_id):
    rows = pq.read_table(DISEASE_PARQUET, columns=["id", "name", "descendants", "dbXRefs"]).to_pylist()
    by_id = {r["id"]: r for r in rows}
    ids = [scope_id] + sorted(by_id[scope_id]["descendants"] or [])
    mesh, no_mesh = {}, []
    for i in ids:
        d = by_id.get(i)
        if not d:
            continue
        m = [x.split(":", 1)[1] for x in (d["dbXRefs"] or []) if x.startswith("MESH:")]
        if not m:
            no_mesh.append(dict(id=i, name=d["name"]))
        for x in m:
            mesh.setdefault(x, []).append(dict(id=i, name=d["name"]))
    return mesh, no_mesh, len(ids)


def get_json(params):
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "clinical-tracker"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except Exception:
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))


def pull(scope_id, mesh_ids, refresh):
    cache = os.path.join(DATA, "references", "ctgov_mesh", scope_id)
    os.makedirs(cache, exist_ok=True)
    expr = " OR ".join(f"AREA[ConditionMeshId]{m} OR AREA[ConditionAncestorId]{m}" for m in sorted(mesh_ids))
    studies, token, page = [], None, 0
    while True:
        path = os.path.join(cache, f"page_{page:03d}.json.gz")
        if os.path.exists(path) and not refresh:
            d = json.load(gzip.open(path, "rt", encoding="utf-8"))
        else:
            p = {"filter.advanced": expr, "fields": FIELDS, "pageSize": 1000, "countTotal": "true", "format": "json"}
            if token:
                p["pageToken"] = token
            d = get_json(p)
            with gzip.open(path, "wt", encoding="utf-8") as f:
                json.dump(d, f)
        studies += d.get("studies", [])
        token = d.get("nextPageToken")
        page += 1
        if not token:
            break
    return studies, expr


def slim(s):
    ps, dv = s.get("protocolSection", {}), s.get("derivedSection", {})
    idm, st, de = ps.get("identificationModule", {}), ps.get("statusModule", {}), ps.get("designModule", {})
    ai, cb, ib = ps.get("armsInterventionsModule", {}), dv.get("conditionBrowseModule", {}), dv.get("interventionBrowseModule", {})
    di = de.get("designInfo", {})
    return dict(
        nct=idm.get("nctId"), acronym=idm.get("acronym"),
        brief_title=idm.get("briefTitle"), official_title=idm.get("officialTitle"),
        org_id=(idm.get("orgStudyIdInfo") or {}).get("id"),
        secondary_ids=[x.get("id") for x in idm.get("secondaryIdInfos", []) if x.get("id")],
        study_type=de.get("studyType"), phases=de.get("phases", []),
        enrollment=(de.get("enrollmentInfo") or {}).get("count"),
        enrollment_type=(de.get("enrollmentInfo") or {}).get("type"),
        allocation=di.get("allocation"), masking=(di.get("maskingInfo") or {}).get("masking"),
        status=st.get("overallStatus"), start=(st.get("startDateStruct") or {}).get("date"),
        primary_completion=(st.get("primaryCompletionDateStruct") or {}).get("date"),
        completion=(st.get("completionDateStruct") or {}).get("date"),
        results_first_posted=(st.get("resultsFirstPostDateStruct") or {}).get("date"),
        cond_mesh=[m["id"] for m in cb.get("meshes", [])], cond_ancestors=[m["id"] for m in cb.get("ancestors", [])],
        int_mesh=[m["id"] for m in ib.get("meshes", [])], int_mesh_terms={m["id"]: m["term"] for m in ib.get("meshes", [])},
        conditions=ps.get("conditionsModule", {}).get("conditions", []),
        arms=[dict(label=a.get("label"), type=a.get("type"), interventions=a.get("interventionNames", []))
              for a in ai.get("armGroups", [])],
        interventions=[dict(type=i.get("type"), name=i.get("name"), other_names=i.get("otherNames", []),
                            arms=i.get("armGroupLabels", [])) for i in ai.get("interventions", [])])


def aact_universe(mesh_ids):
    """Same universe from AACT in a few SQL queries (no API paging). AACT stores MeSH headings, not ids: headings are mapped
    back to MeSH ids through the bulk MeSH table (exact heading = mesh_record.name), so the output schema matches slim()."""
    import store
    from export_disease_tree import aact_client
    a = aact_client()
    q = lambda sql, p: a.run_query(sql, p, limit=5_000_000)
    names = dict(store.con().execute("SELECT ui, name FROM mesh_record").fetchall())
    ui_of = {v.lower(): k for k, v in names.items()}
    heads = [names[m] for m in mesh_ids if m in names]
    bc = q("SELECT nct_id, mesh_term, mesh_type FROM browse_conditions WHERE nct_id IN "
           "(SELECT nct_id FROM browse_conditions WHERE mesh_term = ANY(%s))", (heads,))
    ncts = sorted({r["nct_id"] for r in bc})
    st = q("SELECT nct_id, acronym, brief_title, official_title, study_type, phase, enrollment, enrollment_type, overall_status, "
           "start_date, primary_completion_date, completion_date, results_first_posted_date FROM studies WHERE nct_id = ANY(%s)", (ncts,))
    de = {r["nct_id"]: r for r in q("SELECT nct_id, allocation, masking FROM designs WHERE nct_id = ANY(%s)", (ncts,))}
    ids = q("SELECT nct_id, id_source, id_value FROM id_information WHERE nct_id = ANY(%s)", (ncts,))
    bi = q("SELECT nct_id, mesh_term, mesh_type FROM browse_interventions WHERE nct_id = ANY(%s)", (ncts,))
    iv = q("SELECT id, nct_id, intervention_type, name FROM interventions WHERE nct_id = ANY(%s)", (ncts,))
    on = q("SELECT intervention_id, name FROM intervention_other_names WHERE nct_id = ANY(%s)", (ncts,))
    dg = q("SELECT id, nct_id, group_type, title FROM design_groups WHERE nct_id = ANY(%s)", (ncts,))
    dgi = q("SELECT design_group_id, intervention_id FROM design_group_interventions WHERE nct_id = ANY(%s)", (ncts,))
    cond = q("SELECT nct_id, name FROM conditions WHERE nct_id = ANY(%s)", (ncts,))
    from collections import defaultdict
    g = lambda rows, k: (lambda d: [d[r[k]].append(r) for r in rows] and d or d)(defaultdict(list))
    bc_, bi_, iv_, dg_, ids_, cond_ = g(bc, "nct_id"), g(bi, "nct_id"), g(iv, "nct_id"), g(dg, "nct_id"), g(ids, "nct_id"), g(cond, "nct_id")
    on_ = g(on, "intervention_id")
    grp_iv = defaultdict(list)
    for r in dgi:
        grp_iv[r["design_group_id"]].append(r["intervention_id"])
    iv_by_id = {r["id"]: r for r in iv}
    grp_by_id = {r["id"]: r for r in dg}
    iv_arms = defaultdict(list)
    for gid, ivs in grp_iv.items():
        for i in ivs:
            if gid in grp_by_id:
                iv_arms[i].append(grp_by_id[gid]["title"])
    up = lambda s: (s or "").upper().replace(" ", "_") if s else None
    fmt = lambda d: d.isoformat()[:10] if hasattr(d, "isoformat") else d
    out = {}
    for r in st:
        n = r["nct_id"]
        mesh_c = [ui_of.get(x["mesh_term"].lower()) for x in bc_[n] if x["mesh_type"] == "mesh-list"]
        anc_c = [ui_of.get(x["mesh_term"].lower()) for x in bc_[n] if x["mesh_type"] == "mesh-ancestor"]
        mesh_i = [ui_of.get(x["mesh_term"].lower()) for x in bi_[n] if x["mesh_type"] == "mesh-list"]
        terms_i = {ui_of.get(x["mesh_term"].lower()): x["mesh_term"] for x in bi_[n] if x["mesh_type"] == "mesh-list"}
        out[n] = dict(
            nct=n, acronym=r["acronym"], brief_title=r["brief_title"], official_title=r["official_title"],
            org_id=next((x["id_value"] for x in ids_[n] if x["id_source"] == "org_study_id"), None),
            secondary_ids=[x["id_value"] for x in ids_[n] if x["id_source"] == "secondary_id"],
            study_type=up(r["study_type"]), phases=[p for p in (r["phase"] or "").split("/") if p and p != "NA"],
            enrollment=r["enrollment"], enrollment_type=up(r["enrollment_type"]),
            allocation=up((de.get(n) or {}).get("allocation")), masking=up((de.get(n) or {}).get("masking")),
            status=up(r["overall_status"]), start=fmt(r["start_date"]), primary_completion=fmt(r["primary_completion_date"]),
            completion=fmt(r["completion_date"]), results_first_posted=fmt(r["results_first_posted_date"]),
            cond_mesh=[m for m in mesh_c if m], cond_ancestors=[m for m in anc_c if m],
            int_mesh=[m for m in mesh_i if m], int_mesh_terms={k: v for k, v in terms_i.items() if k},
            conditions=[x["name"] for x in cond_[n]],
            arms=[dict(label=x["title"], type=up(x["group_type"]),
                       interventions=[f"{iv_by_id[i]['intervention_type'].title() if iv_by_id[i]['intervention_type'] else ''}: {iv_by_id[i]['name']}"
                                      for i in grp_iv.get(x["id"], []) if i in iv_by_id]) for x in dg_[n]],
            interventions=[dict(type=up(x["intervention_type"]), name=x["name"], other_names=[o["name"] for o in on_[x["id"]]],
                                arms=iv_arms.get(x["id"], [])) for x in iv_[n]],
            unmapped_headings=[x["mesh_term"] for x in bc_[n] + bi_[n] if not ui_of.get(x["mesh_term"].lower())])
    return out


def aact_crosscheck(headings, api_ncts):
    """Independent copy of the same MeSH derivation: AACT browse_conditions (mesh-list + mesh-ancestor rows) joined on the
    exact MeSH heading (AACT stores headings, not descriptor UIs). Differences are snapshot-date drift between the two."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from export_disease_tree import aact_client
        a = aact_client()
        rows = a.run_query("SELECT DISTINCT nct_id FROM browse_conditions WHERE mesh_term = ANY(%s)", (headings,), limit=200000)
    except Exception as e:
        return dict(status=f"unavailable: {type(e).__name__}")
    s = {r["nct_id"] for r in rows}
    return dict(status="ok", headings=headings, aact=len(s), both=len(s & api_ncts),
                aact_only=sorted(s - api_ncts), api_only=sorted(api_ncts - s))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--disease", default="aml", help="config/diseases/<name>.json supplies scope_id")
    ap.add_argument("--scope-id", help="override the config scope_id")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--source", choices=["api", "aact"], default="api")
    a = ap.parse_args()
    if not a.scope_id:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import disease_config
        a.scope_id = disease_config.load(a.disease)["scope_id"]
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import disease_config as _dc
    out_path = os.path.join(_dc.data_dir(a.disease), "mesh_universe.json.gz")
    mesh, no_mesh, n_nodes = disease_mesh(a.scope_id)
    if a.source == "aact":                          # fast refresh: AACT SQL; MeSH ids re-derived from headings (unmapped flagged)
        studies, expr = [], "AACT browse_conditions mesh_term = disease headings"
        trials = aact_universe(sorted(mesh))
        for t in trials.values():
            t["phases"] = t["phases"] or (["NA"] if t["study_type"] == "INTERVENTIONAL" else [])
    else:                                          # default: ClinicalTrials.gov API, MeSH ids native
        studies, expr = pull(a.scope_id, mesh, a.refresh)
        trials = {t["nct"]: t for t in map(slim, studies)}
    for t in trials.values():
        hit = sorted(set(t["cond_mesh"]) & set(mesh)), sorted(set(t["cond_ancestors"]) & set(mesh))
        t["scope_hit"] = dict(direct=hit[0], ancestor=hit[1])
    heads = {m["id"]: m["term"] for s in studies for m in s.get("derivedSection", {}).get("conditionBrowseModule", {})
             .get("meshes", []) + s.get("derivedSection", {}).get("conditionBrowseModule", {}).get("ancestors", [])
             if m["id"] in mesh}
    if not heads:                                  # AACT route: heading names from the bulk MeSH table
        import store
        heads = {m: n for m, n in store.con().execute("SELECT ui, name FROM mesh_record WHERE ui IN (SELECT unnest(?))",
                                                      [sorted(mesh)]).fetchall()}
    aact_check = aact_crosscheck(sorted(heads.values()), set(trials))
    cmp_ = {}
    cache = os.path.join(DATA, "aact_cache.json.gz")
    if os.path.exists(cache):
        c = json.load(gzip.open(cache, "rt", encoding="utf-8"))
        if c.get("scope_id") == a.scope_id:
            text = set(c["trials"])
            ids = set(trials)
            cmp_ = dict(text_universe=len(text), mesh_universe=len(ids), both=len(text & ids),
                        text_only=sorted(text - ids), mesh_only=sorted(ids - text), text_pulled_at=c.get("pulled_at"))
    out = dict(generated=datetime.now(timezone.utc).isoformat(timespec="seconds"), scope_id=a.scope_id,
               mondo_nodes=n_nodes, disease_mesh=mesh, mondo_without_mesh=no_mesh, filter=expr,
               mesh_headings=heads, source=API, n_trials=len(trials), aact_crosscheck=aact_check, compare_text=cmp_,
               trials=trials)
    with gzip.open(out_path, "wt", encoding="utf-8") as f:
        json.dump(out, f)
    print(f"scope {a.scope_id}: {n_nodes} MONDO nodes, {len(mesh)} MeSH ids, {len(no_mesh)} nodes without MeSH")
    print(f"MeSH universe: {len(trials)} trials")
    if aact_check.get("status") == "ok":
        print(f"AACT browse_conditions cross-check: {aact_check['aact']} trials, both {aact_check['both']}, "
              f"aact_only {len(aact_check['aact_only'])}, api_only {len(aact_check['api_only'])}")
    else:
        print(f"AACT cross-check: {aact_check.get('status')}")
    if cmp_:
        print(f"vs text universe {cmp_['text_universe']}: both {cmp_['both']}, text_only {len(cmp_['text_only'])}, "
              f"mesh_only {len(cmp_['mesh_only'])}")
    print(f"wrote {out_path}")


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
    INP = [os.path.join(B, "inputs", "opentargets", "disease.parquet")]
    OUT = [_os.path.join(D, x) for x in ["mesh_universe.json.gz"]]
    CODE = ["mesh_universe.py", "disease_config.py"]
    if a.trace or "show" in _sys.argv or "pull" in _sys.argv or "--help" in _sys.argv:
        main()
    elif not common.stage_current(a.disease, "mesh_universe", INP, OUT, CODE, a.force):
        main()
        common.stage_done(a.disease, "mesh_universe", INP, OUT, CODE)
