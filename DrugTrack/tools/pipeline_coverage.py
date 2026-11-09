"""Coverage audit: for named investigational agents, is every ClinicalTrials.gov trial of the agent accounted for?
Per agent: ClinicalTrials.gov v2 query.intr=<name OR synonyms> (synonyms = names co-listed on the same intervention record
in the universe). Per trial one deterministic disposition:
  finder_investigational / finder_recent_readout   trial is listed under a regimen in finder.json
  universe_not_finder:<reason>                     in mesh_universe.json.gz but not in the finder (reason from the finder
                                                   rules: study type, primary purpose, status)
  rejected_text:<rule>                             pulled by the condition-text channel and rejected (rule name)
  out_of_scope_condition                           conditions neither carry a scope MeSH id nor name the disease
Agents default to data/sponsor_links.json pipeline keys; --all-unresolved adds every unresolved ('N:') finder ingredient.
Curated aliases (config/agent_aliases.json, agents propose them): {"AGENT": [{"alias", "evidence_nct", "proposed_by"}]}.
An alias is used only when its evidence_nct record names the alias on an intervention AND names the agent (or a sponsor
page found_term for it) in an intervention, title or official title. Unverified aliases are reported, never queried.
Output: <data_dir>/pipeline_coverage.json     Run: python tools/pipeline_coverage.py --disease aml_gen [AGENT ...]
"""
import argparse, gzip, json, os, sys, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disease_config
from mesh_universe import get_json, slim, FIELDS, norm_name, API

LIVE = {"RECRUITING", "NOT_YET_RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--disease", default="aml_gen")
    ap.add_argument("agents", nargs="*")
    ap.add_argument("--all-unresolved", action="store_true", help="also audit every unresolved ('N:') finder ingredient")
    a = ap.parse_args()
    ddir = disease_config.data_dir(a.disease)
    U = json.load(gzip.open(os.path.join(ddir, "mesh_universe.json.gz"), "rt", encoding="utf-8"))
    F = json.load(open(os.path.join(ddir, "finder.json"), encoding="utf-8"))
    where = {}
    for sec in ("investigational", "recent_read_out"):
        for g in F[sec]:
            for t in g["trials"]:
                where.setdefault(t["nct"], []).append(f"{sec}:{g['regimen_key']}")
    rejected = {n: rule for rule, ns in (U.get("text_gap") or {}).get("rejected_ncts", {}).items() for n in ns}
    syn = {}
    for t in U["trials"].values():
        for i in t["interventions"]:
            names = {norm_name(x) for x in [i["name"]] + (i.get("other_names") or []) if x}
            for k in names:
                syn.setdefault(k, set()).update(names)
    sponsor = {}
    sp = os.path.join(disease_config.DATA, "sponsor_links.json")
    if os.path.exists(sp):
        sponsor = json.load(open(sp, encoding="utf-8")).get("pipeline", {})
    alias_path = os.path.join(disease_config.ROOT, "config", "agent_aliases.json")
    aliases = json.load(open(alias_path, encoding="utf-8")) if os.path.exists(alias_path) else {}
    alias_check = {}
    for ag, props in aliases.items():
        if ag.startswith("_"):
            continue
        known = {norm_name(ag)} | {norm_name(k) for k, v in (sponsor.get(ag, {}).get("found_terms") or {}).items() if v}
        for pr in props:
            d = get_json({"query.term": f"AREA[NCTId]{pr['evidence_nct']}", "fields": FIELDS, "format": "json"})
            st = (d.get("studies") or [None])[0]
            ok, why = False, "evidence_nct not found"
            if st:
                t = slim(st)
                iv = {norm_name(x) for i in t["interventions"] for x in [i["name"]] + (i.get("other_names") or []) if x}
                text = " ".join([norm_name(t["brief_title"]), norm_name(t["official_title"])] + sorted(iv))
                has_alias = norm_name(pr["alias"]) in iv
                has_agent = any(k in text for k in known)
                ok = has_alias and has_agent
                why = "verified: alias on intervention, agent named in record" if ok else                     f"rejected: alias_on_intervention={has_alias} agent_named={has_agent}"
            alias_check.setdefault(ag, []).append(dict(pr, verified=ok, check=why))
            if ok:
                syn.setdefault(norm_name(ag), {norm_name(ag)}).add(norm_name(pr["alias"]))
    agents = a.agents
    if not agents:
        agents = list(sponsor)
        if a.all_unresolved:
            agents += sorted({i for g in F["investigational"] for i in g["ingredients"] if f"N:{i}" in g["regimen_key"]})
    out = {}
    for ag in dict.fromkeys(agents):
        # synonyms of 3 characters or fewer ('gps') are ambiguous as free-text intervention queries
        names = sorted(n for n in syn.get(norm_name(ag), {norm_name(ag)}) if len(n) > 3 or n == norm_name(ag))
        q = " OR ".join(f'"{n}"' for n in names[:12])
        studies, token = [], None
        while True:
            p = {"query.intr": q, "fields": FIELDS, "pageSize": 1000, "format": "json"}
            if token:
                p["pageToken"] = token
            try:
                d = get_json(p)
            except Exception as e:
                d = {"error": f"{type(e).__name__}: {e}"}
            if "error" in d:
                print(f"{ag}: query failed ({d['error']}); listed as error")
                break
            studies += d.get("studies", [])
            token = d.get("nextPageToken")
            if not token:
                break
        rows = []
        for s in studies:
            t = slim(s)
            n = t["nct"]
            if n in where:
                disp = "finder"
            elif n in U["trials"]:
                u = U["trials"][n]
                why = ("not_interventional" if u["study_type"] != "INTERVENTIONAL" else
                       "not_live_no_recent_results" if u["status"] not in LIVE else "purpose_or_arm_filter")
                disp = f"universe_not_finder:{why}"
            elif n in rejected:
                disp = f"rejected_text:{rejected[n]}"
            else:
                disp = "out_of_scope_condition"
            rows.append(dict(nct=n, status=t["status"], phases=t["phases"], study_type=t["study_type"],
                             conditions=t["conditions"][:4], disposition=disp, finder=where.get(n, [])))
        out[ag] = dict(query=q, n=len(rows), rows=sorted(rows, key=lambda r: r["disposition"]))
        cnt = {}
        for r in rows:
            cnt[r["disposition"]] = cnt.get(r["disposition"], 0) + 1
        live_missing = [r["nct"] for r in rows if r["status"] in LIVE and r["study_type"] == "INTERVENTIONAL"
                        and r["disposition"].startswith("universe_not_finder")]
        print(f"{ag}: {len(rows)} trials {cnt}" + (f"  LIVE IN-SCOPE NOT IN FINDER: {live_missing}" if live_missing else ""))
    json.dump(dict(source=API, aliases=alias_check, agents=out), open(os.path.join(ddir, "pipeline_coverage.json"), "w", encoding="utf-8"), indent=1)
    print("wrote", os.path.join(ddir, "pipeline_coverage.json"))


if __name__ == "__main__":
    main()
