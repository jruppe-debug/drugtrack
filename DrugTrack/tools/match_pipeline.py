"""
Drug x disease -> trial matching pipeline (DrugTrack RAG outline, V1 + BM25).

    candidates (OT clinical reports + AACT-native condition search)
      -> canonical intervention object (DrugIndex synonyms, OT mechanisms, ontology synonyms)
      -> HARD FILTERS (never overridden by ranking)
      -> BM25 rank per drug
      -> MATCH / REVIEW / REJECT, each with auditable reasons and "why matched" evidence

AACT is the source of truth for trial content. Everything is deterministic: no embeddings, no LLM, no cosine
threshold. Embeddings (V2) come after the labeled set exists, so they can be backtested against it.

Usage (from DrugTrack/):
  ../.venv/Scripts/python tools/match_pipeline.py pull     # AACT -> data/aact_cache.json.gz (slow, network; run when AACT refreshes)
  ../.venv/Scripts/python tools/match_pipeline.py run      # cache -> data/match.json (seconds; rerun after any filter/rank change)
  ../.venv/Scripts/python tools/match_pipeline.py seed     # data/labels_seed.csv: ~150 stratified rows, proposed_label filled, human_label blank
  ../.venv/Scripts/python tools/match_pipeline.py seed --refresh   # same rows, current columns + commentary, human input kept -> data/labels_seed_v2.csv
  ../.venv/Scripts/python tools/match_pipeline.py eval [labels.csv]   # P@10, R@10, P@50, FPR, 3-class accuracy on human-labeled rows
"""
import argparse
import csv
import gzip
import importlib.util
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager

import pyarrow.parquet as pq

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(DATA, "aact_cache.json.gz")
MATCH_OUT = os.path.join(DATA, "match.json")
SEED_OUT = os.path.join(DATA, "labels_seed.csv")
LABELS = os.path.join(DATA, "labels.csv")
SEED_V2 = os.path.join(DATA, "labels_seed_v2.csv")
# Human input files, lowest precedence first. Labels and notes in later files override earlier ones.
HUMAN_FILES = [SEED_OUT, SEED_V2, LABELS]
HUMAN_TIERS = {"FLAGSHIP", "VERIFIED", "NO"}
CACHE_VERSION = 3
GRAVEYARD_OUT = os.path.join(DATA, "graveyard.json")


def _exporter():
    """Reuse the exporter's DrugIndex / Ontology / AACT client / quick_tags so both tools resolve names identically."""
    argv, sys.argv = sys.argv, ["x"]
    try:
        spec = importlib.util.spec_from_file_location("export_disease_tree", os.path.join(HERE, "export_disease_tree.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.argv = argv


ex = _exporter()


# ============================================================================ AACT pull (one reused connection)
def pooled_client():
    """AACTClient opens a new TCP + auth handshake per run_query; reuse one read-only connection for the whole pull."""
    client = ex.aact_client()
    state = {"conn": None}

    @contextmanager
    def reused():
        if state["conn"] is None or state["conn"].closed:
            state["conn"] = _open(client)
        try:
            yield state["conn"]
        finally:
            state["conn"].rollback()   # end the read-only transaction; keep the socket

    client._connection = reused
    client._state = state
    return client


def _open(client):
    import psycopg2
    return psycopg2.connect(host=client.host, port=client.port, dbname=client.dbname, user=client.user,
                            password=client.password, options="-c default_transaction_read_only=on")


def run_query(aact, sql, params, limit, tries=3):
    """AACT's public endpoint drops connections under load (seen: 'Connection timed out'); retry with backoff."""
    for attempt in range(tries):
        try:
            return aact.run_query(sql, params, limit=limit)
        except Exception as e:   # psycopg2.OperationalError / InterfaceError
            if attempt == tries - 1 or type(e).__name__ not in ("OperationalError", "InterfaceError"):
                raise
            if aact._state["conn"] is not None:
                try:
                    aact._state["conn"].close()
                except Exception:
                    pass
            time.sleep(5 * 2 ** attempt)


def scope_terms(onto):
    """Disease strings for the scope node and its descendants: OT name + exact synonyms (lowercase, len >= 4)."""
    ids = {onto.scope} | onto.descendants
    D = pq.read_table(os.path.join(ex.BULK, "disease.parquet"), columns=["id", "name", "exactSynonyms"]).to_pylist()
    terms = defaultdict(set)   # term -> disease ids
    for d in D:
        if d["id"] in ids:
            for t in [d["name"], *(d["exactSynonyms"] or [])]:
                t = re.sub(r"\s+", " ", (t or "").strip().lower())
                if len(t) >= 4:
                    terms[t].add(d["id"])
    return terms


# AML abbreviation / spelling variants the ontology strings miss ("AML", "acute myelogenous leukaemia", "ANLL").
# Also the inverted MeSH form "Leukemia, Myeloid, Acute".
SCOPE_RX = re.compile(r"(?<![a-z0-9])(?:aml|anll|acute\s+(?:myeloid|myelogenous|myeloblastic|non-?lymphocytic|nonlymphoblastic)\s+leuka?emia|"
                      r"leuka?emia,\s*(?:myeloid|myelogenous|non-?lymphocytic),\s*acute)(?![a-z0-9])", re.I)
SCOPE_SQL_RX = (r"(^|[^a-z0-9])(aml|anll|acute (myeloid|myelogenous|myeloblastic|non-?lymphocytic|nonlymphoblastic) leuka?emia|"
                r"leuka?emia, *(myeloid|myelogenous|non-?lymphocytic), *acute)([^a-z0-9]|$)")


def pull(args):
    t0 = time.time()
    onto = ex.Ontology(args.scope_id)
    con = sqlite3.connect(f"file:{os.path.join(ex.BRIDGE_IN, 'clinical_tracker.db')}?mode=ro", uri=True)
    ot_pairs = con.execute("""SELECT DISTINCT i.chembl_id, upper(cr.nct_id) FROM indications i
                              JOIN indication_reports ir USING(indication_id) JOIN clinical_reports cr USING(report_id)
                              WHERE i.disease_id=? AND cr.nct_id IS NOT NULL""", (args.scope_id,)).fetchall()
    ot_pairs = [(c, n) for c, n in ot_pairs if ex.NCT_RE.match(n)]
    aact = pooled_client()
    terms = sorted(scope_terms(onto))
    # AACT-native recall: interventional trials whose conditions or keywords name the scope disease.
    native = set()
    for table in ("conditions", "keywords"):
        rows = run_query(aact, f"""SELECT DISTINCT c.nct_id FROM ctgov.{table} c JOIN ctgov.studies s USING(nct_id)
                                   WHERE s.study_type = 'INTERVENTIONAL' AND (c.downcase_name = ANY(%s) OR c.downcase_name ~ %s)""",
                         (terms, SCOPE_SQL_RX), limit=200000)
        native |= {r["nct_id"] for r in rows}
    print(f"[pull] OT pairs={len(ot_pairs)} OT trials={len({n for _, n in ot_pairs})} AACT-native trials={len(native)} ({time.time()-t0:.0f}s)", flush=True)

    ncts = sorted({n for _, n in ot_pairs} | native)
    trials = {n: dict(nct_id=n, conditions=[], keywords=[], interventions=[], arms=[], arm_links=[], outcomes=[]) for n in ncts}
    q = {
        "studies": ("""SELECT nct_id, brief_title, official_title, study_type, phase, overall_status, enrollment, start_date, completion_date, why_stopped,
                              results_first_posted_date
                       FROM ctgov.studies WHERE nct_id = ANY(%s)""", None),
        "outcomes": ("SELECT nct_id, outcome_type, measure, time_frame FROM ctgov.design_outcomes WHERE nct_id = ANY(%s)", "outcomes"),
        "summary": ("SELECT nct_id, description FROM ctgov.brief_summaries WHERE nct_id = ANY(%s)", None),
        "elig": ("SELECT nct_id, criteria, healthy_volunteers, minimum_age, maximum_age FROM ctgov.eligibilities WHERE nct_id = ANY(%s)", None),
        "design": ("SELECT nct_id, primary_purpose FROM ctgov.designs WHERE nct_id = ANY(%s)", None),
        "conditions": ("SELECT nct_id, name FROM ctgov.conditions WHERE nct_id = ANY(%s)", "conditions"),
        "keywords": ("SELECT nct_id, name FROM ctgov.keywords WHERE nct_id = ANY(%s)", "keywords"),
        "arms": ("SELECT nct_id, id, group_type, title, description FROM ctgov.design_groups WHERE nct_id = ANY(%s)", "arms"),
        "arm_links": ("SELECT nct_id, design_group_id, intervention_id FROM ctgov.design_group_interventions WHERE nct_id = ANY(%s)", "arm_links"),
        "interventions": ("""SELECT i.nct_id, i.id, i.intervention_type, i.name, array_remove(array_agg(o.name), NULL) AS other_names
                             FROM ctgov.interventions i LEFT JOIN ctgov.intervention_other_names o ON o.intervention_id = i.id
                             WHERE i.nct_id = ANY(%s) GROUP BY i.id, i.nct_id, i.intervention_type, i.name""", "interventions"),
    }
    for key, (sql, list_field) in q.items():
        t1, n = time.time(), 0
        for ch in ex.chunks(ncts, 1000):
            for r in run_query(aact, sql, (ch,), limit=500000):
                t, n = trials[r.pop("nct_id")], n + 1
                if list_field == "conditions" or list_field == "keywords":
                    t[list_field].append(r["name"])
                elif list_field:
                    t[list_field].append(r)
                else:
                    t.update(r)
        print(f"[pull] {key}: rows={n} ({time.time()-t1:.0f}s)", flush=True)
    if aact._state["conn"] is not None:
        aact._state["conn"].close()
    # Fold the arm-intervention links into each intervention as the group types of the arms that give it
    # (EXPERIMENTAL, ACTIVE_COMPARATOR, ...). The flagship screen needs to know the drug's role, not just its presence.
    for t in trials.values():
        arm_type = {a["id"]: a.get("group_type") for a in t["arms"]}
        by_iv = defaultdict(set)
        links = t.pop("arm_links")
        for link in links:
            by_iv[link["intervention_id"]].add(arm_type.get(link["design_group_id"]) or "UNKNOWN")
        arm_ids = defaultdict(list)
        for link in links:
            arm_ids[link["intervention_id"]].append(link["design_group_id"])
        for iv in t["interventions"]:
            iv["arm_types"] = sorted(by_iv.get(iv["id"], ()))
            iv["arm_ids"] = sorted(arm_ids.get(iv["id"], ()))
    missing = [n for n, t in trials.items() if "study_type" not in t]
    out = dict(version=CACHE_VERSION, pulled_at=time.strftime("%Y-%m-%dT%H:%M:%S"), scope_id=args.scope_id,
               ot_pairs=ot_pairs, native=sorted(native), missing_in_aact=missing, trials=trials)
    with gzip.open(CACHE, "wt", encoding="utf-8") as f:
        json.dump(out, f, default=str)
    print(f"[pull] trials={len(trials)} missing in AACT={len(missing)} -> {CACHE} ({time.time()-t0:.0f}s total)")


# ============================================================================ canonical intervention object
def canonical_drugs(di, roots):
    dm = {d["id"]: d for d in pq.read_table(os.path.join(ex.BULK, "drug_molecule.parquet"),
                                            columns=["id", "drugType", "synonyms"]).to_pylist()}
    targets, group, _ = ex.load_mechanisms(di)
    moa = defaultdict(set)
    tbl = pq.read_table(os.path.join(ex.BULK, "drug_mechanism_of_action.parquet"), columns=["chemblIds", "mechanismOfAction"]).to_pylist()
    for row in tbl:
        for cid in row["chemblIds"] or []:
            if di.root_of.get(cid) in roots:
                moa[di.root_of[cid]].add(row["mechanismOfAction"])
    out = {}
    for r in roots:
        syn = set()
        for m in di.members.get(r, [r]):
            syn |= {x["label"] for x in (dm.get(m, {}).get("synonyms") or []) if x.get("label")}
        out[r] = dict(drug=di.root_name(r), chembl_root=r, synonyms=sorted(syn - {di.root_name(r)}),
                      trade_names=sorted(lab for lab, _ in di.trade.get(r, {}).values()),
                      arm_terms=sorted(di.arm_terms(r)),
                      targets=sorted({t for t, _ in targets.get(r, set()) if t}),
                      mechanism=sorted(m for m in moa.get(r, set()) if m), target_group=group.get(r),
                      modality=(dm.get(r) or {}).get("drugType"))
    return out


# ============================================================================ hard filters
PK_ONLY_RX = re.compile(r"bioequivalen|food[- ]effect|effect\s+of\s+food|drug[- ]drug\s+interaction|\bDDI\b|mass[- ]balance|relative\s+bioavailability|"
                        r"(?:hepatic|renal)\s+impairment|\bQTc?\b\s+(?:study|prolongation)|thorough\s+QT|absorption,?\s+metabolism", re.I)
HEME_RX = re.compile(r"leuka?emi|lymphom|myelom|myelodysplas|\bMDS\b|myeloproliferat|hematolog|haematolog|blood\s+cancer|myeloid\s+(?:neoplasm|malignan)|"
                     r"myelofibrosis|blastic\s+plasmacytoid|\bCMML\b|\bALL\b|\bCML\b|\bCLL\b|neoplasm of (?:the )?(?:blood|bone marrow)|bone marrow", re.I)
SOLID_RX = re.compile(r"carcinom|sarcom|melanom|glioma|glioblastom|blastoma\b|solid\s+tumou?r|\btumou?rs?\b|cancer|adenocarcinom|mesotheliom|"
                      r"(?:breast|lung|prostate|colorectal|pancrea|ovarian|gastric|hepatocellular|renal|bladder|cervical|head and neck)", re.I)


_TERMS_RX = {}
IV_SPLIT_RX = re.compile(r"\s*(?:;|\+|,|/|\band\b)\s*", re.I)
_ARM_RX = {}   # chembl_root -> compiled alternation of that drug's arm terms


def terms_rx(terms):
    """One compiled word-bounded alternation over all scope terms (longest first). Calling ex.term_in once per term
    rebuilt a regex per call: 628 terms overflow re's 512-pattern cache, so every call recompiled (~3.6 s per trial)."""
    key = id(terms)
    if key not in _TERMS_RX:
        alts = sorted((re.escape(t) for t in terms if len(t) >= 6), key=len, reverse=True)
        _TERMS_RX[key] = re.compile(r"(?<![a-z0-9])(?:" + "|".join(alts) + r")(?![a-z0-9])")
    return _TERMS_RX[key]


def disease_hits(texts, terms):
    """(term, text) pairs where a scope disease string (or the AML abbreviation pattern) occurs."""
    hits, rx = [], terms_rx(terms)
    for txt in texts:
        low = (txt or "").lower()
        if low.strip() in terms:
            hits.append((low.strip(), txt))
            continue
        m = SCOPE_RX.search(low)
        if m:
            hits.append((m.group(0), txt))
            continue
        m = rx.search(re.sub(r"\s+", " ", low))
        if m:
            hits.append((m.group(0), txt))
    return hits


def drug_hits(di, root, canon, trial):
    """Where the drug appears: 'intervention' (resolved by DrugIndex, the strict signal) or 'text' (arm/title mention only)."""
    found = []
    for iv in trial["interventions"]:
        names = [iv["name"], *(iv.get("other_names") or [])]
        # Some sponsors register a whole regimen as one intervention ("Azacytidine;Cytarabine;Chidamide"),
        # so the parts are looked up too.
        names += [p.strip() for n in names for p in IV_SPLIT_RX.split(n) if p.strip() and p.strip() != n]
        for name in names:
            kind, ids = di.lookup(name)
            if kind in ("one", "combo") and root in ids:
                found.append(("intervention", f"{iv['intervention_type']}: '{name}' -> {canon['drug']} (DrugIndex {kind})"))
                break
    if found:
        return found
    blob = " ".join([trial.get("brief_title") or "", trial.get("official_title") or ""] + [f"{a['title']} {a.get('description') or ''}" for a in trial["arms"]]
                    + [iv["name"] for iv in trial["interventions"]]).lower()
    root_key = canon["chembl_root"]
    if root_key not in _ARM_RX:
        alts = sorted((re.escape(re.sub(r"\s+", " ", t.strip().lower())) for t in {*canon["arm_terms"], *canon["synonyms"]} if len(t.strip()) >= 4), key=len, reverse=True)
        _ARM_RX[root_key] = re.compile(r"(?<![a-z0-9])(?:" + "|".join(alts) + r")(?![a-z0-9])") if alts else None
    m = _ARM_RX[root_key].search(re.sub(r"\s+", " ", blob)) if _ARM_RX[root_key] else None
    if m:
        return [("text", f"'{m.group(0)}' named in title/arms/interventions but no intervention record resolves to {canon['drug']}")]
    return []


def hard_filters(trial, terms=None):
    """Trial-level REJECT reasons. These are never overridden by ranking."""
    reasons = []
    if trial.get("study_type") and trial["study_type"] != "INTERVENTIONAL":
        reasons.append(f"non-interventional study ({trial['study_type']})")
    # AML trials often tick "accepts healthy volunteers" by mistake (human labels NCT05149339, NCT05986240).
    if trial.get("healthy_volunteers") is True and not (terms and aml_conditions(trial, terms)):
        reasons.append("accepts healthy volunteers and AML is not a registered condition")
    title = f"{trial.get('brief_title') or ''} {trial.get('official_title') or ''}"
    m = PK_ONLY_RX.search(title)
    if m:
        reasons.append(f"PK/clinical-pharmacology design ('{m.group(0)}' in title)")
    conds = trial["conditions"]
    if conds and not any(HEME_RX.search(c) for c in conds) and any(is_solid(c) for c in conds):
        reasons.append("solid-tumor-only conditions: " + "; ".join(conds[:4]))
    return reasons


# ============================================================================ semantic layer (V2, from the 2026-10-05 human labels)
# Population layer: AML-specific trials are the layer of truth; mixed and basket trials only supplement them.
MPAL_RX = re.compile(r"mixed[- ]phenotype|ambiguous lineage|biphenotypic|bilineal|\bMPAL\b|\bALAL\b", re.I)
GENERIC_COND_RX = re.compile(r"^\s*(?:cancer|neoplasms?|malignan\w*|tumou?rs?)\s*$", re.I)
TRANSPLANT_RX = re.compile(r"transplant|\bH?SCT\b|\bHCT\b|stem[- ]cell|bone marrow|cord blood|conditioning|graft|\bGVHD\b|donor|\bTBI\b", re.I)
# Drug-drug interaction studies: the co-administered drugs are probes or perpetrators, not a treatment regimen.
DDI_RX = re.compile(r"drug[- ]drug\s+interaction|\bDDI\b|perpetrator|victim\s+drug|probe\s+(?:drug|substrate|cocktail)|\bCYP\s?\d[A-Z]\d*\s+(?:inhibit|induc|substrate)", re.I)
DDI_PROBE_RX = re.compile(r"\bprobes?\b|cocktail|midazolam|itraconazole|ketoconazole|posaconazole|voriconazole|fluconazole|cobicistat|ritonavir|"
                          r"rifampi|carbamazepine|caffeine|digoxin|rosuvastatin|omeprazole|warfarin|dextromethorphan|bupropion|repaglinide|"
                          r"metformin|dabigatran|fexofenadine|pioglitazone|famotidine|esomeprazole|pantoprazole", re.I)
LONG_TERM_RX = re.compile(r"long[- ]term|extension stud|roll-?over|continued (?:access|treatment)|expanded access|follow[- ]up study|survivorship", re.I)
EFFICACY_OUT_RX = re.compile(r"respon|remission|\bCRi?\b|\bCRh\b|surviv|\bOS\b|\bEFS\b|\bRFS\b|\bDFS\b|relapse|efficacy|\bMRD\b|"
                             r"transfusion independen|\bORR\b|time to|duration of", re.I)
SAFETY_OUT_RX = re.compile(r"safety|tolerab|adverse|toxicit|\bMTD\b|maximum tolerated|\bDLTs?\b|dose[- ]limiting|"
                           r"recommended phase (?:2|II)|\bRP2D\b|side effect", re.I)
PK_OUT_RX = re.compile(r"pharmacokinetic|\bC ?max\b|\bAUC|bioavailab|bioequivalen|half[- ]life|plasma concentration", re.I)
# Word-bounded copies of HEME_FAMILIES ("ALL" must not match "allogeneic" or "small").
LAYER_FAMILIES = {"MDS": re.compile(r"myelodysplas|\bMDS\b", re.I),
                  "MPN/CMML": re.compile(r"myeloproliferat|myelomonocytic|\bCMML\b|myelofibrosis|polycythemia|thrombocythemia", re.I),
                  "CML": re.compile(r"chronic myelo(?:genous|id) leuka?emia|\bCML\b", re.I),
                  "ALL": re.compile(r"lymphoblastic|\bALL\b|\bB-ALL\b|\bT-ALL\b"),
                  "CLL/lymphoma": re.compile(r"lymphocytic leuka?emia|lymphoma|\bCLL\b", re.I),
                  "myeloma": re.compile(r"myeloma|plasma cell", re.I)}
LAYER_RANK = {"aml_specific": 4, "myeloid": 3, "mixed_heme": 2, "basket": 1, "unscoped": 0}
TRUTH_LAYERS = {"aml_specific", "myeloid"}
ROLE_RANK = {"efficacy+safety": 3, "efficacy": 2, "safety": 1}


def is_solid(c):
    return bool(SOLID_RX.search(c)) and not HEME_RX.search(c) and not GENERIC_COND_RX.match(c)


GENERIC_LEUK_RX = re.compile(r"^\s*leuka?emias?\s*$", re.I)


def aml_conditions(trial, terms):
    """Registered conditions that name AML. Mixed-phenotype / ambiguous-lineage leukemia is not AML (human label NCT04872790).
    A bare 'Leukemia' condition counts when the title names AML (older registrations, e.g. RATIFY NCT00651261). Human rule 2026-10-06."""
    named = title_aml(trial, terms)
    return [c for c in trial["conditions"] if not MPAL_RX.search(c)
            and (disease_hits([c], terms) or AML_SUBTYPE_RX.search(c) or (named and GENERIC_LEUK_RX.match(c)))]


def title_aml(trial, terms):
    return bool(disease_hits([trial.get("brief_title") or "", trial.get("official_title") or ""], terms))


def population_layer(trial, terms):
    """aml_specific: AML only. myeloid: AML + MDS/MPN/CML. mixed_heme: AML + one lymphoid family.
    basket: solid tumors, 2+ lymphoid families or 4+ families. unscoped: AML not a registered condition."""
    aml = aml_conditions(trial, terms)
    if not aml:
        return "unscoped"
    others = [c for c in trial["conditions"] if c not in aml]
    if all(GENERIC_LEUK_RX.match(c) for c in aml):
        # AML named only in the title: the title also carries the other families (NCT00548847: AML, ALL, BP CML, MDS).
        others += [trial.get("brief_title") or "", trial.get("official_title") or ""]
    fams = {f for c in others for f, rx in LAYER_FAMILIES.items() if rx.search(c)}
    if all(GENERIC_LEUK_RX.match(c) for c in aml) and any(re.search(r"lymphoblastic", c, re.I) for c in others):
        fams.add("ALL")
    if any(MPAL_RX.search(c) for c in others):
        fams.add("ALL")
    lymphoid = fams - {"MDS", "MPN/CMML", "CML"}
    if any(is_solid(c) for c in others) or len(lymphoid) >= 2 or len(fams) >= 4:
        return "basket"
    if lymphoid:
        return "mixed_heme"
    return "myeloid" if fams else "aml_specific"


def transplant_procedure(trial):
    """A transplant / conditioning protocol: transplant language in the title and a PROCEDURE or RADIATION intervention.
    Drug-only post-transplant trials (PTCy NCT02969980, briquilimab conditioning NCT04429191) stay drug trials, as does any transplant trial whose title names the drug."""
    title = f"{trial.get('brief_title') or ''} {trial.get('official_title') or ''}"
    types = {iv["intervention_type"] for iv in trial["interventions"]}
    return bool(TRANSPLANT_RX.search(title)) and bool(types & {"PROCEDURE", "RADIATION"})


def study_role(trial, focus=False):
    """What the study measures. Treatment trials: efficacy, safety or efficacy+safety, read from registered primary
    outcomes with phase as the fallback. Others: pk, long_term, supportive, transplant_procedure, screening."""
    purpose = (trial.get("primary_purpose") or "").upper()
    title = f"{trial.get('brief_title') or ''} {trial.get('official_title') or ''}"
    if purpose in ("SCREENING", "DIAGNOSTIC"):
        return "screening"
    if transplant_procedure(trial) and not focus:
        return "transplant_procedure"
    if DDI_RX.search(title):
        return "drug_interaction"
    if PK_ONLY_RX.search(title):
        return "pk"
    if LONG_TERM_RX.search(title) or (trial.get("phase") or "").upper() == "PHASE4":
        return "long_term"
    if purpose in ("SUPPORTIVE_CARE", "PREVENTION", "HEALTH_SERVICES_RESEARCH"):
        return "supportive"
    prim = " ; ".join(o.get("measure") or "" for o in trial.get("outcomes") or [] if (o.get("outcome_type") or "").lower() == "primary")
    eff, saf = bool(EFFICACY_OUT_RX.search(prim)), bool(SAFETY_OUT_RX.search(prim))
    if eff and saf:
        return "efficacy+safety"
    if eff:
        return "efficacy"
    if saf:
        return "safety"
    if prim and PK_OUT_RX.search(prim):
        return "pk"
    return "safety" if (trial.get("phase") or "").upper() in ("PHASE1", "EARLY_PHASE1") else "efficacy"


def has_primary_outcome(trial):
    return any((o.get("outcome_type") or "").lower() == "primary" for o in trial.get("outcomes") or [])


def semantic_decide(decision, reasons, canon, trial, terms):
    """Second pass over the V1 call, from the 2026-10-05 human labels. Returns (decision, reasons, fail_level).
    fail_level 'semantic' marks a row this layer moved to REJECT, so the graveyard can say which logic failed it.
    Transplant/conditioning protocols (a treatment direction, not a therapy) and basket trials (too general for an
    investigational-level call) say nothing about a drug regimen's safety or efficacy, so they never supplement a central study.
    fail_level 'expansion' keeps one as REVIEW when it still has data for the larger thesis (a registered primary outcome and
    the drug in its interventions); otherwise it is a semantic REJECT. Human rules 2026-10-06."""
    if decision == "REJECT":
        return decision, reasons, "hard"
    if "study_type" not in trial:
        return decision, reasons, None
    focus = title_focus(canon, trial)
    role, layer = study_role(trial, focus), population_layer(trial, terms)
    conds = trial["conditions"]
    # Older CTG records register "Leukemia" and put AML in keywords. Accept that when the title names the drug and
    # every condition is hematologic (human labels NCT00002693, NCT00462787; generic "Cancer" stays unnamed, NCT00617929).
    kw_named = focus and disease_hits(trial["keywords"], terms) and conds and all(HEME_RX.search(c) and not MPAL_RX.search(c) for c in conds)
    named = layer != "unscoped" or title_aml(trial, terms) or bool(kw_named)
    sem, supp = [], []
    if layer == "unscoped" and any(MPAL_RX.search(c) for c in conds):
        sem.append("mixed-phenotype / ambiguous-lineage population; AML arm not separable")
    if role == "screening":
        sem.append("screening/diagnostic study with no investigational arm")
    if role == "transplant_procedure":
        supp.append("transplant/conditioning protocol (procedure or radiation), not a drug regimen")
    if layer == "basket":
        supp.append("basket of solid and hematologic tumors" if any(is_solid(c) for c in conds) else "hematologic basket; too general for an AML investigational call")
    if not named:
        heme = [c for c in conds if HEME_RX.search(c) or MPAL_RX.search(c)]
        if heme and all(MPAL_RX.search(c) or LAYER_FAMILIES["ALL"].search(c) for c in heme):
            sem.append("lymphoid / ambiguous-lineage population; AML not named")
        elif any(is_solid(c) for c in conds):
            sem.append("solid and hematologic population; AML not named")
        elif SUPPORTIVE_RX.search(canon["drug"]):
            sem.append("supportive-care agent; AML named in neither conditions nor title")
        elif len({f for c in conds for f, rx in LAYER_FAMILIES.items() if rx.search(c)}) >= 2:
            sem.append("hematologic basket; AML not named")
    if any(r.endswith("absent from interventions, arms and title") for r in reasons) and not focus \
            and (trial.get("primary_purpose") or "").upper() not in ("TREATMENT", ""):
        sem.append(f"drug absent and purpose is {trial.get('primary_purpose')}")
    if (trial.get("phase") or "").upper() in ("NA", "") and not focus and layer not in TRUTH_LAYERS:
        sem.append("no drug-development phase, drug not in title, population not AML-specific")
    if sem:
        return "REJECT", reasons + sem + supp, "semantic"
    if supp:
        if has_primary_outcome(trial) and not any("absent from" in r for r in reasons):
            return "REVIEW", reasons + ["expansion only: " + "; ".join(supp)], "expansion"
        return "REJECT", reasons + supp + ["no evaluable data point for the larger thesis"], "semantic"
    if decision == "REVIEW":
        # Promote when AML is the stated population (conditions, or the title when conditions are generic "Leukemia")
        # and the drug is what the trial is about.
        purpose = (trial.get("primary_purpose") or "").upper()
        purpose_ok = purpose == "TREATMENT" or (focus and purpose == "SUPPORTIVE_CARE" and not SUPPORTIVE_RX.search(canon["drug"]))
        drug_ok = focus or not any("named only in text" in r or "absent from" in r for r in reasons)
        if (layer in TRUTH_LAYERS or (layer == "unscoped" and named)) and purpose_ok and drug_ok and role not in ("pk", "drug_interaction", "long_term"):
            return "MATCH", [], None
    return decision, reasons, None


def decide(di, root, canon, trial, terms, ot_linked):
    if "study_type" not in trial:
        return "REVIEW", ["NCT not found in AACT (withdrawn/renumbered?)"], []
    reasons = hard_filters(trial, terms)
    why = []
    if ot_linked:
        why.append("Open Targets clinical report links this drug-disease pair to the NCT")
    dh = drug_hits(di, root, canon, trial)
    if not dh:
        reasons.append(f"{canon['drug']} absent from interventions, arms and title")
    why += [d for _, d in dh]
    cond_hits = disease_hits(trial["conditions"], terms)
    kw_hits = disease_hits(trial["keywords"] + [trial.get("brief_title") or "", trial.get("official_title") or ""], terms)
    if cond_hits:
        why.append("condition '" + cond_hits[0][1] + f"' matches scope term '{cond_hits[0][0]}'")
    elif kw_hits:
        why.append("scope term '" + kw_hits[0][0] + "' in keywords/title (not in conditions)")
    # Drug absence alone is not one of the outline's hard filters. When Open Targets links the pair,
    # the trial usually names the drug under a regimen acronym (VaBU, CACAG), so a human decides.
    if ot_linked and not dh and reasons == [reasons[-1]]:
        return "REVIEW", reasons, why
    if reasons:
        return "REJECT", reasons, why
    review = []
    if dh[0][0] != "intervention":
        review.append("drug named only in text, not a resolved intervention")
    if not cond_hits:
        if kw_hits:
            review.append("scope disease in keywords/title only")
        elif any(HEME_RX.search(c) for c in trial["conditions"]):
            review.append("broad hematologic conditions, scope disease not named: " + "; ".join(trial["conditions"][:4]))
        else:
            review.append("scope disease not named in conditions/keywords/title: " + "; ".join(trial["conditions"][:4]))
    if review:
        return "REVIEW", review, why
    return "MATCH", [], why


# ============================================================================ flagship screen (strict tier inside MATCH)
# A MATCH says the trial gives the drug to AML patients. The flagship screen is stricter: the drug must be the
# investigational agent of a treatment trial in an AML population. One flagship per drug anchors the tree, and
# supplemental sources (PubMed, sponsor, HCP, FDA label) attach to it once verified.
SUPPORTIVE_RX = re.compile(r"filgrastim|lenograstim|sargramostim|molgramostim|mesna|mercaptoethanesulfon|setron\b|aprepitant|"
                           r"posaconazole|voriconazole|fluconazole|isavucon|micafungin|caspofungin|amphotericin|letermovir|acyclovir|"
                           r"tacrolimus|sirolimus|mycophenol|cyclosporin|ciclosporin|antithymocyte|antilymphocyte|abatacept|"
                           r"allopurinol|rasburicase|dexrazoxane|acetylcysteine|deferasirox|heparin|tranexamic|eltrombopag|romiplostim", re.I)
# Co-enrolling AML with myeloid neighbours (MDS, CMML/MPN) or with ALL is normal for an AML trial. Several lymphoid
# families, or four families of any kind, mean an all-comers hematologic basket where AML is a minority.
HEME_FAMILIES = {"MDS": re.compile(r"myelodysplas|\bMDS\b", re.I),
                 "MPN/CMML": re.compile(r"myeloproliferat|myelomonocytic|\bCMML\b|myelofibrosis|polycythemia|thrombocythemia", re.I),
                 "CML": re.compile(r"chronic myelo(?:genous|id) leuka?emia|\bCML\b", re.I),
                 "ALL": re.compile(r"lymphoblastic|\bALL\b", re.I),
                 "CLL/lymphoma": re.compile(r"lymphocytic leuka?emia|lymphoma|\bCLL\b", re.I),
                 "myeloma": re.compile(r"myeloma|plasma cell", re.I)}
# FAB/WHO AML subtype names in NCI-style condition lists that the ontology synonyms miss.
AML_SUBTYPE_RX = re.compile(r"\(M[0-7][ab]?\)|erythroid leuka?emia|mono(?:blastic|cytic) leuka?emia|megakaryoblastic|"
                            r"(?<!chronic )myeloid leuka?emia|myeloblastic|promyelocytic|myelomonocytic leuka?emia \(M4", re.I)
COMPARATOR_ARMS = {"ACTIVE_COMPARATOR", "PLACEBO_COMPARATOR", "SHAM_COMPARATOR", "NO_INTERVENTION"}
PLACEBO_RX = re.compile(r"placebo|sham|vehicle", re.I)
LIVE = {"RECRUITING", "ACTIVE_NOT_RECRUITING", "NOT_YET_RECRUITING", "ENROLLING_BY_INVITATION"}
PHASE_RANK = {"PHASE3": 6, "PHASE2/PHASE3": 5, "PHASE2": 4, "PHASE1/PHASE2": 3, "PHASE1": 2, "EARLY_PHASE1": 1, "PHASE4": 0}


def resolved_ivs(di, root, trial):
    """The trial's intervention records that resolve to this drug, including parts of regimen strings."""
    out = []
    for iv in trial["interventions"]:
        names = [iv["name"], *(iv.get("other_names") or [])]
        names += [p.strip() for n in names for p in IV_SPLIT_RX.split(n) if p.strip()]
        if any(kind in ("one", "combo") and root in ids for kind, ids in map(di.lookup, names)):
            out.append(iv)
    return out


def iv_drug_names(di, iv):
    names = set()
    for part in [iv["name"], *IV_SPLIT_RX.split(iv["name"])]:
        kind, ids = di.lookup(part.strip()) if part.strip() else ("none", ())
        if kind in ("one", "combo"):
            names |= {di.root_name(r) for r in ids}
    return names or {iv["name"]}


def flagship_screen(di, root, canon, trial, terms):
    """Returns (fails, role, regimen). No fails means the trial verifies this drug's investigational arm."""
    fails = []
    if (trial.get("primary_purpose") or "").upper() != "TREATMENT":
        fails.append(f"primary purpose is {trial.get('primary_purpose') or 'not registered'}, not TREATMENT")
    if PHASE_RANK.get((trial.get("phase") or "").upper()) is None:
        fails.append(f"no drug-development phase ({trial.get('phase')})")
    if (trial.get("overall_status") or "").upper() in ("WITHDRAWN",):
        fails.append("withdrawn before enrollment")
    if SUPPORTIVE_RX.search(canon["drug"]) or any(SUPPORTIVE_RX.search(m) for m in canon["mechanism"]):
        fails.append("supportive-care agent, not an AML therapy")
    ivs = resolved_ivs(di, root, trial)
    types = {t for iv in ivs for t in iv.get("arm_types") or ()}
    drug_arms = {a for iv in ivs for a in iv.get("arm_ids") or ()}
    placebo_only = any(a.get("group_type") == "PLACEBO_COMPARATOR" and a.get("id") not in drug_arms for a in trial["arms"])
    if types == {"ACTIVE_COMPARATOR"} and placebo_only:
        # Drug added only in arms registered ACTIVE_COMPARATOR against a placebo arm: the drug is what the trial tests
        # (VIALE-A NCT02993523). Human rule 2026-10-06.
        types = {"EXPERIMENTAL"}
        ivs = [dict(iv, arm_types=["EXPERIMENTAL"]) for iv in ivs]
    if "EXPERIMENTAL" in types:
        role = "EXPERIMENTAL"
    elif not types and len(trial["arms"]) <= 1:
        role = "single-arm (no arm links)"
    else:
        role = "/".join(sorted(types)) or "unlinked in a multi-arm trial"
        fails.append(f"drug only in {role} arm(s)")
    if "EXPERIMENTAL" in types and types & COMPARATOR_ARMS:
        # Given in both the experimental and the control arm, the drug is the backbone, not what the trial tests.
        role = "backbone"
        fails.append("backbone: drug is in both experimental and comparator arms")
    conds = trial["conditions"]
    if not disease_hits(conds, terms) and not aml_conditions(trial, terms):
        fails.append("AML not named in registered conditions")
    fams = {f for c in conds if not AML_SUBTYPE_RX.search(c) and not disease_hits([c], terms)
            for f, rx in HEME_FAMILIES.items() if rx.search(c)}
    lymphoid = fams - {"MDS", "MPN/CMML"}
    if len(lymphoid) >= 2 or len(fams) >= 4:
        fails.append(f"basket population (AML plus {', '.join(sorted(fams))})")
    if any(SOLID_RX.search(c) and not HEME_RX.search(c) for c in conds):
        fails.append("mixed solid-tumor and hematologic population")
    exp_arms = {a for iv in ivs if "EXPERIMENTAL" in (iv.get("arm_types") or ()) for a in iv.get("arm_ids") or ()}
    regimen = sorted({n for iv in trial["interventions"] if iv not in ivs and exp_arms & set(iv.get("arm_ids") or ())
                      and iv["intervention_type"] in ex.DRUG_LIKE and not PLACEBO_RX.search(iv["name"]) for n in iv_drug_names(di, iv)}
                     | {n for iv in ivs for n in iv_drug_names(di, iv)} - {canon["drug"]})
    return fails, role, regimen


def flagship_key(row):
    """Flagship preference: AML-specific layer, then pivotal (phase 3 with posted results), then any posted results, then
    efficacy+safety role, then drug named in the title, then live, then latest phase, then largest enrollment, then BM25.
    Pivotal-with-results outranks live because a central study must carry comparable arm data. Human rule 2026-10-06."""
    posted = bool(row.get("results_posted"))
    pivotal = posted and row["phase"] in ("PHASE3", "PHASE2/PHASE3")
    return (LAYER_RANK.get(row.get("layer"), 0), pivotal, posted, ROLE_RANK.get(row.get("study_role"), 0), row["title_focus"], row["status"] in LIVE,
            PHASE_RANK.get(row["phase"] or "", -1), row.get("enrollment") or 0, row["bm25"])


def title_focus(canon, trial):
    title = re.sub(r"\s+", " ", f"{trial.get('brief_title') or ''} {trial.get('official_title') or ''}".lower())
    names = {canon["drug"], *canon["synonyms"], *canon["trade_names"]}
    if any(re.search(r"(?<![a-z0-9])" + re.escape(x.lower()) + r"(?![a-z0-9])", title) for x in names if len(x) >= 4):
        return True
    # Development codes appear as "AMG 553", "AMG-553" or "AMG553"; compare them with separators removed.
    flat = re.sub(r"[^a-z0-9]", "", title)
    return any(len(c) >= 5 and re.search(r"[a-z]\d", c) and c in flat for c in (re.sub(r"[^a-z0-9]", "", x.lower()) for x in names))


# ============================================================================ BM25 (hand-rolled, no dependency)
STOP = set("a an and are as at be by for from in into is of on or the to with without this that these those will be study trial phase patients "
           "patient subjects treatment treated therapy dose doses mg day days".split())


def tokens(text):
    return [w for w in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", (text or "").lower()) if w not in STOP and len(w) > 1]


def trial_doc(t):
    return " ".join([t.get("brief_title") or "", t.get("official_title") or "", t.get("description") or "", " ".join(t["conditions"]),
                     " ".join(t["keywords"]), " ".join(f"{i['name']} {' '.join(i.get('other_names') or [])}" for i in t["interventions"]),
                     " ".join(f"{a['title']} {a.get('description') or ''}" for a in t["arms"]), (t.get("criteria") or "")[:3000]])


class BM25:
    def __init__(self, docs, k1=1.2, b=0.75):
        self.k1, self.b = k1, b
        self.tf = {k: Counter(tokens(v)) for k, v in docs.items()}
        self.len = {k: sum(c.values()) for k, c in self.tf.items()}
        self.avg = sum(self.len.values()) / max(1, len(self.len))
        df = Counter(w for c in self.tf.values() for w in c)
        n = len(self.tf)
        self.idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in df.items()}

    def score(self, key, query_terms):
        tf, L, s, contrib = self.tf[key], self.len[key], 0.0, []
        for w in query_terms:
            f = tf.get(w, 0)
            if f:
                v = self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * L / self.avg))
                s += v
                contrib.append((w, round(v, 2)))
        return round(s, 3), sorted(contrib, key=lambda x: -x[1])[:5]


def query_terms(canon, disease_strings):
    """Canonical object -> query token SET (each token once, so a drug with 40 synonyms does not outweigh the disease)."""
    parts = [canon["drug"], *canon["synonyms"][:15], *canon["trade_names"], *canon["targets"], *canon["mechanism"], *disease_strings]
    return sorted(set(tokens(" ".join(parts))))


# ============================================================================ run
DECISION_ORDER = {"MATCH": 0, "REVIEW": 1, "REJECT": 2}


def commentary(t, decision, reasons, fails, role, regimen, focus):
    """NCT-level verification note built only from the cached AACT record, so a reviewer can check each claim."""
    bits = [" ".join(x for x in (t.get("phase") or "no phase", t.get("overall_status") or "", f"n={t.get('enrollment') or '?'}") if x)]
    if role:
        bits.append(f"arm role {role}" + (f" with {' + '.join(regimen[:6])}" if regimen else ", no linked partner drugs"))
    bits.append("title names drug" if focus else "drug not in title")
    if reasons:
        bits.append(f"{decision} because " + "; ".join(reasons))
    if decision != "REJECT":
        bits.append("flagship screen passes" if not fails else "flagship screen fails: " + "; ".join(fails))
    return ". ".join(bits) + "."


def read_csv(path):
    # Excel saves CSV as cp1252 unless told otherwise, so fall back to it.
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(path, newline="", encoding=enc) as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    sys.exit(f"Cannot decode {path}")


def load_human():
    """(chembl_root, nct) -> {label, tier, notes, file} from every human file present."""
    out = {}
    for path in HUMAN_FILES:
        if not os.path.exists(path):
            continue
        for r in read_csv(path):
            label = (r.get("human_label") or "").strip().upper()
            tier = (r.get("human_tier") or "").strip().upper()
            notes = (r.get("notes") or "").strip()
            if label and label not in DECISION_ORDER or tier and tier not in HUMAN_TIERS:
                print(f"warning: {os.path.basename(path)} {r.get('nct_id')}: ignored human_label={label!r} human_tier={tier!r}", file=sys.stderr)
            if label in DECISION_ORDER or tier in HUMAN_TIERS or notes:
                cur = out.setdefault((r["chembl_root"], r["nct_id"]), {})
                cur.update({k: v for k, v in (("label", label if label in DECISION_ORDER else ""), ("tier", tier if tier in HUMAN_TIERS else ""),
                                                ("notes", notes)) if v}, file=os.path.basename(path))
    return out


def load_cache():
    if not os.path.exists(CACHE):
        sys.exit(f"No cache at {CACHE}; run 'pull' first.")
    with gzip.open(CACHE, "rt", encoding="utf-8") as f:
        c = json.load(f)
    if c.get("version") != CACHE_VERSION:
        sys.exit(f"Cache version {c.get('version')} != {CACHE_VERSION}; rerun 'pull'.")
    return c


_TAGS = {}


def trial_tags(nct, t):
    """Same fields and order as the exporter's per-trial tags (conditions, keywords, eligibility INCLUSION, title), so the two agree.
    Memoized per trial: quick_tags costs ~12 ms and a trial appears in one pair per drug."""
    if nct not in _TAGS:
        fields = [t["conditions"], t["keywords"], [ex.inclusion_part(t.get("criteria"))], [t.get("brief_title") or ""]]
        _TAGS[nct] = ex.quick_tags(" ; ".join(" ; ".join(f) for f in fields)) if "study_type" in t else []
    return _TAGS[nct]


def run(args):
    t0 = time.time()
    c = load_cache()
    di = ex.DrugIndex()
    onto = ex.Ontology(c["scope_id"])
    terms = scope_terms(onto)
    trials = c["trials"]
    # Resolve any intervention string the OT synonym index misses through sponsor-supplied other names already in the cache.
    for t in trials.values():
        for iv in t["interventions"]:
            if di._lookup1(iv["name"])[0] == "none":
                for o in iv.get("other_names") or []:
                    kind, ids = di._lookup1(o)
                    if kind in ("one", "combo") and iv["name"] not in di.aliases:
                        di.aliases[iv["name"]], di.alias_via[iv["name"]] = set(ids), o
                        break
    pairs = {}   # (root, nct) -> source set
    scope_roots = set()
    for cid, nct in c["ot_pairs"]:
        r = di.root_of.get(cid, cid)
        scope_roots.add(r)
        pairs.setdefault((r, nct), set()).add("opentargets")
    for nct in c["native"]:
        for iv in trials[nct]["interventions"]:
            if iv["intervention_type"] not in ex.DRUG_LIKE:
                continue
            kind, ids = di.lookup(iv["name"])
            for r in (ids if kind in ("one", "combo") else ()):
                if r in scope_roots:
                    pairs.setdefault((r, nct), set()).add("aact_native")
    canon = canonical_drugs(di, scope_roots)
    scope_name = onto.name.get(c["scope_id"], c["scope_id"])
    dis_syn = sorted(t for t, ids in terms.items() if c["scope_id"] in ids)
    bm = BM25({n: trial_doc(t) for n, t in trials.items() if "study_type" in t})
    human = load_human()
    rows = []
    for (r, nct), src in pairs.items():
        t = trials[nct]
        decision, reasons, why = decide(di, r, canon[r], t, terms, "opentargets" in src)
        v1_decision = decision
        decision, reasons, fail_level = semantic_decide(decision, reasons, canon[r], t, terms)
        score, top = bm.score(nct, query_terms(canon[r], dis_syn)) if nct in bm.tf else (0.0, [])
        if top:
            why.append("BM25 top terms: " + ", ".join(f"{w} ({v})" for w, v in top))
        fails, role, regimen = flagship_screen(di, r, canon[r], t, terms) if decision != "REJECT" else ([], None, [])
        # DDI probes/perpetrators and the drug's own code names are not regimen partners.
        own = [x.lower() for x in (canon[r]["drug"], *canon[r]["synonyms"]) if len(x) >= 4]
        ddi_probes = [n for n in regimen if DDI_PROBE_RX.search(n)]
        regimen = [n for n in regimen if n not in ddi_probes and not any(re.search(r"(?<![a-z0-9])" + re.escape(o) + r"(?![a-z0-9])", n.lower()) for o in own)]
        focus = title_focus(canon[r], t)
        srole = study_role(t, focus) if "study_type" in t else None
        layer = population_layer(t, terms) if "study_type" in t else "unscoped"
        # Foundational study: an investigational arm with a clear regimen and a registered primary outcome to evaluate.
        if decision == "MATCH":
            if not has_primary_outcome(t):
                fails.append("no registered primary outcome (no evaluable metric)")
            if srole not in ROLE_RANK:
                fails.append(f"study role is {srole}, not efficacy/safety")
            if layer not in TRUTH_LAYERS:
                fails.append(f"population layer is {layer}, not AML-specific or myeloid")
        auto_tier = "VERIFIED" if decision == "MATCH" and not fails else None
        # Human labels override the pipeline; the pipeline's own call is kept in auto_* so eval never scores human against human.
        h = human.get((r, nct), {})
        final = h.get("label") or decision
        # Expansion rows never reach MATCH, even on a human MATCH label from before the 2026-10-06 rule. A human REJECT stays.
        expansion = fail_level == "expansion" or (h.get("label") and (layer == "basket" or srole == "transplant_procedure"))
        if expansion and final == "MATCH":
            final = "REVIEW"
        expansion = bool(expansion) and final == "REVIEW"
        tier = auto_tier if final == "MATCH" else None
        if final == "MATCH" and h.get("tier") in ("FLAGSHIP", "VERIFIED"):
            tier = "VERIFIED"
        elif h.get("tier") == "NO":
            tier = None
        rows.append(dict(tier=tier, auto_tier=auto_tier, human_tier=h.get("tier"), flagship_fails=fails, arm_role=role, title_focus=focus,
                         layer=layer, study_role=srole, v1_decision=v1_decision, expansion=("transplant" if srole == "transplant_procedure" else "basket") if expansion else None,
                         fail_level="human" if final == "REJECT" and decision != "REJECT" else (fail_level if final == "REJECT" else None),
                         regimen_key=" + ".join(sorted({canon[r]["drug"], *regimen})), start_date=t.get("start_date"),
                         primary_outcomes=[o["measure"] for o in t.get("outcomes") or [] if (o.get("outcome_type") or "").lower() == "primary"][:4],
                         why_stopped=t.get("why_stopped"),
                         regimen=regimen, ddi_probes=ddi_probes, enrollment=t.get("enrollment"), results_posted=t.get("results_first_posted_date"), drug=canon[r]["drug"], chembl_root=r, disease=scope_name, nct_id=nct, sources=sorted(src),
                         decision=final, auto_decision=decision, decided_by="human" if h.get("label") else "pipeline", human_note=h.get("notes"),
                         commentary=commentary(t, decision, reasons, fails, role, regimen, focus),
                         reasons=reasons, why_matched=why, bm25=score, title=t.get("brief_title"), phase=t.get("phase"),
                         status=t.get("overall_status"), conditions=t["conditions"][:6], setting_tags=trial_tags(nct, t)))
    by_drug = defaultdict(list)
    for row in rows:
        by_drug[row["chembl_root"]].append(row)
    anchors = load_anchors()
    for lst in by_drug.values():
        auto = max((x for x in lst if x["auto_tier"]), key=flagship_key, default=None)
        if auto:
            auto["auto_flagship"] = True
        anchor = anchors.get(lst[0]["chembl_root"])
        lead = anchored_central(lst, anchor)
        if lead:
            # FDA approval package is the source of truth for approved drugs: its earliest efficacy trial is central,
            # whatever the layer gate, role or ranking key say. Human REJECT labels are never overridden (anchored_central).
            lead["tier"] = "FLAGSHIP"
        else:
            # A human FLAGSHIP pin wins over the ranking key.
            verified = sorted((x for x in lst if x["tier"]), key=lambda x: (x["human_tier"] == "FLAGSHIP", flagship_key(x)), reverse=True)
            if verified:
                verified[0]["tier"] = "FLAGSHIP"
        attach_supplemental(lst, anchor if lead else None)
        lst.sort(key=lambda x: (DECISION_ORDER[x["decision"]], -x["bm25"], x["nct_id"]))
        for i, row in enumerate(lst, 1):
            row["rank"] = i if row["decision"] != "REJECT" else None
    rows.sort(key=lambda x: (x["drug"], x["rank"] or 10 ** 6, x["nct_id"]))
    dec = Counter(r["decision"] for r in rows)
    graveyard = [graveyard_row(r) for r in rows if r["decision"] == "REJECT"]
    reason_counts = Counter(re.sub(r"[:(].*", "", re.sub(r"^.* absent from", "drug absent from", x)).strip() for r in rows for x in r["reasons"])
    src_counts = Counter("+".join(r["sources"]) for r in rows)
    summary = dict(pairs=len(rows), drugs=len(by_drug), trials=len({r["nct_id"] for r in rows}), decisions=dict(dec),
                   sources=dict(src_counts), reasons=dict(reason_counts.most_common()),
                   native_only_match=sum(r["sources"] == ["aact_native"] and r["decision"] == "MATCH" for r in rows),
                   verified_pairs=sum(r["tier"] is not None for r in rows),
                   drugs_with_flagship=sum(r["tier"] == "FLAGSHIP" for r in rows),
                   live_flagships=sum(r["tier"] == "FLAGSHIP" and r["status"] in LIVE for r in rows),
                   flagship_fails=dict(Counter(re.sub(r"[(].*| is .*|only in .*", "", x).strip() for r in rows for x in r["flagship_fails"]).most_common()),
                   human_labeled=sum(r["decided_by"] == "human" for r in rows),
                   human_overrides=sum(r["decided_by"] == "human" and r["decision"] != r["auto_decision"] for r in rows),
                   resolved=dec["MATCH"] + dec["REJECT"],
                   expansion=dict(Counter(r["expansion"] for r in rows if r["expansion"])),
                   resolved_incl_expansion=dec["MATCH"] + dec["REJECT"] + sum(bool(r["expansion"]) for r in rows),
                   v1_decisions=dict(Counter(r["v1_decision"] for r in rows)),
                   semantic_moves=dict(Counter(f"{r['v1_decision']}->{r['auto_decision']}" for r in rows if r["v1_decision"] != r["auto_decision"])),
                   layers_match=dict(Counter(r["layer"] for r in rows if r["decision"] == "MATCH")),
                   central_in_truth_layer=sum(r["tier"] == "FLAGSHIP" and r["layer"] in TRUTH_LAYERS for r in rows),
                   central_fda_anchored=sum(r["tier"] == "FLAGSHIP" and bool(r.get("approval_basis")) for r in rows),
                   central_live_excluded=[f"{r['drug']} {r['nct_id']} {r['status']}" for r in rows if r["tier"] == "FLAGSHIP" and r.get("central_exclusion")],
                   supplemental=dict(Counter(r["relation"] for r in rows if r.get("relation") and r["relation"] != "central")),
                   graveyard=len(graveyard), graveyard_by_level=dict(Counter(g["fail_level"] for g in graveyard)),
                   graveyard_salvage=dict(Counter(g["salvage_as"] for g in graveyard if g["salvage_as"])),
                   ot_reject=sum("opentargets" in r["sources"] and r["decision"] == "REJECT" for r in rows))
    out = dict(meta=dict(generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"), aact_pulled_at=c["pulled_at"], scope_id=c["scope_id"],
                         scope_name=scope_name, method="hard filters + BM25 (V1) + semantic layer (V2: population layer, study role, foundational arm); no embeddings, no LLM",
                         disease_terms=len(terms), summary=summary),
               drugs={r: canon[r] for r in sorted(by_drug)}, pairs=rows)
    with open(MATCH_OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    with open(GRAVEYARD_OUT, "w", encoding="utf-8") as f:
        json.dump(dict(meta=dict(generated_at=out["meta"]["generated_at"], count=len(graveyard),
                                 note="Pairs from the broad universe search that failed a level of the logic. fail_level: hard (registry filters), "
                                      "semantic (population/role rules), human (label). salvage_as: may still serve as a supplemental source."),
                       rows=graveyard), f, indent=1)
    print(json.dumps(summary, indent=1))
    print(f"wrote {MATCH_OUT} ({time.time()-t0:.0f}s)")


SALVAGE = {"pk": "pk", "drug_interaction": "drug_interaction", "safety": "safety", "long_term": "long_term", "supportive": "supportive"}


def graveyard_row(r):
    note = (r.get("human_note") or "").lower()
    salvage = "supplemental" if "supplement" in note else SALVAGE.get(r["study_role"])
    if r["layer"] == "basket" or r["study_role"] == "transplant_procedure":
        salvage = None
    elif not salvage and r["layer"] == "mixed_heme":
        salvage = "mixed_heme"
    return dict(drug=r["drug"], chembl_root=r["chembl_root"], nct_id=r["nct_id"], title=r["title"], phase=r["phase"], status=r["status"],
                fail_level=r["fail_level"] or "hard", reasons=r["reasons"], layer=r["layer"], study_role=r["study_role"],
                salvage_as=salvage, human_note=r.get("human_note"))


def load_anchors():
    """data/central_anchor.json (tools/central_anchor.py): per approved drug, the trials its FDA labels/reviews/announcements
    name as efficacy evidence, keyed here by chembl_root. Missing file = no anchoring."""
    p = os.path.join(DATA, "central_anchor.json")
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        d = json.load(f)["drugs"]
    return {v["chembl_root"]: v for v in d.values() if v.get("status") == "anchored"}


def anchored_central(lst, anchor):
    """Central for an approved drug = lead trial of its earliest approval whose documents name an AML trial of this drug.
    Year-sort check: only anchors that start before their approval (validation verified) can be central. A live central is
    allowed only with this exclusion recorded: the FDA names it as approval evidence while it still enrolls/follows up."""
    if not anchor:
        return None
    rows = {x["nct_id"]: x for x in lst}
    for a in anchor["anchors"]:
        x = rows.get(a["nct"])
        if a["role"] != "lead" or a["validation"] != "verified" or not x:
            continue
        if x["decision"] == "REJECT" and x["decided_by"] == "human":
            x["approval_conflict"] = f"FDA names this trial as efficacy basis ({a['approval_date']}) but the human label is REJECT"
            continue
        x["approval_basis"] = a
        x["approved_regimen"] = a["approved_regimen_key"]
        if x["status"] in LIVE:
            x["central_exclusion"] = f"{x['status'].lower()}: named as efficacy basis in FDA {a['submission']} {a['approval_date']} ({', '.join(a['sources'])})"
        return x
    return None


def attach_supplemental(lst, anchor=None):
    """Hang the drug's other studies on its central (FLAGSHIP) study with a relation, and link each regimen across time.
    companion = same regimen with an efficacy/safety role; otherwise the study role (safety, pk, long_term, supportive, basket).
    Approved drugs (anchor): later approvals' trials = approval_lead / approval_co_basis; a trial of an approved regimen =
    companion of that regimen; a combination never approved = investigational_combination (listed apart from approvals)."""
    by_reg = defaultdict(list)
    for x in lst:
        if x["decision"] != "REJECT":
            by_reg[x["regimen_key"]].append(x)
    for reg in by_reg.values():
        reg.sort(key=lambda x: (x.get("start_date") or "9999", x["nct_id"]))
        for i, x in enumerate(reg):
            x["regimen_prior"] = [y["nct_id"] for y in reg[max(0, i - 3):i]]
            x["regimen_next"] = [y["nct_id"] for y in reg[i + 1:i + 4]]
        last = reg[-1]
        st = (last["status"] or "").upper()
        if st in ("TERMINATED", "WITHDRAWN", "SUSPENDED"):
            outcome = f"discontinued ({st.lower()}" + (f": {last['why_stopped'][:120]}" if last.get("why_stopped") else "") + ")"
        else:
            outcome = "ongoing" if st in LIVE else (st.lower() or "unknown")
        for x in reg:
            x["regimen_outcome"] = outcome
    central = next((x for x in lst if x["tier"] == "FLAGSHIP"), None)
    if not central:
        return
    central["relation"] = "central"
    approved, basis = {central.get("approved_regimen") or central["regimen_key"]}, {}
    for a in (anchor or {}).get("anchors", []):
        if a["validation"] == "verified":
            approved.add(a["approved_regimen_key"])
            basis.setdefault(a["nct"], a)
    for x in lst:
        if x is central or x.get("expansion"):
            continue
        a = basis.get(x["nct_id"])
        if a and x["decision"] != "REJECT":
            x["approval_basis"], x["approved_regimen"] = a, a["approved_regimen_key"]
            rel = "approval_lead" if a["role"] == "lead" else "approval_co_basis"
        elif x["decision"] == "REJECT":
            rel = graveyard_row(x)["salvage_as"]
            if not rel:
                continue
        elif x["study_role"] == "transplant_procedure" or x["layer"] == "basket":
            continue
        elif x["study_role"] == "drug_interaction" or (x["study_role"] == "pk" and x.get("ddi_probes")):
            # DDI / probe studies: own supplemental group, never a combination regimen
            rel = "drug_interaction"
        elif x["layer"] == "mixed_heme":
            rel = "mixed_heme"
        elif anchor and x["regimen_key"] in approved and x["study_role"] in ROLE_RANK:
            rel = "companion"
            x["approved_regimen"] = x["regimen_key"]
        elif anchor and x["regimen"]:
            rel = "investigational_combination"
        elif x["regimen_key"] == central["regimen_key"] and x["study_role"] in ROLE_RANK:
            rel = "companion"
        else:
            rel = x["study_role"] or "related"
        x["relation"], x["central_nct"] = rel, central["nct_id"]


# ============================================================================ seed + eval
SEED_FIELDS = ["drug", "chembl_root", "nct_id", "title", "phase", "status", "proposed_label", "proposed_tier", "arm_role", "regimen", "commentary",
               "sources", "bm25", "rank", "conditions", "human_label", "human_tier", "notes"]


def seed_row(p, h):
    return dict(drug=p["drug"], chembl_root=p["chembl_root"], nct_id=p["nct_id"], title=p["title"], phase=p["phase"], status=p["status"],
                proposed_label=p["auto_decision"], proposed_tier="FLAGSHIP" if p.get("auto_flagship") else (p["auto_tier"] or ""),
                arm_role=p["arm_role"] or "", regimen=" + ".join(p["regimen"]), commentary=p["commentary"], sources="+".join(p["sources"]),
                bm25=p["bm25"], rank=p["rank"] or "", conditions="; ".join(p["conditions"]),
                human_label=h.get("label", ""), human_tier=h.get("tier", ""), notes=h.get("notes", ""))


def seed(args):
    with open(MATCH_OUT, encoding="utf-8") as f:
        m = json.load(f)
    human = load_human()
    if args.refresh:
        # Same rows as the existing seed, new columns, human columns carried over. Written beside it, never over it.
        pred = {(p["chembl_root"], p["nct_id"]): p for p in m["pairs"]}
        keys = [(r["chembl_root"], r["nct_id"]) for r in read_csv(SEED_OUT)]
        with open(SEED_V2, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=SEED_FIELDS)
            w.writeheader()
            for k in keys:
                if k in pred:
                    w.writerow(seed_row(pred[k], human.get(k, {})))
        print(f"wrote {SEED_V2}: {len(keys)} rows, {sum(k in human for k in keys)} with human input carried over.")
        return
    if os.path.exists(SEED_OUT) and not args.force:
        sys.exit(f"{SEED_OUT} exists (may hold your labels); pass --force to overwrite or --refresh to rebuild its rows.")
    rng = random.Random(args.seed)
    quota = {"MATCH": args.n * 2 // 5, "REVIEW": args.n * 1 // 3}
    quota["REJECT"] = args.n - sum(quota.values())
    picked = []
    for decision, k in quota.items():
        pool = [p for p in m["pairs"] if p["auto_decision"] == decision]
        # stratify: round-robin across drugs, with AACT-native-only pairs and OT pairs interleaved, so no single drug dominates
        by = defaultdict(list)
        for p in pool:
            by[(p["drug"], "aact_native" in p["sources"] and "opentargets" not in p["sources"])].append(p)
        for lst in by.values():
            rng.shuffle(lst)
        keys = sorted(by)
        rng.shuffle(keys)
        while len([x for x in picked if x["decision"] == decision]) < k and any(by.values()):
            for key in keys:
                if by[key] and len([x for x in picked if x["decision"] == decision]) < k:
                    picked.append(by[key].pop())
    with open(SEED_OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=SEED_FIELDS)
        w.writeheader()
        for p in sorted(picked, key=lambda x: (x["drug"], x["nct_id"])):
            w.writerow(seed_row(p, human.get((p["chembl_root"], p["nct_id"]), {})))
    print(f"wrote {SEED_OUT}: {Counter(p['decision'] for p in picked)} across {len({p['drug'] for p in picked})} drugs. "
          f"Fill human_label (MATCH/REVIEW/REJECT), save as {LABELS}, then run 'eval'.")


def tier_precision(rows, gold):
    """Share of pipeline-tiered rows the human called MATCH. The strict tier should sit near 1.0."""
    hit = sum(gold[(p["chembl_root"], p["nct_id"])] == "MATCH" for p in rows)
    return dict(labeled=len(rows), human_match=hit, precision=round(hit / len(rows), 3) if rows else None)


def evaluate(args):
    path = args.labels or (LABELS if os.path.exists(LABELS) else SEED_OUT)
    with open(MATCH_OUT, encoding="utf-8") as f:
        pred = {(p["chembl_root"], p["nct_id"]): p for p in json.load(f)["pairs"]}
    lab = [r for r in read_csv(path) if (r.get("human_label") or "").strip().upper() in DECISION_ORDER]
    if not lab:
        sys.exit(f"No human_label values in {path}. Proposed labels are the pipeline's own output, so scoring them would be circular.")
    gold = {(r["chembl_root"], r["nct_id"]): r["human_label"].strip().upper() for r in lab}
    missing = [k for k in gold if k not in pred]
    conf = Counter((gold[k], pred[k]["auto_decision"]) for k in gold if k in pred)
    n = sum(conf.values())
    acc = sum(v for (g, p), v in conf.items() if g == p) / n if n else 0
    fp = sum(v for (g, p), v in conf.items() if p == "MATCH" and g != "MATCH")
    tn = sum(v for (g, p), v in conf.items() if p != "MATCH" and g != "MATCH")
    # Ranking metrics over the labeled pairs only (condensed list): per drug, order labeled pairs by pipeline rank; REJECT ranks last.
    by_drug = defaultdict(list)
    for k, g in gold.items():
        if k in pred:
            by_drug[k[0]].append((pred[k]["rank"] or 10 ** 6, -pred[k]["bm25"], g))
    def at(k):
        ps, rs = [], []
        for lst in by_drug.values():
            rel = sum(g == "MATCH" for *_, g in lst)
            if not rel:
                continue
            top = [g for *_, g in sorted(lst)[:k]]
            ps.append(sum(g == "MATCH" for g in top) / min(k, len(lst)))
            rs.append(sum(g == "MATCH" for g in top) / rel)
        return (round(sum(ps) / len(ps), 3) if ps else None, round(sum(rs) / len(rs), 3) if rs else None, len(ps))
    p10, r10, nd = at(10)
    p50, _, _ = at(50)
    res = dict(labels_file=path, labeled=len(gold), not_in_match_output=len(missing), accuracy_3class=round(acc, 3),
               fpr_match=round(fp / (fp + tn), 3) if fp + tn else None, precision_match=round(conf[("MATCH", "MATCH")] / max(1, sum(v for (g, p), v in conf.items() if p == "MATCH")), 3),
               p_at_10=p10, r_at_10=r10, p_at_50=p50, drugs_with_relevant=nd,
               verified_tier=tier_precision([pred[k] for k in gold if k in pred and pred[k]["auto_tier"]], gold),
               flagship=tier_precision([pred[k] for k in gold if k in pred and pred[k].get("auto_flagship")], gold),
               confusion={f"human={g} pred={p}": v for (g, p), v in sorted(conf.items())})
    print(json.dumps(res, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull")
    p.add_argument("--scope-id", default="MONDO_0018874")
    sub.add_parser("run")
    s = sub.add_parser("seed")
    s.add_argument("--n", type=int, default=150)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--force", action="store_true")
    s.add_argument("--refresh", action="store_true", help=f"rebuild the existing seed's rows with current columns into {os.path.basename(SEED_V2)}")
    e = sub.add_parser("eval")
    e.add_argument("labels", nargs="?")
    a = ap.parse_args()
    {"pull": pull, "run": run, "seed": seed, "eval": evaluate}[a.cmd](a)


if __name__ == "__main__":
    main()
