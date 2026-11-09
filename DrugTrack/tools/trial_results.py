"""Registry results and eligibility for a disease's trial universe, from AACT, into the store (one SQL pull per table).
Tables (shared across diseases, keyed by nct_id; re-pulled rows replace old ones):
  aact_outcome(nct, outcome_id, type, title, time_frame, population, endpoint)      endpoint = closed category below
  aact_measure(nct, outcome_id, group_title, param_type, value, value_num, units, lo, hi, dispersion_type)
  aact_analysis(nct, outcome_id, param_type, value, ci_lo, ci_hi, p_value, method, groups)
  aact_elig(nct, criteria, min_age, max_age, adult, child, older_adult)
  aact_trial(nct, primary_purpose, keywords)
Endpoint categories (title regex, first match): ORR, CR, MRD, PFS, EFS, OS, DOR, DFS/RFS, TTP, safety/PK (not efficacy).
Run:  python tools/trial_results.py --disease cll
"""
import argparse, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disease_config, store
from common import load_outputs

ENDPOINTS = [("ORR", r"overall response|objective response|\bORR\b|response rate"), ("CR", r"complete (?:remission|response)|\bCRR?\b|\bCRh\b|\bCRi\b"),
             ("MRD", r"minimal residual|measurable residual|\bMRD\b|\buMRD\b"), ("PFS", r"progression[- ]free|\bPFS\b"),
             ("EFS", r"event[- ]free|\bEFS\b"), ("OS", r"overall survival|\bOS\b"), ("DOR", r"duration of (?:response|remission)|\bDOR\b"),
             ("DFS", r"disease[- ]free|relapse[- ]free|\bRFS\b|\bDFS\b"), ("TTP", r"time to progression|\bTTP\b"),
             ("safety", r"adverse event|toxicit|dose[- ]limiting|\bDLT\b|maximum tolerated|\bMTD\b|safety"),
             ("PK", r"pharmacokinetic|\bAUC\b|\bCmax\b|concentration|half-life")]
ENDPOINTS = [(k, re.compile(v, re.I)) for k, v in ENDPOINTS]


def endpoint(title):
    return next((k for k, r in ENDPOINTS if r.search(title or "")), "other")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--disease", required=True)
    a = ap.parse_args()
    (U,) = load_outputs(a.disease, "mesh_universe.json.gz")
    ncts = sorted(U["trials"])
    from export_disease_tree import aact_client
    aact = aact_client()
    q = lambda sql: aact.run_query(sql, (ncts,), limit=10_000_000)
    disease_config.log(a.disease, "trial_results", f"AACT pull for {len(ncts)} trials")
    outc = q("SELECT nct_id, id, outcome_type, title, time_frame, population FROM outcomes WHERE nct_id = ANY(%s)")
    meas = q("SELECT m.nct_id, m.outcome_id, g.title AS group_title, m.param_type, m.param_value, m.param_value_num, m.units, "
             "m.dispersion_lower_limit, m.dispersion_upper_limit, m.dispersion_type, m.classification, m.category "
             "FROM outcome_measurements m LEFT JOIN result_groups g ON g.id = m.result_group_id WHERE m.nct_id = ANY(%s)")
    anal = q("SELECT nct_id, outcome_id, param_type, param_value, ci_lower_limit, ci_upper_limit, p_value, method, groups_description "
             "FROM outcome_analyses WHERE nct_id = ANY(%s)")
    elig = q("SELECT nct_id, criteria, minimum_age, maximum_age, adult, child, older_adult FROM eligibilities WHERE nct_id = ANY(%s)")
    des = q("SELECT nct_id, primary_purpose FROM designs WHERE nct_id = ANY(%s)")
    kw = q("SELECT nct_id, name FROM keywords WHERE nct_id = ANY(%s)")
    c = store.con()
    c.execute("CREATE TABLE IF NOT EXISTS aact_outcome(nct VARCHAR, outcome_id BIGINT, type VARCHAR, title VARCHAR, time_frame VARCHAR, population VARCHAR, endpoint VARCHAR)")
    c.execute("CREATE TABLE IF NOT EXISTS aact_measure(nct VARCHAR, outcome_id BIGINT, group_title VARCHAR, param_type VARCHAR, value VARCHAR, value_num DOUBLE, units VARCHAR, lo VARCHAR, hi VARCHAR, dispersion_type VARCHAR, classification VARCHAR, category VARCHAR)")
    c.execute("CREATE TABLE IF NOT EXISTS aact_analysis(nct VARCHAR, outcome_id BIGINT, param_type VARCHAR, value VARCHAR, ci_lo VARCHAR, ci_hi VARCHAR, p_value VARCHAR, method VARCHAR, groups VARCHAR)")
    c.execute("CREATE TABLE IF NOT EXISTS aact_elig(nct VARCHAR, criteria VARCHAR, min_age VARCHAR, max_age VARCHAR, adult BOOLEAN, child BOOLEAN, older_adult BOOLEAN)")
    c.execute("CREATE TABLE IF NOT EXISTS aact_trial(nct VARCHAR, primary_purpose VARCHAR, keywords VARCHAR)")
    for t in ("aact_outcome", "aact_measure", "aact_analysis", "aact_elig", "aact_trial"):
        c.execute(f"DELETE FROM {t} WHERE nct IN (SELECT unnest(?))", [ncts])
    s = lambda v: None if v is None else str(v)
    store._bulk_insert("aact_outcome", ["nct", "outcome_id", "type", "title", "time_frame", "population", "endpoint"],
                       [(r["nct_id"], r["id"], r["outcome_type"], r["title"], r["time_frame"], r["population"], endpoint(r["title"])) for r in outc])
    store._bulk_insert("aact_measure", ["nct", "outcome_id", "group_title", "param_type", "value", "value_num", "units", "lo", "hi", "dispersion_type", "classification", "category"],
                       [(r["nct_id"], r["outcome_id"], r["group_title"], r["param_type"], s(r["param_value"]),
                         float(r["param_value_num"]) if r["param_value_num"] is not None else None, r["units"], s(r["dispersion_lower_limit"]),
                         s(r["dispersion_upper_limit"]), r["dispersion_type"], r["classification"], r["category"]) for r in meas])
    store._bulk_insert("aact_analysis", ["nct", "outcome_id", "param_type", "value", "ci_lo", "ci_hi", "p_value", "method", "groups"],
                       [(r["nct_id"], r["outcome_id"], r["param_type"], s(r["param_value"]), s(r["ci_lower_limit"]), s(r["ci_upper_limit"]),
                         s(r["p_value"]), r["method"], r["groups_description"]) for r in anal])
    store._bulk_insert("aact_elig", ["nct", "criteria", "min_age", "max_age", "adult", "child", "older_adult"],
                       [(r["nct_id"], r["criteria"], r["minimum_age"], r["maximum_age"], r["adult"], r["child"], r["older_adult"]) for r in elig])
    kws = {}
    for r in kw:
        kws.setdefault(r["nct_id"], []).append(r["name"])
    store._bulk_insert("aact_trial", ["nct", "primary_purpose", "keywords"], [(r["nct_id"], r["primary_purpose"], json.dumps(kws.get(r["nct_id"], []))) for r in des])
    n_res = len({r["nct_id"] for r in meas})
    print(f"{len(ncts)} trials: {len(outc)} outcomes ({n_res} trials with posted results), {len(meas)} measurements, "
          f"{len(anal)} analyses, {len(elig)} eligibility rows")
    print(c.execute("SELECT endpoint, count(*) FROM aact_outcome WHERE nct IN (SELECT unnest(?)) GROUP BY 1 ORDER BY 2 DESC", [ncts]).fetchall())


if __name__ == "__main__":
    main()
