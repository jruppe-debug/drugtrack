#!/usr/bin/env python
"""
Source index builder. NO LLM anywhere: raw table pulls only, joined later on identifier values.

Pulls three outside sources into ONE local SQLite (AACT_bridge/inputs/source_index.db) so the exporter can
index-match against them with zero network calls and so a run is reproducible against a stamped snapshot:

  DrugCentral   public read-only Postgres (host unmtid-dbs.net:5433 - the credential below is the one DrugCentral
                publishes on its own download page for this purpose). Tables used: structures, identifier (ChEMBL_ID /
                UNII / DrugBank / PubChem), approval (agency + date), omop_relationship (curated indications),
                act_table_full (curated mechanism-of-action rows, moa=1, with the MoA's own source recorded).
  GtoPdb        IUPHAR/BPS Guide to Pharmacology public CSV downloads (ligands, ligand_id_mapping, interactions,
                targets_and_families). Expert-curated ligand->target interactions with Ensembl gene ids.
  OT targets    Open Targets GraphQL, ONLY for the Ensembl target ids referenced by drug_mechanism_of_action.parquet:
                gives symbol / name / synonyms / UniProt so Open Targets' targets can be compared to the other two
                sources by identifier instead of by free-text protein name.

openFDA is deliberately NOT here: it is per-drug live lookup with a cache, and lives in export_disease_tree.py
(LabelChecker / ApprovalChecker) because its query keys (UNII) come out of this index.

Run:  "C:/Users/jwrup/Clinical Tracker/.venv/Scripts/python.exe" tools/source_index.py [--refresh]
"""
import argparse, csv, json, os, sqlite3, sys, time, urllib.request
from datetime import datetime, timezone

import pyarrow.parquet as pq

BRIDGE = os.environ.get("AACT_BRIDGE_DIR", r"C:\Users\jwrup\Clinical Tracker\AACT_bridge")
BRIDGE_IN = os.path.join(BRIDGE, "inputs")
BULK = os.path.join(BRIDGE_IN, "opentargets")
SRC_DIR = os.path.join(BRIDGE_IN, "sources", "gtopdb")
DB_PATH = os.path.join(BRIDGE_IN, "source_index.db")

DC = dict(host="unmtid-dbs.net", port=5433, dbname="drugcentral", user="drugman", password="dosage")   # DrugCentral's published public read-only login
GT_BASE = "https://www.guidetopharmacology.org/DATA"
GT_FILES = ["ligands.csv", "ligand_id_mapping.csv", "interactions.csv", "targets_and_families.csv"]
OT_GQL = "https://api.platform.opentargets.org/api/v4/graphql"

SCHEMA = """
CREATE TABLE dc_structure(struct_id INTEGER PRIMARY KEY, name TEXT, cas TEXT, inchikey TEXT, status TEXT);
CREATE TABLE dc_identifier(struct_id INTEGER, id_type TEXT, identifier TEXT);
CREATE TABLE dc_approval(struct_id INTEGER, agency TEXT, date TEXT, applicant TEXT, orphan TEXT);
CREATE TABLE dc_indication(struct_id INTEGER, relationship TEXT, concept_name TEXT, umls_cui TEXT, snomed_conceptid TEXT);
CREATE TABLE dc_moa(struct_id INTEGER, target_name TEXT, target_class TEXT, gene TEXT, accession TEXT, swissprot TEXT, action_type TEXT, moa_source TEXT, organism TEXT, act_type TEXT, act_value REAL);
CREATE TABLE dc_activity(struct_id INTEGER, gene TEXT, target_name TEXT, act_type TEXT, act_value REAL, act_unit TEXT, action_type TEXT, act_source TEXT);
CREATE TABLE gt_ligand(ligand_id TEXT PRIMARY KEY, name TEXT, type TEXT, approved TEXT, withdrawn TEXT, chembl_id TEXT, drugbank_id TEXT, drugcentral_id TEXT, pubchem_cid TEXT, inchikey TEXT);
CREATE TABLE gt_interaction(ligand_id TEXT, ligand_name TEXT, target_id TEXT, target_name TEXT, gene_symbol TEXT, uniprot TEXT, ensembl_gene TEXT,
                            target_species TEXT, itype TEXT, action TEXT, primary_target INTEGER, affinity_median TEXT, affinity_units TEXT, pubmed TEXT);
CREATE TABLE gt_target_alias(gene_symbol TEXT, alias TEXT);
CREATE TABLE ot_target(ensembl_id TEXT PRIMARY KEY, symbol TEXT, name TEXT, uniprot TEXT);
CREATE TABLE ot_target_alias(ensembl_id TEXT, alias TEXT);
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX i_dc_act ON dc_activity(struct_id);
CREATE INDEX i_dc_ident ON dc_identifier(id_type, identifier);
CREATE INDEX i_dc_ident_s ON dc_identifier(struct_id);
CREATE INDEX i_dc_struct_ik ON dc_structure(inchikey);
CREATE INDEX i_gt_lig_ch ON gt_ligand(chembl_id);
CREATE INDEX i_gt_int_l ON gt_interaction(ligand_id);
"""


def iso(d):
    return d.isoformat() if d else None


def pull_drugcentral(con):
    import psycopg2
    last = None
    for _ in range(3):
        try:
            pg = psycopg2.connect(connect_timeout=30, options="-c default_transaction_read_only=on", **DC)
            break
        except Exception as e:
            last = e
            time.sleep(5)
    else:
        raise SystemExit(f"DrugCentral unreachable: {last}")
    cur = pg.cursor()

    def q(sql):
        cur.execute(sql)
        return cur.fetchall()

    con.executemany("INSERT INTO dc_structure VALUES (?,?,?,?,?)", [(r[0], r[1], r[2], r[3], r[4]) for r in q("SELECT id, name, cas_reg_no, inchikey, status FROM structures")])
    con.executemany("INSERT INTO dc_identifier VALUES (?,?,?)", [(r[0], r[1], r[2]) for r in q(
        "SELECT struct_id, id_type, identifier FROM identifier WHERE id_type IN ('ChEMBL_ID','UNII','DRUGBANK_ID','PUBCHEM_CID','RXNORM','INN_ID') AND struct_id IS NOT NULL")])
    con.executemany("INSERT INTO dc_approval VALUES (?,?,?,?,?)", [(r[0], r[1], iso(r[2]), r[3], str(r[4])) for r in q(
        "SELECT struct_id, type, approval, applicant, orphan FROM approval WHERE struct_id IS NOT NULL")])
    con.executemany("INSERT INTO dc_indication VALUES (?,?,?,?,?)", [(r[0], r[1], r[2], r[3], r[4]) for r in q(
        "SELECT struct_id, relationship_name, concept_name, umls_cui, snomed_conceptid::text FROM omop_relationship WHERE relationship_name IN ('indication','off-label use') AND struct_id IS NOT NULL")])
    con.executemany("INSERT INTO dc_moa VALUES (?,?,?,?,?,?,?,?,?,?,?)", [tuple(r) for r in q(
        "SELECT struct_id, target_name, target_class, gene, accession, swissprot, action_type, moa_source, organism, act_type, act_value::float FROM act_table_full WHERE moa::int = 1 AND struct_id IS NOT NULL")])
    # Non-curated activity rows (potency measurements). NOT mechanism-of-action claims - used only for the separate, lower
    # 'activity-only' mechanism tier, where no curated mechanism exists at all.
    con.executemany("INSERT INTO dc_activity VALUES (?,?,?,?,?,?,?,?)", [tuple(r) for r in q(
        "SELECT struct_id, gene, target_name, act_type, act_value::float, act_unit, action_type, act_source FROM act_table_full "
        "WHERE (moa IS NULL OR moa::int = 0) AND organism = 'Homo sapiens' AND gene IS NOT NULL AND act_value IS NOT NULL "
        "AND act_type IN ('Ki','Kd','IC50','EC50') AND struct_id IS NOT NULL")])
    con.execute("INSERT INTO meta VALUES ('drugcentral_fetched_at', ?)", (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    mx = q("SELECT type, max(approval) FROM approval WHERE approval IS NOT NULL GROUP BY 1")
    con.execute("INSERT INTO meta VALUES ('drugcentral_latest_approval_by_agency', ?)", (json.dumps({a: iso(d) for a, d in mx}),))
    pg.close()


def gt_download(refresh):
    os.makedirs(SRC_DIR, exist_ok=True)
    for fn in GT_FILES:
        p = os.path.join(SRC_DIR, fn)
        if os.path.exists(p) and not refresh:
            continue
        req = urllib.request.Request(f"{GT_BASE}/{fn}", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=180) as r, open(p, "wb") as f:
            f.write(r.read())
        print(f"  downloaded GtoPdb {fn}", flush=True)


def gt_rows(fn):
    """Yield dict rows; line 1 of every GtoPdb file is a quoted '# GtoPdb Version: ...' comment."""
    with open(os.path.join(SRC_DIR, fn), encoding="utf-8-sig", newline="") as f:
        version = f.readline().strip().strip('"')
        for row in csv.DictReader(f):
            yield version, row


def g(row, *names):
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n].strip()
    return ""


def pull_gtopdb(con):
    version = ""
    lig = {}
    for version, r in gt_rows("ligands.csv"):
        lig[g(r, "Ligand ID")] = dict(name=g(r, "Name"), type=g(r, "Type"), approved=g(r, "Approved"), withdrawn=g(r, "Withdrawn"),
                                      chembl_id=g(r, "ChEMBL ID"), pubchem_cid=g(r, "PubChem CID"), inchikey=g(r, "InChIKey"), drugbank_id="", drugcentral_id="")
    for _, r in gt_rows("ligand_id_mapping.csv"):
        d = lig.setdefault(g(r, "Ligand id", "Ligand ID"), dict(name=g(r, "Name"), type=g(r, "Type"), approved="", withdrawn="", chembl_id="", pubchem_cid="", inchikey="", drugbank_id="", drugcentral_id=""))
        d["chembl_id"] = d["chembl_id"] or g(r, "ChEMBl ID", "ChEMBL ID")
        d["drugbank_id"] = g(r, "DrugBank ID")
        d["drugcentral_id"] = g(r, "Drug Central ID")
        d["pubchem_cid"] = d["pubchem_cid"] or g(r, "PubChem CID")
    con.executemany("INSERT INTO gt_ligand VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(k, v["name"], v["type"], v["approved"], v["withdrawn"], v["chembl_id"], v["drugbank_id"], v["drugcentral_id"], v["pubchem_cid"], v["inchikey"]) for k, v in lig.items() if k])
    inter = []
    for _, r in gt_rows("interactions.csv"):
        lid = g(r, "Ligand ID")
        if not lid:
            continue
        inter.append((lid, g(r, "Ligand"), g(r, "Target ID"), g(r, "Target"), g(r, "Target Gene Symbol"), g(r, "Target UniProt ID"), g(r, "Target Ensembl Gene ID"),
                      g(r, "Target Species"), g(r, "Type"), g(r, "Action"), 1 if g(r, "Primary Target").lower() == "true" else 0,
                      g(r, "Affinity Median"), g(r, "Affinity Units"), g(r, "PubMed ID")))
    con.executemany("INSERT INTO gt_interaction VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", inter)
    aliases = set()
    for _, r in gt_rows("targets_and_families.csv"):
        sym = g(r, "HGNC symbol")
        if not sym:
            continue
        for a in [g(r, "Target abbreviated name"), g(r, "Target name")] + [x for x in g(r, "synonyms").split("|") if x]:
            if a:
                aliases.add((sym, a))
    con.executemany("INSERT INTO gt_target_alias VALUES (?,?)", sorted(aliases))
    con.execute("INSERT INTO meta VALUES ('gtopdb_version', ?)", (version,))


def pull_ot_targets(con):
    """Symbol / synonyms / UniProt for exactly the Ensembl ids Open Targets' mechanism-of-action rows reference."""
    tbl = pq.read_table(os.path.join(BULK, "drug_mechanism_of_action.parquet"), columns=["targets"]).to_pylist()
    ids = sorted({t for r in tbl for t in (r["targets"] or [])})
    query = "query($ids:[String!]!){targets(ensemblIds:$ids){id approvedSymbol approvedName synonyms{label} proteinIds{id source}}}"
    rows, aliases = [], []
    for i in range(0, len(ids), 100):
        body = json.dumps({"query": query, "variables": {"ids": ids[i:i + 100]}}).encode()
        for attempt in range(3):
            try:
                req = urllib.request.Request(OT_GQL, data=body, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
                data = json.load(urllib.request.urlopen(req, timeout=60))
                break
            except Exception:
                time.sleep(3)
        else:
            raise SystemExit("Open Targets GraphQL unreachable")
        for t in data["data"]["targets"]:
            sp = next((p["id"] for p in t.get("proteinIds") or [] if p["source"] == "uniprot_swissprot"), "")
            rows.append((t["id"], t["approvedSymbol"], t["approvedName"], sp))
            for s in t.get("synonyms") or []:
                aliases.append((t["id"], s["label"]))
    con.executemany("INSERT INTO ot_target VALUES (?,?,?,?)", rows)
    con.executemany("INSERT INTO ot_target_alias VALUES (?,?)", aliases)
    return len(ids), len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-download GtoPdb files")
    args = ap.parse_args()
    t0 = time.time()
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    pull_drugcentral(con)
    print(f"[drugcentral] pulled ({time.time()-t0:.0f}s)", flush=True)
    gt_download(args.refresh)
    pull_gtopdb(con)
    print(f"[gtopdb] parsed ({time.time()-t0:.0f}s)", flush=True)
    asked, got = pull_ot_targets(con)
    print(f"[open targets] target symbols: asked {asked}, returned {got} ({time.time()-t0:.0f}s)", flush=True)
    con.commit()
    print("  dc_activity units:", con.execute("select act_unit, count(*) from dc_activity group by 1 order by 2 desc limit 6").fetchall())
    for t in ["dc_structure", "dc_identifier", "dc_approval", "dc_indication", "dc_moa", "dc_activity", "gt_ligand", "gt_interaction", "gt_target_alias", "ot_target", "ot_target_alias"]:
        print(f"  {t:18} {con.execute(f'select count(*) from {t}').fetchone()[0]:>7} rows")
    for k, v in con.execute("select k, v from meta"):
        print(f"  meta {k} = {v[:160]}")
    print(f"wrote {DB_PATH} ({os.path.getsize(DB_PATH)//1024} KB)")


if __name__ == "__main__":
    main()
