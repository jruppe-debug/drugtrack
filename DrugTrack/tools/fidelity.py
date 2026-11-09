"""
Deterministic fidelity logic shared by export_disease_tree.py and the reports. NO LLM: identifier joins,
keyword/symbol matching and tier rules only. Everything here is a pure function of the source index
(source_index.db), the Open Targets parquet files and what the exporter already holds per drug.

Principle: fidelity counts independent ORIGIN FAMILIES, not databases. Sources that ingest the same
underlying documents are one family (Open Targets' US approval evidence, openFDA label text and DailyMed
are all the FDA SPL label; Open Targets' mechanism-of-action and DrugCentral's moa_source='CHEMBL' rows are
both ChEMBL curation). A source silent on a drug is not a contradiction.
"""
import os
import re
import sqlite3
from collections import defaultdict

import pyarrow.parquet as pq

# Precision-oncology target classes, keyed by GENE SYMBOL (an identifier, not free-text protein name), checked
# in this priority order - first match wins (quizartinib/midostaurin hit several off-target kinases but are the
# FLT3 class). Same buckets as the exporter's original name-regex rules; the symbol form lets DrugCentral and
# IUPHAR rows feed the same assignment.
SYMBOL_GROUP_RULES = [("FLT3", "FLT3"), ("IDH1", "IDH1"), ("IDH2", "IDH2"), ("MEN1", "NPM1"), ("BCL2", "BCL2"),
                      ("CD33", "CD33"), ("IL3RA", "CD123"), ("SMO", "Hedgehog (SMO)")]
# Words inside a curated INDICATION concept that restrict it to a mutation group
# (e.g. DrugCentral: "Relapsing acute myeloid leukemia with a susceptible IDH1 mutation").
INDICATION_GROUP_TOKENS = [(re.compile(r"\bFLT3\b|FMS-like tyrosine kinase", re.I), "FLT3"), (re.compile(r"\bIDH1\b", re.I), "IDH1"),
                           (re.compile(r"\bIDH2\b", re.I), "IDH2"), (re.compile(r"\bNPM1\b", re.I), "NPM1")]

# DrugCentral records the source of each mechanism row; map it to an origin family.
DC_MOA_FAMILY = {"CHEMBL": "ChEMBL curation", "DRUG LABEL": "drug label", "IUPHAR": "IUPHAR expert curation"}
OTHER_FAMILY = "literature / other database"
ACTIVITY_PMIN = 7.0     # pActivity >= 7 == potency at or below 100 nM; both DrugCentral act_value and IUPHAR affinity are already on the p-scale


def split_symbols(s):
    return {x.upper() for x in re.split(r"[|,;/\s]+", s or "") if x}


def group_from_symbols(symbols):
    for sym, label in SYMBOL_GROUP_RULES:
        if sym in symbols:
            return label, sym
    return None, None


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def text_mentions(text, aliases):
    """First alias found in free text, else None. Token match, not substring: 'BCL-2' == 'BCL2' (adjacent tokens are
    joined), a multi-word alias must appear as a whole phrase, and aliases of <=4 characters must appear in
    UPPER case as written so the gene MET is not matched by the word 'met'."""
    if not text:
        return None
    padded = " " + norm(text) + " "
    lt = padded.split()
    low = set(lt) | {lt[i] + lt[i + 1] for i in range(len(lt) - 1)}
    rt = re.findall(r"[A-Za-z0-9]+", text)
    raw = set(rt) | {rt[i] + rt[i + 1] for i in range(len(rt) - 1)}
    for a in aliases:
        na = norm(a)
        squashed = na.replace(" ", "")
        if len(squashed) < 3:
            continue
        if len(squashed) <= 4:
            if a.upper() == a and squashed.upper() in {r.upper() for r in raw if r == r.upper()}:
                return a
            continue
        if " " in na:
            if f" {na} " in padded:
                return a
        elif na in low:
            return a
    return None


def scope_term_regex(disease_parquet, scope_id):
    """Every name + exact synonym of the scope disease AND all its descendants, from Open Targets' own ontology,
    as one alternation. Replaces a hand-written disease regex: e.g. 'acute promyelocytic leukemia' (a subtype)
    is found in a label because the ontology says it is under the scope, not because someone remembered to type it."""
    rows = pq.read_table(disease_parquet, columns=["id", "name", "descendants", "exactSynonyms"]).to_pylist()
    by_id = {r["id"]: r for r in rows}
    ids = [scope_id] + list(by_id[scope_id]["descendants"] or [])
    terms = set()
    for i in ids:
        d = by_id.get(i)
        if not d:
            continue
        for t in [d["name"]] + list(d["exactSynonyms"] or []):
            t = (t or "").strip()
            if not t or "," in t or "(" in t:          # inverted MeSH-style / parenthetical variants add noise, not coverage
                continue
            if len(t) >= 5 or (t.isalpha() and t.isupper() and 3 <= len(t) <= 6):
                terms.add(t)
    terms = sorted(terms, key=len, reverse=True)
    return r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(t) for t in terms) + r")(?![A-Za-z0-9])", len(terms), len(ids)


class SourceIndex:
    """Read-only view of source_index.db plus the Open Targets identifiers needed to join it (InChIKey, DrugBank)."""

    def __init__(self, db_path, drug_molecule_parquet):
        self.con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        c = self.con
        self.dc_ident = defaultdict(set)                 # (id_type, identifier) -> struct ids
        self.dc_struct_ident = defaultdict(lambda: defaultdict(set))
        for s, t, i in c.execute("SELECT struct_id, id_type, identifier FROM dc_identifier"):
            self.dc_ident[(t, i)].add(s)
            self.dc_struct_ident[s][t].add(i)
        self.dc_by_ik = defaultdict(set)
        for s, ik in c.execute("SELECT struct_id, inchikey FROM dc_structure WHERE inchikey IS NOT NULL"):
            self.dc_by_ik[ik].add(s)
        self.gt_by_chembl, self.gt_by_dcid, self.gt_by_db = defaultdict(set), defaultdict(set), defaultdict(set)
        for lid, ch, db, dcid in c.execute("SELECT ligand_id, chembl_id, drugbank_id, drugcentral_id FROM gt_ligand"):
            if ch:
                self.gt_by_chembl[ch].add(lid)
            if dcid:
                self.gt_by_dcid[dcid].add(lid)
            if db:
                self.gt_by_db[db].add(lid)
        self.ot_symbol = {e: s for e, s in c.execute("SELECT ensembl_id, symbol FROM ot_target")}
        self.aliases = defaultdict(set)                  # SYMBOL -> aliases (OT synonyms + IUPHAR names)
        for e, a in c.execute("SELECT ensembl_id, alias FROM ot_target_alias"):
            if e in self.ot_symbol:
                self.aliases[self.ot_symbol[e].upper()].add(a)
        for s, a in c.execute("SELECT gene_symbol, alias FROM gt_target_alias"):
            self.aliases[s.upper()].add(a)
        self.dc_by_name, self.gt_by_name = defaultdict(set), defaultdict(set)
        for s_id, nm in c.execute("SELECT struct_id, name FROM dc_structure WHERE name IS NOT NULL"):
            self.dc_by_name[norm(nm)].add(s_id)
        for lid, nm in c.execute("SELECT ligand_id, name FROM gt_ligand WHERE name<>''"):
            self.gt_by_name[norm(nm)].add(lid)
        self.ot_ik, self.ot_drugbank, self.ot_name = {}, defaultdict(set), {}
        for r in pq.read_table(drug_molecule_parquet, columns=["id", "name", "inchiKey", "crossReferences"]).to_pylist():
            self.ot_name[r["id"]] = r["name"]
            if r["inchiKey"]:
                self.ot_ik[r["id"]] = r["inchiKey"]
            for x in r["crossReferences"] or []:
                if x["source"] == "drugbank":
                    self.ot_drugbank[r["id"]].update(x.get("ids") or [])

    def match(self, member_ids):
        """Identifier join, strongest key first, weaker keys only if the stronger found nothing.
        -> ({struct_id: method}, {ligand_id: method})"""
        dc, gt = {}, {}
        for cid in member_ids:
            for s in self.dc_ident.get(("ChEMBL_ID", cid), ()):
                dc.setdefault(s, "ChEMBL ID")
        if not dc:
            for cid in member_ids:
                for s in self.dc_by_ik.get(self.ot_ik.get(cid), ()):
                    dc.setdefault(s, "InChIKey")
        if not dc:
            for cid in member_ids:
                for db in self.ot_drugbank.get(cid, ()):
                    for s in self.dc_ident.get(("DRUGBANK_ID", db), ()):
                        dc.setdefault(s, "DrugBank ID")
        if not dc:                                        # weakest key: identical normalised name, only when every identifier failed
            for cid in member_ids:
                for s in self.dc_by_name.get(norm(self.ot_name.get(cid, "")), ()):
                    dc.setdefault(s, "exact name (weak)")
        for cid in member_ids:
            for lid in self.gt_by_chembl.get(cid, ()):
                gt.setdefault(lid, "ChEMBL ID")
        if not gt:
            for s in dc:
                for lid in self.gt_by_dcid.get(str(s), ()):
                    gt.setdefault(lid, "DrugCentral ID")
        if not gt:
            for cid in member_ids:
                for db in self.ot_drugbank.get(cid, ()):
                    for lid in self.gt_by_db.get(db, ()):
                        gt.setdefault(lid, "DrugBank ID")
        if not gt:
            for cid in member_ids:
                for lid in self.gt_by_name.get(norm(self.ot_name.get(cid, "")), ()):
                    gt.setdefault(lid, "exact name (weak)")
        return dc, gt

    def unii(self, structs):
        return sorted({u for s in structs for u in self.dc_struct_ident[s].get("UNII", ())})

    def evidence(self, dc, gt, scope_re=None):
        """Everything the two sources say about one drug. scope_re (compiled) selects the AML-relevant curated indications."""
        q = self.con.execute
        ev = dict(dc_structs=sorted(dc), dc_match=sorted(set(dc.values())), gt_ligands=sorted(gt), gt_match=sorted(set(gt.values())),
                  unii=self.unii(dc), approvals=[], indications=[], dc_moa=[], gt_targets=[], gt_approved=False, dc_activity=[], gt_activity=[],
                  gt_citations=[])
        for s in dc:
            for ag, dt, ap in q("SELECT agency, date, applicant FROM dc_approval WHERE struct_id=? AND agency IN ('FDA','EMA','PMDA')", (s,)):
                ev["approvals"].append(dict(agency=ag, date=dt, applicant=ap))
            for (name,) in q("SELECT concept_name FROM dc_indication WHERE struct_id=? AND relationship='indication'", (s,)):
                if scope_re is None or scope_re.search(name):
                    ev["indications"].append(name)
            for gene, tname, act, src, org, at, av in q("SELECT gene, target_name, action_type, moa_source, organism, act_type, act_value FROM dc_moa WHERE struct_id=?", (s,)):
                if org == "Homo sapiens" and gene:
                    ev["dc_moa"].append(dict(symbols=sorted(split_symbols(gene)), target=tname, action=act, source=src or "unspecified",
                                             family=DC_MOA_FAMILY.get(src, OTHER_FAMILY), act_type=at, p=av))
        for s in dc:
            for gene, tname, at, val, act in q("SELECT gene, target_name, act_type, act_value, action_type FROM dc_activity WHERE struct_id=?", (s,)):
                ev["dc_activity"].append(dict(symbols=sorted(split_symbols(gene)), target=tname, type=at, p=val, action=act))
        for lid in gt:
            for sym, tname, units, med, act in q("SELECT gene_symbol, target_name, affinity_units, affinity_median, action FROM gt_interaction "
                                                 "WHERE ligand_id=? AND target_species='Human' AND gene_symbol<>'' AND affinity_units LIKE 'p%' AND affinity_median<>''", (lid,)):
                try:
                    ev["gt_activity"].append(dict(symbol=sym.upper(), target=tname, type=units, p=float(med), action=act))
                except ValueError:
                    pass
        for lid in gt:
            r = q("SELECT approved FROM gt_ligand WHERE ligand_id=?", (lid,)).fetchone()
            ev["gt_approved"] = ev["gt_approved"] or (r is not None and r[0] == "yes")
            for sym, tname, act, pubmed in q("SELECT gene_symbol, target_name, action, pubmed FROM gt_interaction WHERE ligand_id=? AND primary_target=1 AND target_species='Human' AND gene_symbol<>''", (lid,)):
                ev["gt_targets"].append(dict(symbol=sym.upper(), target=tname, action=act))
                if pubmed:
                    pmids = sorted({p.strip() for p in pubmed.split("|") if p.strip()})
                    if pmids:
                        ev["gt_citations"].append(dict(symbol=sym.upper(), target=tname, pmids=pmids,
                                                        urls=[f"https://pubmed.ncbi.nlm.nih.gov/{p}/" for p in pmids]))
        ev["approvals"] = [dict(t) for t in {tuple(sorted(a.items())) for a in ev["approvals"]}]
        ev["indications"] = sorted(set(ev["indications"]))
        return ev

    def mechanism_families(self, ot_ensembl_ids, ev, label_moa_text=None):
        """family -> set of gene symbols. Families are ORIGINS: two rows in one family are one origin."""
        fam = defaultdict(set)
        for e in ot_ensembl_ids or []:
            if e in self.ot_symbol:
                fam["ChEMBL curation"].add(self.ot_symbol[e].upper())          # Open Targets MoA is ChEMBL-curated
        for m in ev.get("dc_moa", []):
            fam[m["family"]].update(m["symbols"])
        for t in ev.get("gt_targets", []):
            fam["IUPHAR expert curation"].add(t["symbol"])
        if label_moa_text:
            # keyword lookup of candidate targets (any family) inside the label's own mechanism-of-action text
            candidates = set().union(*fam.values()) if fam else set()
            for sym in candidates:
                if text_mentions(label_moa_text, {sym} | self.aliases.get(sym, set())):
                    fam["drug label"].add(sym)
        return dict(fam)


def potency_rank(ev, symbol):
    """Where does `symbol` rank among this drug's own measured potencies, highest first? Pool is CURATED sources only
    - DrugCentral's own curated MoA potency (dc_moa's p) and IUPHAR affinity - deliberately excluding raw dc_activity
    (DrugCentral's uncurated high-throughput screening panel, act_table_full with moa=0). 0 = the drug's single most
    potent known target. None = no curated potency data exists for this drug (the rule that uses this then falls
    back, since selectivity can't be judged). This is what separates "FLT3 is this drug's defining target"
    (gilteritinib/midostaurin/crenolanib/quizartinib, FLT3 ranks top-2) from "FLT3 is one of many kinases this
    promiscuous inhibitor happens to hit" (sunitinib/pexidartinib: several targets measurably more potent) - found
    live: BOTH classes were originally assigned to the FLT3 group on ChEMBL's mere presence of the interaction, with
    no potency check. A second bug found live: including raw dc_activity in the pool buried midostaurin's genuine,
    curated, rank-0 FLT3 potency under ~200 weak screening-panel off-target hits (a kinase-selectivity assay tests
    hundreds of kinases at once, most barely binding) - a promiscuity SCREEN is not the same claim as a promiscuity
    FINDING, so it is excluded from ranking (raw dc_activity is still used for the separate activity-only tier,
    where by definition no curated mechanism exists to ruin)."""
    best = defaultdict(lambda: -1e9)
    for m in ev.get("dc_moa", []):
        if m.get("p") is not None:
            for sym in m["symbols"]:
                best[sym] = max(best[sym], m["p"])
    for a in ev.get("gt_activity", []):
        best[a["symbol"]] = max(best[a["symbol"]], a["p"])
    if not best or symbol not in best:
        return None, len(best)
    order = sorted(best, key=lambda k: -best[k])
    return order.index(symbol), len(best)


def activity_families(ev, pmin=ACTIVITY_PMIN):
    """family -> {symbol: best pActivity} from raw potency rows at or above pmin. These are measurements, not mechanism-of-action
    claims: they feed only the separate 'activity-only' tier and can never originate a mutation/mechanism group."""
    fam = defaultdict(dict)
    for a in ev.get("dc_activity", []):
        if a["p"] >= pmin:
            for sym in a["symbols"]:
                fam["DrugCentral potency"][sym] = max(fam["DrugCentral potency"].get(sym, 0.0), a["p"])
    for a in ev.get("gt_activity", []):
        if a["p"] >= pmin:
            fam["IUPHAR affinity"][a["symbol"]] = max(fam["IUPHAR affinity"].get(a["symbol"], 0.0), a["p"])
    return {k: dict(v) for k, v in fam.items()}


def mechanism_tier(fam, act=None):
    """corroborated = some target is listed by >=2 origin families; single-family = targets known from one origin only;
    activity-only = NO curated mechanism anywhere, but potency measurements exist (lowest tier, never a mechanism claim)."""
    if not fam or not any(fam.values()):
        if act and any(act.values()):
            sup = defaultdict(set)
            for f, d in act.items():
                for sym in d:
                    sup[sym].add(f)
            return "activity-only", {sym: sorted(v) for sym, v in sup.items()}
        return "no data", {}
    support = defaultdict(set)
    for f, syms in fam.items():
        for s in syms:
            support[s].add(f)
    return ("corroborated" if any(len(v) >= 2 for v in support.values()) else "single-family"), {s: sorted(v) for s, v in support.items()}


def approval_tier(strict_families, confirmations):
    """strict_families: set of independent origin families stating the approval for the scope disease
    (US label family, EU regulator, JP regulator, reference page). confirmations: count of separate concordance
    items (curated indication database agrees; a dated regulator record exists for the same regulator; dates agree)."""
    n = len(strict_families)
    if n >= 2 or (n == 1 and confirmations >= 2):
        return "A"
    if n == 1 and confirmations == 1:
        return "B"
    if n == 1:
        return "C"
    return "D"
