"""Efficacy tables of each approval-time label, tagged to the trial and the regimen they report.
Per verified record: label section 14 (LOINC 34092-7) subsections for the disease (title or opening text names it) are read
in document order (dailymed_label.flow). Study context = the last trial named in the text before a table: an NCT id, or a
trial name that the registry holds for one of the record's basis trials (acronym or title token, common.UniverseIndex);
a subsection with a single basis trial uses that trial. Each table (dailymed_label.parse_table) inherits the context.
Table -> regimen: the verification phase_checks say which basis trial matched each regimen key, so a table reported for
trial X is tagged with the regimen keys verified against X (one approval letter covering two regimens - venetoclax with
azacitidine/decitabine in M14-358 vs with low-dose cytarabine in M14-387 - gets two separate table sets).
Efficacy rows = first-column labels matching the endpoint categories of trial_results.ENDPOINTS (ORR, CR, MRD, PFS, EFS, OS,
DOR, DFS, TTP); hazard ratios as parsed by parse_table.
Output: <data_dir>/label_efficacy.json      Run: python tools/label_efficacy.py --disease cll
"""
import argparse, json, os, re, sys
import xml.etree.ElementTree as ET
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disease_config
from common import load_outputs, scope_regex, strip_paren
from dailymed_label import flow, parse_table, txt
from trial_results import endpoint

V = "{urn:hl7-org:v3}"
NCT = re.compile(r"NCT\d{8}")
VCACHE = os.path.join(disease_config.DATA, "dailymed", "versions")


def s14_subsections(root, scope):
    for s in root.iter(V + "section"):
        c = s.find(V + "code")
        if c is None or c.get("code") != "34092-7":
            continue
        subs = [x for x in s.findall(f"{V}component/{V}section")] or [s]
        for x in subs:
            t = x.find(V + "title")
            title = txt(t) if t is not None else ""
            if scope.search(strip_paren(title)) or scope.search(txt(x)[:400]):
                yield title, x
        return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--disease", required=True)
    a = ap.parse_args()
    cfg = disease_config.load(a.disease)
    scope = scope_regex(cfg)
    R, V_, U = load_outputs(a.disease, "indication_records.json", "regimen_verification.json", "mesh_universe.json.gz")
    from regimen_key import Crosswalk
    from verify_regimens import index_for
    idx = index_for(U, Crosswalk())
    ver = {r["record_id"]: r for r in V_["results"]}
    out = {}
    for rec in R["records"]:
        v = ver.get(rec["record_id"])
        if not v or not v["status"].startswith("verified"):
            continue
        basis = set(v["basis_ncts"])
        key_of = {}
        for ph, lst in v["phase_checks"].items():
            for x in lst:
                if x.get("nct"):
                    key_of.setdefault(x["nct"], []).append(dict(phase=ph, key=x["key"]))
        xml = os.path.join(VCACHE, f"{rec['label']['setid']}_v{rec['label']['version']}.xml")
        if not os.path.exists(xml):
            continue
        T = U["trials"]
        from common import norm_id
        ids_of = {n: {norm_id(x) for x in [T[n].get("org_id")] + T[n].get("secondary_ids", []) if x and len(norm_id(x)) >= 5}
                  for n in basis if n in T}

        def named_in(text):
            """Basis trials named in the text (NCT, registry trial name, org / secondary study id), ordered by LAST position:
            the trial named nearest to the following table comes first."""
            pos = {}
            for m in NCT.finditer(text):
                if m.group(0) in basis:
                    pos[m.group(0)] = max(pos.get(m.group(0), -1), m.start())
            for tok in re.finditer(r"[A-Z][A-Za-z0-9-]{3,}", text):
                for n_ in idx.by_name([tok.group(0)], basis):
                    pos[n_] = max(pos.get(n_, -1), tok.start())
            flat_pos = [(m.start(), m.group(0)) for m in re.finditer(r"[A-Za-z0-9]+(?:[\s\-_/.][A-Za-z0-9]+)*", text)]
            for n_, ids in ids_of.items():
                for st, chunk in flat_pos:
                    if any(i in norm_id(chunk) for i in ids):
                        pos[n_] = max(pos.get(n_, -1), st)
            return sorted(pos, key=lambda x: -pos[x])

        all_keys = [(ph, k) for ph, ks in rec["regimen_keys"].items() for k in ks]
        from regimen_key import parse_key
        arms_of = v.get("arms", {})

        def keys_for(nct):                          # regimen keys verified against this trial (phase checks, else its arms)
            ks = key_of.get(nct, [])
            if not ks and nct in arms_of:
                ks = [dict(phase=ph, key=k) for ph, k in all_keys
                      if any(parse_key(k)["rx"] <= parse_key(a_["key"])["rx"] for a_ in arms_of[nct])]
            return ks

        partner_names = {}                          # regimen key -> partner drug names (label phases), for column tagging
        for ph, d in rec["phases"].items():
            for alt, k in zip(d["alternatives"] or [[]], rec["regimen_keys"].get(ph, [])):
                partner_names[k] = [p_["name"] for p_ in alt if p_.get("name")]
        tables, unassigned = [], 0
        for title, sub in s14_subsections(ET.parse(xml).getroot(), scope):
            sub_text = txt(sub)
            in_sub = sorted(set(named_in(sub_text)))
            ctx = in_sub[0] if len(in_sub) == 1 else None
            for kind, el in flow(sub):
                if kind == "para":
                    t = txt(el)
                    named = named_in(t)
                    if named:
                        ctx = named[0]
                elif kind == "table":
                    tb = parse_table(el)
                    cap_named = named_in(tb["caption"])
                    nct = cap_named[0] if cap_named else ctx
                    rows = [dict(endpoint=endpoint(r[0]), label=r[0], cells=r[1:]) for r in tb["rows"] if r and endpoint(r[0]) not in ("other", "safety", "PK")]
                    if not rows and not tb["hazard_ratios"]:
                        continue                    # baseline / safety tables
                    if not nct:
                        unassigned += 1
                    regs = keys_for(nct)
                    col_reg = {}                    # column header naming a partner drug -> that regimen only
                    for ci, col in enumerate(tb["columns"][1:], 1):
                        if re.search(r"placebo|control|comparator|investigator'?s choice", col, re.I):
                            col_reg[ci] = "comparator"
                            continue
                        # substring, not word match: SPL headers glue words ('withAzacitidineN = 67')
                        hit = [r_["key"] for r_ in regs if any(n_.lower() in col.lower().replace(" ", "")
                                                               or n_.lower() in col.lower() for n_ in partner_names.get(r_["key"], []))]
                        if len(hit) == 1:
                            col_reg[ci] = hit[0]
                    tables.append(dict(subsection=title, nct=nct, regimens=regs, column_regimen=col_reg, caption=tb["caption"], columns=tb["columns"],
                                       efficacy_rows=rows[:15], hazard_ratios=tb["hazard_ratios"]))
        out[rec["record_id"]] = dict(drug=rec["drug"], tables=tables, unassigned_tables=unassigned,
                                     label=dict(setid=rec["label"]["setid"], version=rec["label"]["version"]))
        disease_config.log(a.disease, "label_efficacy", f"{rec['record_id']}: {len(tables)} efficacy tables, {unassigned} without a trial")
    p = os.path.join(disease_config.data_dir(a.disease), "label_efficacy.json")
    json.dump(out, open(p, "w", encoding="utf-8"), indent=1)
    n = sum(len(x["tables"]) for x in out.values())
    print(f"{len(out)} records, {n} efficacy tables, {sum(x['unassigned_tables'] for x in out.values())} without trial -> {p}")


if __name__ == "__main__":
    main()
