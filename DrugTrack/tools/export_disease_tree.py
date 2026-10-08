#!/usr/bin/env python
"""
Deterministic disease-tree exporter v2. NO LLM anywhere in this file.

Sources (all read-only, every decision carries a source tag):
  OT   Open Targets bulk parquet (disease ontology, drug molecules, regulatory/label reports)
  CT   Clinical Tracker clinical_tracker.db (indications at the scope node)
  AACT live ctgov schema (trial metadata, arms -> interventions)
  FDA  openFDA drug labels (live, cached) - current US label text

Categorisation layer (see meta.rules in the output):
  * drugs are merged by OT parentId, then by an explicit salt-suffix rule
  * approval evidence is per REGULATOR (US / EU / JP) and per ontology RELATION
    (exact / descendant / ancestor-with-distance) - so "approved at 'myeloid leukemia' by EMA"
    is never confused with "approved for AML by the FDA"
  * approved REGIMENS are parsed from live US label clauses ("in combination with ...")
  * every trial is placed by the CONTENTS of its arms into a regimen (arm labels are unreliable);
    trials with no usable arms go to an explicit audit bucket, never dropped

Run with Clinical Tracker's interpreter:
  "C:/Users/jwrup/Clinical Tracker/.venv/Scripts/python.exe" tools/export_disease_tree.py
"""
import argparse, hashlib, io, json, os, re, shutil, sqlite3, sys, time, urllib.error, urllib.parse, urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fidelity as F

CT_DIR = os.environ.get("CLINICAL_TRACKER_DIR", r"C:\Users\jwrup\Clinical Tracker")   # AACT client + credentials only (never copied)
BRIDGE = os.environ.get("AACT_BRIDGE_DIR", r"C:\Users\jwrup\Clinical Tracker\AACT_bridge")       # every static input file lives here
BRIDGE_IN = os.path.join(BRIDGE, "inputs")
BULK = os.path.join(BRIDGE_IN, "opentargets")
TESTS_IN = os.path.join(BRIDGE_IN, "tests")
REF_DIR = os.path.join(BRIDGE_IN, "reference")
SOURCE_DB = os.path.join(BRIDGE_IN, "source_index.db")   # built by tools/source_index.py
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

STAGE_RANK = {s: i for i, s in enumerate(["UNKNOWN", "EARLY_PHASE_1", "PHASE_1", "PHASE_1_2", "PHASE_2", "PHASE_2_3", "PHASE_3", "APPROVAL"])}
STAGE_ORDER = ["PHASE_3", "PHASE_2_3", "PHASE_2", "PHASE_1_2", "PHASE_1", "EARLY_PHASE_1", "UNKNOWN"]
ACTIVE = ("RECRUITING", "NOT_YET_RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION")
DRUG_LIKE = ("DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT", "GENETIC")
NCT_RE = re.compile(r"^NCT\d{8}$")
REG_TYPES = ("REGULATORY_AGENCY", "DRUG_LABEL")
JURIS = {"DailyMed": "US", "FDA": "US", "EMA": "EU", "EMA Human Drugs": "EU", "PMDA": "JP"}
NEAR_ANCESTOR_MAX = 2       # ancestor terms within this many hops count as "broader term"; farther ones ("neoplasm") are ignored
# US-only approval gate (human criteria, 2026-10-07). Other regulators are reference only. Backbone chemotherapy and
# supportive/conditioning agents are shown only inside regimens of approved drugs, never as approved drugs themselves.
BACKBONE_DRUGS = {"CYTARABINE", "DAUNORUBICIN", "IDARUBICIN", "MITOXANTRONE", "DOXORUBICIN", "THIOGUANINE", "ETOPOSIDE", "FLUDARABINE"}
SUPPORTIVE_DRUGS = {"SARGRAMOSTIM", "FILGRASTIM", "PEGFILGRASTIM", "LENOGRASTIM", "MELPHALAN", "THIOTEPA", "POSACONAZOLE", "METRONIDAZOLE"}
LABEL_CACHE_VERSION = 3   # v3: labels now carry application numbers, UNII and mechanism_of_action text
SALT_RE = re.compile(r"^(?P<base>.+?)\s+(?:di|tri|hemi|sesqui)?(?:hydrochloride|hydrobromide|mesylate|mesilate|fumarate|maleate|sodium|potassium|"
                     r"phosphate|sulfate|sulphate|tartrate|citrate|acetate|besylate|tosylate|succinate|malate|anhydrous|monohydrate|hydrate|dihydrate)$", re.I)
NORM_STOP = re.compile(r"\b(tablets?|capsules?|injection|oral|infusion|monotherapy|regimen|therapy|hydrochloride|hcl|mesylate|mesilate|fumarate|maleate|"
                       r"sodium|free base|low[- ]dose|high[- ]dose|intermediate[- ]dose|standard|induction|consolidation)\b")


def raw(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def norm(s):
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\b\d+(\.\d+)?\s*(mg|mcg|g|ml|iu|%)\b.*", " ", s)
    s = NORM_STOP.sub(" ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def term_in(term, text_lower):
    t = re.sub(r"\s+", " ", (term or "").strip().lower())
    return len(t) >= 4 and re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", text_lower) is not None


def iso_mtime(path):
    return datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).isoformat(timespec="seconds")


# ============================================================================ drug index (OT drug_molecule)
class DrugIndex:
    """Maps free text -> canonical ROOT drug id, deterministically. Ambiguity is refused, never guessed."""

    def __init__(self):
        dm = pq.read_table(os.path.join(BULK, "drug_molecule.parquet"),
                           columns=["id", "name", "synonyms", "tradeNames", "parentId"]).to_pylist()
        self.name = {d["id"]: d["name"] for d in dm}
        self.root_of, self.merge = {}, {}
        for d in dm:
            self.root_of[d["id"]] = d["parentId"] or d["id"]
            self.merge[d["id"]] = "parentId" if d["parentId"] else "self"
        # salt-suffix rule for records OT does not link by parentId (e.g. DAUNORUBICIN HYDROCHLORIDE -> DAUNORUBICIN)
        by_name = {}
        for did, r in self.root_of.items():
            if r == did:
                by_name.setdefault(self.name[did].upper(), did)
        for did in list(self.root_of):
            if self.root_of[did] != did:
                continue
            m = SALT_RE.match(self.name[did])
            if m and m.group("base").upper() in by_name and by_name[m.group("base").upper()] != did:
                self.root_of[did] = by_name[m.group("base").upper()]
                self.merge[did] = "salt-suffix"
        self.members = defaultdict(list)
        for did, r in self.root_of.items():
            self.members[r].append(did)
        self.idx, self.combo = {}, {}
        self.combo_terms = defaultdict(set)   # root -> product names it is a component of (e.g. ASTX727)
        for d in dm:
            root = self.root_of[d["id"]]
            labels = [d["name"]] + [x["label"] for x in (d["synonyms"] or [])] + [x["label"] for x in (d["tradeNames"] or [])]
            for lab in labels:
                m = re.match(r"(.+?)\s+component of\s+(.+)", lab, re.I)
                if m:
                    self.combo.setdefault(raw(m.group(2)), set()).add(root)   # e.g. "cedazuridine component of ASTX727"
                    self.combo_terms[root].add(m.group(2).strip())
                    continue
                for n in {norm(lab), raw(lab)}:
                    if n:
                        self.idx.setdefault(n, set()).add(root)
        self.exact_names = {d["name"].upper() for d in dm}
        self.trade = defaultdict(dict)   # root -> {UPPER trade name: (label, OT source)}
        for d in dm:
            for x in (d["tradeNames"] or []):
                if x["label"] and not re.search(r"component of", x["label"], re.I):
                    self.trade[self.root_of[d["id"]]].setdefault(x["label"].strip().upper(), (x["label"].strip(), x.get("source")))
        self.combo_products = set()   # frozensets of root ids that a US label defines as ONE product (e.g. Vyxeos = daunorubicin + cytarabine)
        self.aliases, self.alias_via = {}, {}   # exact AACT intervention string -> roots, via a sponsor-supplied other name

    def load_aact_aliases(self, aact, names):
        """Sponsor-supplied alternate names (ctgov.intervention_other_names) for strings that did not resolve. Source-tagged."""
        names = sorted(set(names))
        for i in range(0, len(names), 400):
            for r in aact.run_query("""SELECT i.name AS iname, o.name AS other FROM ctgov.interventions i
                JOIN ctgov.intervention_other_names o ON o.intervention_id = i.id WHERE i.name = ANY(%s)""", (names[i:i + 400],), limit=200000):
                kind, ids = self._lookup1(r["other"])
                if kind in ("one", "combo") and r["iname"] not in self.aliases:
                    self.aliases[r["iname"]] = set(ids)
                    self.alias_via[r["iname"]] = r["other"]

    def root_name(self, root):
        return self.name.get(root, root)

    def arm_terms(self, root):
        """Strings that legitimately name this drug in free text: merged record names and Open Targets trade names."""
        terms = {self.name.get(m, "") for m in self.members.get(root, [])[:6]} | {self.name.get(root, "")}
        terms |= {lab for lab, _ in self.trade.get(root, {}).values()}
        terms |= self.combo_terms.get(root, set())
        return {t for t in terms if len(t) >= 4}

    def _names_of(self, root):
        """Every name a merged drug is legitimately known by: each member record's name, plus that name minus a trailing salt word."""
        out = set()
        for m in self.members[root]:
            nm = self.name.get(m, "")
            out.add(raw(nm))
            sm = SALT_RE.match(nm)
            if sm:
                out.add(raw(sm.group("base")))
        return out

    def _lookup1(self, text):
        found = set()
        for cand in [text] + re.findall(r"\(([^)]+)\)", text or ""):
            for n in (norm(cand), raw(cand)):
                if n in self.combo:
                    return "combo", set(self.combo[n])
                if n in self.idx:
                    found |= self.idx[n]
            if found:
                break
        if len(found) > 1:
            # Open Targets/ChEMBL synonym lists contain noise (e.g. 'cyclophosphamide' is also a synonym on LORNOXICAM).
            # Tie-break ONLY on an exact match against a member record's own name - never on similarity.
            base_raw, base_norm = raw(re.sub(r"\(.*?\)", " ", text or "")), norm(text)
            exact = [r for r in found if base_raw in self._names_of(r) or base_norm in self._names_of(r)]
            if len(exact) == 1:
                return "one", set(exact)
            # Ambiguity AMONG THE COMPONENTS of one label-defined product (CPX-351 / Vyxeos -> daunorubicin + cytarabine):
            # corroborated independently by Open Targets' own search, which returns exactly those two drugs.
            if frozenset(found) in self.combo_products:
                return "combo", set(found)
        if not found and text in self.aliases:
            ids = self.aliases[text]
            return ("one" if len(ids) == 1 else "combo"), set(ids)
        return ("one" if len(found) == 1 else "amb" if found else "none"), found

    def lookup(self, text):
        """-> (kind, roots)  kind in one | combo | amb | none.  'combo' also covers 'X and Y' / 'X + Y' strings
        whose components ALL resolve uniquely (partial resolution is refused)."""
        kind, ids = self._lookup1(text)
        if kind in ("one", "combo"):
            return kind, ids
        parts = [p for p in re.split(r"\s*(?:\+|/|,|;|\bplus\b|\band\b|\bwith\b)\s*", text or "", flags=re.I) if p.strip()]
        if len(parts) >= 2:
            res = [self._lookup1(p) for p in parts]
            if all(k in ("one", "combo") for k, _ in res):
                return "combo", set().union(*[i for _, i in res])
        return kind, ids

    DESCRIPTOR_RE = re.compile(r"\b(chemotherapy|chemo|regimen|therapy|standard|induction|consolidation|conditioning|treatment|care|protocol|cohort|arm|monotherapy|salvage|intensive)\b", re.I)
    NONDRUG_RE = re.compile(r"\b(cell|car[- ]?t|infusion|transplant|lymphocyte|leukocyte|vaccine|irradiation|radiation|biospecimen|procedure|graft|hsct|tbi)\b", re.I)

    def classify_unresolved(self, text):
        kind, ids = self._lookup1(text)
        if kind == "amb" and len(ids) >= 3:
            return "drug-class term (several distinct drugs, e.g. G-CSF) - not a single drug"
        if kind == "amb":
            return "ambiguous label - two Open Targets drugs share it (refused, not guessed)"
        if self.NONDRUG_RE.search(text) or re.search(r"\bCAR\b", text):
            return "cell therapy / procedure / non-drug"
        if self.DESCRIPTOR_RE.search(text):
            return "regimen descriptor (needs a curated dictionary)"
        if re.match(r"^[A-Za-z]{1,6}[- ]?\d{2,}", text.strip()):
            return "internal compound code (no Open Targets synonym)"
        return "no Open Targets match (novel agent, name variant or typo)"


# ============================================================================ disease ontology (OT disease)
class Ontology:
    def __init__(self, scope_id):
        D = pq.read_table(os.path.join(BULK, "disease.parquet"), columns=["id", "name", "parents", "descendants"]).to_pylist()
        self.name = {d["id"]: d["name"] for d in D}
        parents = {d["id"]: d["parents"] or [] for d in D}
        self.scope = scope_id
        self.descendants = set(next((d["descendants"] or [] for d in D if d["id"] == scope_id), []))
        self.ancestor_dist, frontier = {scope_id: 0}, [scope_id]
        while frontier:
            nxt = []
            for x in frontier:
                for p in parents.get(x, []):
                    if p not in self.ancestor_dist:
                        self.ancestor_dist[p] = self.ancestor_dist[x] + 1
                        nxt.append(p)
            frontier = nxt

    def relation(self, disease_id):
        if disease_id == self.scope:
            return ("exact", 0)
        if disease_id in self.descendants:
            return ("descendant", None)
        if disease_id in self.ancestor_dist:
            return ("ancestor", self.ancestor_dist[disease_id])
        return None


def load_regulatory(di, onto):
    """OT regulatory/label reports -> per-root approval evidence, tagged by regulator and ontology relation."""
    tbl = pq.read_table(os.path.join(BULK, "clinical_report.parquet"), columns=["id", "type", "source", "clinicalStage", "drugs", "diseases"])
    mask = pc.and_(pc.is_in(tbl["type"], value_set=pa.array(list(REG_TYPES))), pc.equal(tbl["clinicalStage"], "APPROVAL"))
    ev = defaultdict(lambda: defaultdict(int))
    for r in tbl.filter(mask).to_pylist():
        jur = JURIS.get(r["source"], r["source"])
        roots = {di.root_of.get(d["drugId"], d["drugId"]) for d in (r["drugs"] or []) if d["drugId"]}
        for dis in (r["diseases"] or []):
            rel = onto.relation(dis["diseaseId"]) if dis["diseaseId"] else None
            if rel is None:
                continue
            for root in roots:
                ev[root][(jur, r["source"], dis["diseaseId"], rel[0], rel[1])] += 1
    out = {}
    for root, d in ev.items():
        out[root] = sorted(
            [dict(jurisdiction=k[0], source=k[1], disease_id=k[2], disease=onto.name.get(k[2], k[2]), relation=k[3],
                  distance=k[4], reports=n) for k, n in d.items()],
            key=lambda e: (e["relation"] != "exact", e["jurisdiction"], e["disease"]))
    return out


# ============================================================================ EMA authorised indications (EPAR medicines report)
EMA_URL = "https://www.ema.europa.eu/en/documents/report/medicines-output-medicines_json-report_en.json"
EMA_PATH = os.path.join(BRIDGE_IN, "ema", "medicines.json")
EMA_MAX_AGE_S = 7 * 86400


def substance_key(s):
    """Normalised substance name with a trailing salt word removed ('histamine dihydrochloride' -> 'histamine')."""
    s = raw(s)
    m = SALT_RE.match(s)
    return m.group("base") if m else s


def load_ema(refresh=False):
    """EMA's public medicines report (one JSON with every EPAR) -> substance key -> authorised human products.
    Cached under BRIDGE_IN/ema; re-downloaded when older than EMA_MAX_AGE_S or on --refresh-ema. Returns (index, provenance)."""
    if refresh or not os.path.exists(EMA_PATH) or time.time() - os.path.getmtime(EMA_PATH) > EMA_MAX_AGE_S:
        try:
            req = urllib.request.Request(EMA_URL, headers={"User-Agent": "clinical-tracker"})
            with urllib.request.urlopen(req, timeout=120) as r:
                body = r.read()
            json.loads(body)
            os.makedirs(os.path.dirname(EMA_PATH), exist_ok=True)
            with open(EMA_PATH, "wb") as f:
                f.write(body)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            if not os.path.exists(EMA_PATH):
                print(f"[ema] download failed and no cache: {e}", flush=True)
                return {}, dict(source=EMA_URL, status=f"unavailable: {e}")
            print(f"[ema] download failed, using cache: {e}", flush=True)
    with open(EMA_PATH, encoding="utf-8") as f:
        doc = json.load(f)
    idx = defaultdict(list)
    for r in doc.get("data", []):
        if r.get("category") != "Human" or r.get("medicine_status") != "Authorised":
            continue
        subs = re.split(r"\s*[;/]\s*|\s*,\s*|\s+and\s+", (r.get("international_non_proprietary_name_common_name") or r.get("active_substance") or ""))
        keys = {substance_key(s) for s in subs if s.strip()}
        prod = dict(medicine=r.get("name_of_medicine"), url=r.get("medicine_url"), substances=sorted(keys),
                    authorised=r.get("marketing_authorisation_date"), indication=re.sub(r"\s+", " ", r.get("therapeutic_indication") or "").strip())
        for k in keys:
            idx[k].append(prod)
    meta = doc.get("meta", {})
    return idx, dict(source=EMA_URL, cache=os.path.relpath(EMA_PATH, BRIDGE), report_timestamp=meta.get("timestamp"),
                     records=meta.get("total_records"), file_mtime=iso_mtime(EMA_PATH), status="ok")


SUPPORTIVE_TEXT_RE = re.compile(r"neutropeni|neutrophil|febrile|fever|mobiliz|myelosuppressive", re.I)
# Therapeutic role, read from the indication sentences that name the scope disease (US label + EMA). First match wins.
ROLE_RULES = [
    ("conditioning", re.compile(r"conditioning|prior to (?:haematopoietic|hematopoietic|stem cell)|before (?:haematopoietic|hematopoietic)", re.I)),
    ("supportive care", SUPPORTIVE_TEXT_RE),
    ("maintenance", re.compile(r"maintenance|in (?:first )?(?:complete )?remission|continued treatment", re.I)),
]
CONCOMITANT_RE = re.compile(r"concomitantly treated with\s+(.+?)(?:[.;(]|$)", re.I)


def therapeutic_role(sentences):
    """[(source, sentence)] naming the scope disease -> role tag: one of conditioning, supportive care, maintenance, combination,
    monotherapy (plus 'or combination' when the text allows both). The partner text is quoted, never inferred."""
    if not sentences:
        return None
    roles, partners, mono = [], [], False
    for _, s in sentences:
        role = next((r for r, rx in ROLE_RULES if rx.search(s)), None)
        m = PARTNER_RE.search(s) or CONCOMITANT_RE.search(s)
        if m:
            # Drop the trailing verb phrase ("... venetoclax is indicated") so only the partner names remain.
            partners.append(re.sub(r"\s+(?:is|are) indicated\b.*$", "", m.group(1)).strip(" ,")[:80])
        if re.search(r"monotherapy|as a single agent", s, re.I) or not m:
            mono = True
        if role:
            roles.append(role)
    role = Counter(roles).most_common(1)[0][0] if roles else None
    combo_only = bool(partners) and not mono
    kind = "combination required" if combo_only else ("monotherapy or combination" if partners else "monotherapy")
    partners = sorted(set(partners))
    tag = (f"{role}, " if role else "") + kind + (f" (with {'; '.join(partners[:3])})" if partners else "")
    return dict(tag=tag, setting=role, combination=kind, partners=partners or None, basis=sorted({src for src, _ in sentences}))


# ============================================================================ mechanism of action (OT drug_mechanism_of_action)
# Precision-oncology target classes worth their own bucket in the AML landscape, checked in this priority
# order (first match wins - e.g. quizartinib/midostaurin hit several off-target kinases but are clinically
# the FLT3-inhibitor class, so FLT3 is checked first). Anything not in this list (DNA methyltransferase,
# topoisomerase, DNA polymerase, retinoic acid receptor, GM-CSF receptor, etc.) is backbone/broad-mechanism
# chemo, not a mutation- or antigen-restricted precision target, and stays General.
MECHANISM_GROUP_RULES = [
    (re.compile(r"FLT3", re.I), "FLT3"),
    (re.compile(r"Isocitrate dehydrogenase.*cytoplasmic", re.I), "IDH1"),
    (re.compile(r"Isocitrate dehydrogenase.*mitochondrial", re.I), "IDH2"),
    # Menin inhibitors as a class target both NPM1-mutant and KMT2A-rearranged AML (revumenib is approved
    # for both, via two separate FDA pages); grouped under "NPM1" to match pr_expectations.json's claim
    # vocabulary so an investigational Menin inhibitor with no approval yet still lands beside revumenib/
    # ziftomenib instead of a same-meaning-but-differently-spelled group of its own.
    (re.compile(r"\bMenin\b", re.I), "NPM1"),
    (re.compile(r"Apoptosis regulator Bcl-2", re.I), "BCL2"),
    (re.compile(r"Myeloid cell surface antigen CD33", re.I), "CD33"),
    (re.compile(r"Interleukin-3 receptor subunit alpha", re.I), "CD123"),
    (re.compile(r"Protein smoothened", re.I), "Hedgehog (SMO)"),
]


def load_mechanisms(di):
    """Drug -> molecular target(s), from OT's drug_mechanism_of_action dataset (not part of the 4 datasets
    Clinical Tracker's bulk.py originally fetched - hand-staged here the same way, see BULK). This is what
    lets an INVESTIGATIONAL drug (no approval, so no pr_expectations.json claim) still land in a real
    mutation/target bucket instead of defaulting to General for lack of any other signal."""
    path = os.path.join(BULK, "drug_mechanism_of_action.parquet")
    if not os.path.exists(path):
        return {}, {}, {}
    tbl = pq.read_table(path, columns=["chemblIds", "targetName", "actionType", "targets"]).to_pylist()
    targets_by_root, group_by_root, ensembl_by_root = defaultdict(set), {}, defaultdict(set)
    for row in tbl:
        for cid in row["chemblIds"] or []:
            root = di.root_of.get(cid)
            if root is None:
                continue
            targets_by_root[root].add((row["targetName"], row["actionType"]))
            ensembl_by_root[root].update(row["targets"] or [])
    for root, hits in targets_by_root.items():
        names = {t for t, _ in hits}
        for rx, label in MECHANISM_GROUP_RULES:
            if any(rx.search(n) for n in names):
                group_by_root[root] = label
                break
    return targets_by_root, group_by_root, ensembl_by_root


# ============================================================================ openFDA labels (live, cached)
def fda_request(url, api_key, timeout):
    """GET an openFDA URL, adding the API key to the REQUEST only. Callers store the key-less url in caches so the
    key never lands in a cache file or an output."""
    with urllib.request.urlopen(url + (f"&api_key={api_key}" if api_key else ""), timeout=timeout) as r:
        return json.load(r)


class LabelChecker:
    """openFDA drug labels (DailyMed SPL). Looked up by UNII when the source index supplies one (an identifier: exact,
    name-independent, also finds combination-product labels that list the ingredient), else by generic/substance name."""

    def __init__(self, cache_path, api_key=None):
        self.cache_path = cache_path
        self.api_key = api_key
        self.cache = json.load(open(cache_path, encoding="utf-8")) if os.path.exists(cache_path) else {}
        self.rate_limited, self.network_calls = False, 0

    def fetch_unii(self, uniis):
        uniis = sorted(set(uniis))[:3]
        terms = "+OR+".join(f"openfda.unii:%22{urllib.parse.quote(u, safe='')}%22" for u in uniis)
        return self._query("UNII:" + "|".join(uniis), f"https://api.fda.gov/drug/label.json?search={terms}&limit=8")

    def fetch(self, name):
        q = urllib.parse.quote(name, safe="")
        return self._query(name.upper(), "https://api.fda.gov/drug/label.json?search="
                           f"openfda.generic_name:%22{q}%22+openfda.substance_name:%22{q}%22&limit=5")

    def _query(self, key, url):
        c = self.cache.get(key)
        if c and c.get("v") == LABEL_CACHE_VERSION:
            return c
        if self.rate_limited:
            return {"status": "not_checked", "reason": "openFDA rate limit reached earlier in this run"}
        rec = {"v": LABEL_CACHE_VERSION, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "url": url}
        try:
            self.network_calls += 1
            data = fda_request(url, self.api_key, 25)
            labels = []
            for res in data.get("results", []):
                ofda = res.get("openfda", {})
                labels.append(dict(brand=(ofda.get("brand_name") or [None])[0], set_id=res.get("set_id"),
                                   effective_time=res.get("effective_time"), substances=ofda.get("substance_name") or [],
                                   application_numbers=ofda.get("application_number") or [], unii=ofda.get("unii") or [],
                                   indications=" ".join(res.get("indications_and_usage", []))[:5000],
                                   moa=" ".join(res.get("mechanism_of_action", []))[:4000]))
            rec.update(status="found" if labels else "no_label", labels=labels)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                rec.update(status="no_label", labels=[])
            elif e.code == 429:
                self.rate_limited = True
                return {"status": "not_checked", "reason": "openFDA HTTP 429"}
            else:
                return {"status": "not_checked", "reason": f"openFDA HTTP {e.code}"}
        except Exception as e:
            return {"status": "not_checked", "reason": f"{type(e).__name__}: {e}"}
        self.cache[key] = rec
        time.sleep(0.25 if self.api_key else 0.6)
        return rec

    def save(self):
        json.dump(self.cache, open(self.cache_path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)


APPROVAL_CACHE_VERSION = 2   # v2: applications carry their Drugs@FDA document URLs (approval letters, labels, reviews)


def iso_day(s):
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if s and len(s) == 8 else s


class ApprovalChecker:
    """Live Drugs@FDA (openFDA drugsfda): US NDA/BLA original-approval dates + brand names. US only; says nothing about WHICH indication
    on its own - a label's application number (see LabelChecker) is what ties an application record to an indication."""

    def __init__(self, cache_path, api_key=None):
        self.api_key = api_key
        self.cache_path = cache_path
        self.cache = json.load(open(cache_path, encoding="utf-8")) if os.path.exists(cache_path) else {}
        self.rate_limited, self.network_calls = False, 0

    def fetch(self, name):
        key = name.upper()
        c = self.cache.get(key)
        if c and c.get("v") == APPROVAL_CACHE_VERSION:
            return c
        if self.rate_limited:
            return {"status": "not_checked", "reason": "openFDA rate limit reached earlier in this run"}
        q = urllib.parse.quote(key, safe="")
        url = f"https://api.fda.gov/drug/drugsfda.json?search=products.active_ingredients.name:%22{q}%22&limit=100"
        rec = {"v": APPROVAL_CACHE_VERSION, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "url": url}
        try:
            self.network_calls += 1
            data = fda_request(url, self.api_key, 25)
            apps = []
            for res in data.get("results", []):
                appn = res.get("application_number", "")
                if not appn.startswith(("NDA", "BLA")):
                    continue
                subs = res.get("submissions", [])
                ap = lambda s: s.get("submission_status") == "AP" and s.get("submission_status_date")
                orig = sorted(s["submission_status_date"] for s in subs if s.get("submission_type") == "ORIG" and ap(s))
                eff = sorted({s["submission_status_date"] for s in subs if s.get("submission_type") == "SUPPL" and s.get("submission_class_code") == "EFFICACY" and ap(s)})
                apps.append(dict(application=appn, sponsor=res.get("sponsor_name"),
                                 brands=sorted({p["brand_name"] for p in res.get("products", []) if p.get("brand_name")}),
                                 ingredients=sorted({a["name"].upper() for p in res.get("products", []) for a in p.get("active_ingredients", [])}),
                                 first_approval=iso_day(orig[0]) if orig else None, efficacy_supplements=[iso_day(x) for x in eff],
                                 docs=[dict(sub=sb.get("submission_type"), cls=sb.get("submission_class_code"), date=iso_day(sb.get("submission_status_date")),
                                            type=x.get("type"), doc_date=iso_day(x.get("date")), url=x.get("url"))
                                       for sb in subs if ap(sb) and (sb.get("submission_type") == "ORIG" or sb.get("submission_class_code") == "EFFICACY")
                                       for x in (sb.get("application_docs") or []) if x.get("type") in ("Letter", "Label", "Review") and x.get("url")]))
            rec.update(status="found" if apps else "none", apps=apps)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                rec.update(status="none", apps=[])
            elif e.code == 429:
                self.rate_limited = True
                return {"status": "not_checked", "reason": "openFDA HTTP 429"}
            else:
                return {"status": "not_checked", "reason": f"openFDA HTTP {e.code}"}
        except Exception as e:
            return {"status": "not_checked", "reason": f"{type(e).__name__}: {e}"}
        self.cache[key] = rec
        time.sleep(0.25 if self.api_key else 0.6)
        return rec

    def summarize(self, names):
        names = list(dict.fromkeys(n.upper() for n in names))[:4]
        apps, unchecked = {}, False
        for n in names:
            rec = self.fetch(n)
            if rec.get("status") == "not_checked":
                unchecked = True
                continue
            for a in rec.get("apps", []):
                apps.setdefault(a["application"], a)
        brands = []
        for a in apps.values():
            for b in a["brands"]:
                if not any(b.upper() in i or i in b.upper() for i in a["ingredients"]):   # 'DECITABINE' as a brand name is just the generic
                    brands.append(dict(brand=b.title(), application=a["application"], sponsor=a["sponsor"], first_approval=a["first_approval"],
                                       ingredients=a["ingredients"], source="Drugs@FDA (live)"))
        dated = sorted((a["first_approval"], a["application"], (a["brands"] or [None])[0]) for a in apps.values() if a["first_approval"])
        return dict(status="not_checked" if unchecked and not apps else ("found" if apps else "none"), queried=names, brands=brands,
                    applications=[dict(application=a["application"], sponsor=a["sponsor"], brands=a["brands"], first_approval=a["first_approval"],
                                       efficacy_supplements=a["efficacy_supplements"], docs=a.get("docs", [])) for a in apps.values()],
                    first_us_approval=dict(date=dated[0][0], application=dated[0][1], product=dated[0][2]) if dated else None,
                    efficacy_supplements={a["application"]: a["efficacy_supplements"] for a in apps.values() if a["efficacy_supplements"]})

    def save(self):
        json.dump(self.cache, open(self.cache_path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)


class LetterChecker:
    """FDA approval letters: the regulator's own dated action document, linked from Drugs@FDA (application_docs, type 'Letter'; the
    URLs come from the database, none are typed here). Each PDF is fetched, text-extracted and searched with the ontology-derived
    scope terms - keyword lookup, no inference. Only a flag, the matched term and a short snippet are cached (never the full text).
    Scanned pre-2000s letters have no text layer and are reported as no_text, not guessed."""

    def __init__(self, cache_path):
        self.cache_path = cache_path
        self.cache = json.load(open(cache_path, encoding="utf-8")) if os.path.exists(cache_path) else {}
        self.network_calls = 0

    def check(self, url, scope_id, scope_re):
        key = f"{scope_id}|{url}"
        c = self.cache.get(key)
        if c and c.get("v") == 1 and c.get("status") in ("text", "no_text"):
            return c
        rec = {"v": 1, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:
            from pypdf import PdfReader
            self.network_calls += 1
            # Drugs@FDA's own JSON is not always a valid URL: a letter covering two submissions together (e.g.
            # venetoclax "208573Orig1s020, s021ltr.pdf") comes back with a literal space and comma, which urllib
            # rejects outright. unquote-then-requote is idempotent for URLs that were already properly encoded.
            parts = urllib.parse.urlsplit(url.replace("http://", "https://", 1))
            fixed = parts._replace(path=urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/:"))
            req = urllib.request.Request(urllib.parse.urlunsplit(fixed), headers={"User-Agent": "Mozilla/5.0 (DrugTrack letter check)"})
            with urllib.request.urlopen(req, timeout=45) as r:
                blob = r.read()
            txt = re.sub(r"\s+", " ", " ".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(blob)).pages[:5]))
            m = scope_re.search(txt)
            rec.update(status="text" if len(txt) > 200 else "no_text", chars=len(txt), mentions=bool(m), term=m.group(0) if m else None,
                       snippet=txt[max(0, m.start() - 110):m.end() + 170] if m else None)
        except Exception as e:
            rec.update(status="fetch_failed", reason=f"{type(e).__name__}: {e}"[:140], mentions=False)
        self.cache[key] = rec
        time.sleep(0.3)
        return rec

    def save(self):
        json.dump(self.cache, open(self.cache_path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)


FDA_DATE_RE = re.compile(r"On\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4}),?\s+the\s+(?:U\.S\.\s+)?(?:Food and Drug Administration|FDA)\s+((?:approved|granted)[^.]{0,400}\.)", re.I)
# Older FDA press-announcement pages (e.g. enasidenib, 2017) don't use the "On <date>, the FDA approved ..."
# sentence at all - they carry the date only in a "For Immediate Release: <date>" header. Found live: the
# first run of this cross-check silently returned approval_date=None for every such page until this fallback
# was added, which would have made every pr_expectations.json date claim against an old-style page unverifiable.
FDA_PRESS_DATE_RE = re.compile(r"For Immediate Release:\s*([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", re.I)
REFERENCE_CACHE_VERSION = 3   # bump whenever _fetch's parsing changes - pages_cache.json stores the PARSED fields, not raw text, so a regex fix alone never re-parses a cached page


class ReferenceSources:
    """Public approval pages / press releases fetched raw (HTTP GET, tags stripped) and keyword-searched. Independent of every other source."""

    def __init__(self, ref_dir):
        self.dir = ref_dir
        self.spec = json.load(open(os.path.join(ref_dir, "reference_sources.json"), encoding="utf-8"))["sources"]
        self.cache_path = os.path.join(ref_dir, "pages_cache.json")
        self.cache = json.load(open(self.cache_path, encoding="utf-8")) if os.path.exists(self.cache_path) else {}
        self.network_calls = 0
        self.pages = {}

    def _fetch(self, s):
        req = urllib.request.Request(s["url"], headers={"User-Agent": "Mozilla/5.0 (DrugTrack reference check)"})
        rec = {"v": REFERENCE_CACHE_VERSION, "url": s["url"], "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:
            self.network_calls += 1
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode("utf-8", "ignore")
                rec["http"] = r.status
            t = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
            t = re.sub(r"\s+", " ", __import__("html").unescape(re.sub(r"<[^>]+>", " ", t))).strip()
            # FDA's site returns HTTP 200 for a removed page and serves its own "Page Not Found" template instead of a
            # real 404 - found live on fda_onureg, which looked "fetched" with no date until this check was added, when
            # it was actually as dead as the other 3 pages in this set.
            if re.match(r"^Page Not Found \|", t):
                rec.update(status="not_fetched", reason="HTTP 200 but FDA served its 'Page Not Found' template (soft 404)",
                           text="", approval_date=None, approval_sentence=None)
                time.sleep(0.5)
                return rec
            m = FDA_DATE_RE.search(t)
            if m:
                rec.update(status="fetched", chars=len(t), text=t[:80000],
                           approval_date=datetime.strptime(re.sub(r"\s+", " ", m.group(1)), "%B %d, %Y").date().isoformat(),
                           approval_sentence=m.group(0)[:520])
            else:
                m2 = FDA_PRESS_DATE_RE.search(t)
                rec.update(status="fetched", chars=len(t), text=t[:80000],
                           approval_date=datetime.strptime(m2.group(1), "%B %d, %Y").date().isoformat() if m2 else None,
                           approval_sentence=(f"FDA press release dated {m2.group(1)} (release-header date; no 'On <date>, the FDA approved' clause on this page)" if m2 else None))
        except Exception as e:
            rec.update(status="not_fetched", reason=f"{type(e).__name__}: {e}"[:160], text="", approval_date=None, approval_sentence=None)
        time.sleep(0.5)
        return rec

    def load(self):
        for s in self.spec:
            c = self.cache.get(s["id"])
            if not (c and c.get("v") == REFERENCE_CACHE_VERSION and c.get("url") == s["url"] and c.get("status") == "fetched"):
                c = self.cache[s["id"]] = self._fetch(s)
            self.pages[s["id"]] = dict(s, **{k: c.get(k) for k in ("status", "chars", "text", "approval_date", "approval_sentence", "fetched_at", "reason")})
        json.dump(self.cache, open(self.cache_path, "w", encoding="utf-8"), ensure_ascii=False)

    def for_drug(self, root_name):
        return [p for p in self.pages.values() if root_name.upper() in [d.upper() for d in p["drugs"]]]

    @staticmethod
    def count(page, terms):
        t = (page.get("text") or "").lower()
        return sum(len(re.findall(r"(?<![a-z0-9])" + re.escape(x.lower()) + r"(?![a-z0-9])", t)) for x in terms if len(x) >= 4)


SETTING_TAGS = [(r"newly diagnosed", "newly diagnosed"), (r"relapsed or refractory|relapsed/refractory", "relapsed/refractory"),
                (r"complete remission|maintenance|continued treatment", "post-remission / maintenance"), (r"\bFLT3", "FLT3"),
                (r"\bIDH1", "IDH1"), (r"\bIDH2", "IDH2"), (r"NPM1", "NPM1"), (r"KMT2A", "KMT2A"),
                (r"75 years", "age >=75 / unfit for intensive"), (r"\binduction\b", "induction"), (r"\bconsolidation\b", "consolidation"),
                (r"\bpediatric\b", "pediatric")]
PARTNER_RE = re.compile(r"in combination with\s+(.+?)(?:\s+for\s+(?:the\s+)?treatment|\s+for\s+adult|\s+in\s+adult|\s+in\s+patients|[.;]|$)", re.I)

# Quick search tags. Deterministic regex over label/EMA indication sentences (approved) or a trial's title, AACT conditions,
# keywords and the INCLUSION part of its eligibility criteria (investigated). Tags are AML-native settings plus biomarkers;
# a line of therapy is only inferred from the setting and says so.
UNFIT_RE = re.compile(r"(?:ineligible|not\s+(?:be\s+)?(?:eligible|candidates?|suitable|fit|appropriate))\s+for\s+(?:\w+\s+){0,3}?(?:intensive|induction|standard)"
                      r"|unfit\s+for|\bunfit\b|non-?intensive|less[- ]intensive|low[- ]intensity|(?:≥|>=|=>)\s*75|(?:aged?\s+)?75\s+years\s+(?:or|and)\s+older"
                      r"|at\s+least\s+75|older\s+than\s+74|preclude\s+(?:the\s+)?(?:use\s+of\s+)?intensive", re.I)
FIT_RE = re.compile(r"(?<!in)eligible\s+for\s+(?:\w+\s+){0,2}?intensive|fit\s+for\s+intensive|intensive\s+(?:induction|chemotherapy)|standard\s+(?:\w+\s+){0,3}?induction"
                    r"|7\s*\+\s*3|cytarabine\s+and\s+daunorubicin|daunorubicin\s+and\s+cytarabine", re.I)
QUICK_TAG_RULES = [
    ("newly diagnosed", r"newly[- ]diagnosed|previously\s+untreated|untreated\s+(?:acute|AML)|treatment[- ]na[iï]ve|\bde\s+novo\b|first[- ]line|front[- ]?line"),
    ("relapsed/refractory", r"relapsed|refractory|\bR/R\b|salvage|second[- ]line|first\s+relapse|in\s+relapse"),
    ("maintenance", r"maintenance|continued\s+treatment|post[- ]remission\s+therapy"),
    ("consolidation", r"consolidation"),
    ("post-HSCT", r"(?:after|following|post)[- ](?:allogeneic\s+)?(?:HSCT|SCT|hematopoietic|haematopoietic|stem\s+cell|transplant|allo)|post[- ]?transplant"),
    ("conditioning", r"conditioning\s+(?:regimen|therapy|chemotherapy)|myeloablative|reduced[- ]intensity\s+conditioning"),
    ("supportive care", r"febrile\s+neutropenia|fungal\s+infection|antiemetic|mucositis|tumou?r\s+lysis"),
    ("pediatric", r"pediatric|paediatric|\bchild(?:ren|hood)\b|\binfants?\b|adolescent"),
    ("APL", r"acute\s+promyelocytic|\bAPL\b|PML[-/:]?RARA|t\(15;17\)"),
]
# Biomarker hits are dropped when the text says the marker is absent (e.g. "FLT3-ITD negative", "without IDH1 mutation").
BIOMARKERS = [("FLT3", r"\bFLT-?3"), ("IDH1", r"\bIDH-?1"), ("IDH2", r"\bIDH-?2"), ("NPM1", r"\bNPM-?1"), ("KMT2A", r"KMT2A|\bMLL(?:-r\b|[- ]rearrange)"),
              ("CD33", r"\bCD-?33"), ("TP53", r"\bTP53|\bp53\b")]
BIOMARKER_NEG_AFTER = re.compile(r"^[\w\s/-]{0,25}?(?:negative|wild[- ]?type|\bwt\b|unmutated|non-?mutated)", re.I)
BIOMARKER_NEG_BEFORE = re.compile(r"(?:without|absence\s+of|\bno|non-|negative\s+for)\s*[\w/-]{0,10}\s*$", re.I)
# Setting hits are dropped when inclusion text carves the population out (e.g. "AML other than APL", "non-APL", "APL is excluded").
SETTING_NEG_BEFORE = re.compile(r"(?:except(?:\s+for)?|excluding|exclusion\s+of|other\s+than|with\s+the\s+exception\s+of|\bnot|\bno|non-?|but\s+not)"
                                r"\s*(?:\(\s*)?(?:\w+\s+){0,2}$", re.I)
SETTING_NEG_AFTER = re.compile(r"^[^.;,]{0,25}?\b(?:(?:is|are|will\s+be|must\s+be)\s+(?:not\s+eligible|ineligible|excluded)|excluded|not\s+(?:eligible|allowed|permitted))", re.I)
LINE_INFERRED = {"newly diagnosed": "1L / first line (inferred)", "relapsed/refractory": "2L+ / second line (inferred)"}
TAG_ORDER = ["newly diagnosed", "ND fit (intensive)", "unfit for intensive (non-intensive)", "relapsed/refractory", "maintenance", "consolidation", "post-HSCT",
             "conditioning", "supportive care", "pediatric", "APL"] + [b for b, _ in BIOMARKERS] + list(LINE_INFERRED.values())


def quick_tags(text):
    """Text -> sorted list of quick tags. Fit/unfit are judged per sentence so 'ineligible for intensive chemotherapy' never counts as fit."""
    text = re.sub(r"\s+", " ", text or "")
    tags = {t for t, rx in QUICK_TAG_RULES
            if any(not SETTING_NEG_BEFORE.search(text[max(0, m.start() - 30):m.start()]) and not SETTING_NEG_AFTER.search(text[m.end():m.end() + 60])
                   for m in re.finditer(rx, text, re.I))}
    for name, rx in BIOMARKERS:
        if any(not BIOMARKER_NEG_AFTER.search(text[m.end():m.end() + 40]) and not BIOMARKER_NEG_BEFORE.search(text[max(0, m.start() - 25):m.start()])
               for m in re.finditer(rx, text, re.I)):
            tags.add(name)
    unfit = fit = False
    for s in re.split(r"(?<=[.;])\s+|\s\*\s|\s-\s", text):
        if UNFIT_RE.search(s):
            unfit = True
        elif FIT_RE.search(s):
            fit = True
    if unfit:
        tags.add("unfit for intensive (non-intensive)")
    if fit and "newly diagnosed" in tags:
        tags.add("ND fit (intensive)")
    tags |= {LINE_INFERRED[t] for t in list(tags) if t in LINE_INFERRED}
    return sorted(tags, key=TAG_ORDER.index)


def inclusion_part(criteria):
    """Eligibility criteria text -> the inclusion section only (exclusion text names the excluded population, so it must not tag)."""
    t = criteria or ""
    m = re.search(r"exclusion\s+criteria", t, re.I)
    return t[:m.start()] if m else t


def parse_label_regimens(rec, scope_regex, di, fallback_root):
    """Live US label text -> approved regimens for the scope disease. Deterministic regex; the sentence is kept as evidence."""
    out, mentions, treatment, products = [], False, False, set()
    for lab in rec.get("labels", []) if rec.get("status") == "found" else []:
        text = re.sub(r"\s+", " ", lab["indications"])
        base = set()
        for s in lab.get("substances", []):
            k, ids = di.lookup(s)
            if k in ("one", "combo"):
                base |= ids
        base = base or {fallback_root}
        if len(base) >= 2:
            products.add(frozenset(base))       # a single label product with several active substances
        for sent in re.split(r"(?<=[.;])\s+|\(\s*\d+(?:\.\d+)?\s*\)", text):
            if not re.search(scope_regex, sent, re.I):
                continue
            mentions = True
            if SUPPORTIVE_TEXT_RE.search(sent):
                # label names the disease only as the setting for supportive care (e.g. G-CSF after induction chemo)
                out.append(dict(brand=lab["brand"], set_id=lab["set_id"], effective_time=lab["effective_time"], sentence=sent.strip()[:420],
                                application_numbers=lab.get("application_numbers", []),
                                roots=None, parse="supportive-care clause - excluded from approved regimens", role="supportive_care", setting=[]))
                continue
            treatment = True
            tags = [t for rx, t in SETTING_TAGS if re.search(rx, sent, re.I)]
            ev = dict(brand=lab["brand"], set_id=lab["set_id"], effective_time=lab["effective_time"], sentence=sent.strip()[:420],
                      application_numbers=lab.get("application_numbers", []), role="treatment", setting=tags)
            m = PARTNER_RE.search(sent)
            if not m:
                out.append(dict(ev, roots=frozenset(base), parse="monotherapy clause (no 'in combination with')"))
                continue
            ptxt = m.group(1).strip(" ,")
            parts = re.split(r",\s*or\s+|,\s*|\s+or\s+", ptxt) if re.search(r"\bor\b", ptxt, re.I) else re.split(r",\s*|\s+and\s+", ptxt)
            resolved = []
            for p in parts:
                k, ids = di.lookup(p)
                resolved.append(ids if k in ("one", "combo") else None)
            if re.search(r"\bor\b", ptxt, re.I):          # alternatives: one regimen per partner
                for p, ids in zip(parts, resolved):
                    if ids:
                        out.append(dict(ev, roots=frozenset(base | ids), parse=f"alternative partner '{p.strip()}'"))
            elif resolved and all(resolved):                # all partners together
                out.append(dict(ev, roots=frozenset(base.union(*resolved)), parse="all partners together"))
            else:
                out.append(dict(ev, roots=None, parse=f"partners unresolved: '{ptxt[:80]}'"))
    return dict(mentions=mentions, treatment_mentions=treatment, regimens=out, products=products)


# ============================================================================ AACT (live)
def aact_client():
    sys.path.insert(0, CT_DIR)
    from dotenv import load_dotenv
    load_dotenv(os.path.join(CT_DIR, ".env"))
    from sources.aact.client import AACTClient
    return AACTClient()


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def aact_trial_meta(aact, ncts):
    out = {}
    for ch in chunks(ncts, 800):
        for r in aact.run_query("SELECT nct_id, brief_title, phase, overall_status, start_date, enrollment FROM ctgov.studies WHERE nct_id = ANY(%s)",
                                (ch,), limit=100000):
            out[r["nct_id"]] = r
    return out


APL_RX = re.compile(r"promyelocytic|promyelocitic|\bAPL\b(?!-\d)|PML[-/:]?RARA|t\(15;17\)", re.I)
APL_NEG_RX = re.compile(r"(?:non[- ]?|other than |except(?: for)? |excluding |exclusion of |without |not )(?:acute )?(?:APL|promyelocytic)", re.I)
DISEASE_RX = re.compile(r"leuk|AML|myeloid|MDS|myelodysplas|neoplasm|cancer|tumou?r|lymphom|myelom", re.I)


def apl_only_trials(aact, ncts):
    """Trials about APL alone (not AML at large): every AACT disease condition names APL, or the brief title names APL.
    A negated mention ('AML except APL') never counts. APL gets its own chart; these trials stay out of the AML tree."""
    cond, title = defaultdict(list), {}
    for ch in chunks(sorted(ncts), 800):
        for r in aact.run_query("SELECT nct_id, name FROM ctgov.conditions WHERE nct_id = ANY(%s)", (ch,), limit=500000):
            cond[r["nct_id"]].append(r["name"] or "")
        for r in aact.run_query("SELECT nct_id, brief_title, official_title FROM ctgov.studies WHERE nct_id = ANY(%s)", (ch,), limit=100000):
            title[r["nct_id"]] = (r["brief_title"] or "", r["official_title"] or "")
    out = set()
    for n in ncts:
        dz = [c for c in cond[n] if DISEASE_RX.search(c) or APL_RX.search(c)]
        bt, ot = title.get(n, ("", ""))
        if dz and all(APL_RX.search(c) and not APL_NEG_RX.search(c) for c in dz):
            out.add(n)
        elif APL_RX.search(bt) and not APL_NEG_RX.search(bt + " " + ot):
            out.add(n)
    return out


def apl_pair(p):
    """Same APL-only rule as apl_only_trials, applied to a match/graveyard row's own conditions and title (no AACT call)."""
    dz = [c for c in (p.get("conditions") or []) if DISEASE_RX.search(c) or APL_RX.search(c)]
    t = p.get("title") or ""
    if dz and all(APL_RX.search(c) and not APL_NEG_RX.search(c) for c in dz):
        return True
    return bool(APL_RX.search(t)) and not APL_NEG_RX.search(t)


def aact_enrich(aact, meta):
    """Adds AACT dates and the lead sponsor to each trial's metadata (the 'year' layer)."""
    ncts = [n for n in meta if NCT_RE.match(n)]
    for ch in chunks(ncts, 800):
        for r in aact.run_query("""SELECT nct_id, start_date_type, primary_completion_date, completion_date, study_first_submitted_date
                                   FROM ctgov.studies WHERE nct_id = ANY(%s)""", (ch,), limit=100000):
            meta[r["nct_id"]].update(start_date_type=r["start_date_type"], primary_completion_date=r["primary_completion_date"],
                                     completion_date=r["completion_date"], first_submitted=r["study_first_submitted_date"])
        for r in aact.run_query("SELECT nct_id, name, agency_class FROM ctgov.sponsors WHERE lead_or_collaborator = 'lead' AND nct_id = ANY(%s)", (ch,), limit=100000):
            meta[r["nct_id"]].update(lead_sponsor=r["name"], sponsor_class=r["agency_class"])
        # Quick tags per trial: each source field is tagged separately so tag_basis can say where a tag came from.
        src = defaultdict(lambda: defaultdict(list))
        for tbl in ("conditions", "keywords"):
            for r in aact.run_query(f"SELECT nct_id, name FROM ctgov.{tbl} WHERE nct_id = ANY(%s)", (ch,), limit=500000):
                src[r["nct_id"]][tbl].append(r["name"] or "")
        for r in aact.run_query("SELECT nct_id, criteria FROM ctgov.eligibilities WHERE nct_id = ANY(%s)", (ch,), limit=100000):
            src[r["nct_id"]]["eligibility inclusion"].append(inclusion_part(r["criteria"]))
        for n in ch:
            fields = dict(src[n], title=[meta[n].get("brief_title") or ""])
            per = {f: quick_tags(" ; ".join(texts)) for f, texts in fields.items()}
            # Tags come from all fields together (e.g. "newly diagnosed" in the title + "intensive induction" in inclusion = ND fit);
            # the basis lists the single fields that carry the tag alone, or "combined fields" when only the union does.
            tags = quick_tags(" ; ".join(" ; ".join(t) for t in fields.values()))
            basis = {t: [f for f, ts in per.items() if t in ts] or ["combined fields"] for t in tags}
            meta[n].update(quick_tags=tags or None, quick_tag_basis=basis or None)


def norm_id(v):
    return re.sub(r"[\s\-_/.]", "", v or "").upper()


def aact_links(aact, ncts, sponsor_of):
    """Explicit trial-to-trial links found in AACT. (a) the same non-NIH secondary/organisation ID under the same lead sponsor;
    (b) one trial's description text cites another trial's NCT number. Returns [(nct_a, nct_b, evidence)]."""
    s = {n for n in ncts if NCT_RE.match(n)}
    by_id = defaultdict(set)
    for ch in chunks(sorted(s), 800):
        for r in aact.run_query("SELECT nct_id, id_value FROM ctgov.id_information WHERE nct_id = ANY(%s) AND (id_type IS NULL OR id_type = 'OTHER')", (ch,), limit=200000):
            k = norm_id(r["id_value"])
            if len(k) >= 6 and not k.startswith("NCT"):
                by_id[k].add(r["nct_id"])
    links = []
    for k, ns in by_id.items():
        ns = sorted(ns)
        for i in range(len(ns)):
            for j in range(i + 1, len(ns)):
                if sponsor_of(ns[i]) and sponsor_of(ns[i]) == sponsor_of(ns[j]):
                    links.append((ns[i], ns[j], f"shared identifier {k} (same lead sponsor)"))
    for tbl in ("detailed_descriptions", "brief_summaries"):
        for ch in chunks(sorted(s), 800):
            for r in aact.run_query(f"SELECT DISTINCT nct_id, (regexp_matches(description, 'NCT[0-9]{{8}}', 'g'))[1] AS ref FROM ctgov.{tbl} WHERE nct_id = ANY(%s)", (ch,), limit=200000):
                if r["ref"] in s and r["ref"] != r["nct_id"]:
                    links.append((r["nct_id"], r["ref"], f"{tbl.replace('_', ' ')} of {r['nct_id']} cites {r['ref']}"))
    return links


def aact_arms(aact, ncts):
    arms = defaultdict(dict)
    for ch in chunks(ncts, 500):
        for r in aact.run_query("""SELECT dg.nct_id, dg.id AS gid, dg.group_type, dg.title AS arm, dg.description AS adesc, i.intervention_type AS itype, i.name AS iname,
                   (SELECT array_agg(o.name) FROM ctgov.intervention_other_names o WHERE o.intervention_id = i.id) AS others
            FROM ctgov.design_groups dg
            LEFT JOIN ctgov.design_group_interventions dgi ON dgi.design_group_id = dg.id
            LEFT JOIN ctgov.interventions i ON i.id = dgi.intervention_id
            WHERE dg.nct_id = ANY(%s)""", (ch,), limit=500000):
            a = arms[r["nct_id"]].setdefault(r["gid"], dict(title=r["arm"], type=r["group_type"], desc=r["adesc"] or "", ints=[], others={}))
            if r["iname"]:
                a["ints"].append((r["itype"], r["iname"]))
                a["others"][r["iname"]] = list(r["others"] or [])
    return arms


def build_regimens(ncts, arms, di):
    """Place each trial by the CONTENTS of its arms. Arm labels are unreliable (VIALE-A's venetoclax arm is typed ACTIVE_COMPARATOR)."""
    regimens = {}
    buckets = dict(no_arms=set(), arms_without_intervention_links=set(), control_or_non_drug_only=set())
    comparator = defaultdict(list)
    seen_names = set()
    for nct in ncts:
        alist = arms.get(nct)
        if not alist:
            buckets["no_arms"].add(nct)
            continue
        if not any(a["ints"] for a in alist.values()):
            buckets["arms_without_intervention_links"].add(nct)
            continue
        placed = False
        for a in alist.values():
            if any(re.search(r"placebo", n, re.I) for _, n in a["ints"]):
                comparator[nct].append(f"{a['title']} [{a['type']}]")     # placebo-containing arm = control, not a regimen under test
                continue
            roots, unmapped, src = set(), set(), defaultdict(set)
            for t, n in a["ints"]:
                if t not in DRUG_LIKE:
                    continue
                seen_names.add(n)
                k, ids = di.lookup(n)
                if k in ("one", "combo"):
                    roots |= ids
                    for x in ids:
                        src[x].add(n)
                        src[x].update(a.get("others", {}).get(n, []))
                else:
                    unmapped.add(n.strip())
            if not roots and not unmapped:
                continue
            linked_only, arm_text_named = [], None
            if len(roots) >= 2:
                # Co-administration keyword lookup: a component must be NAMED in the arm's own title/description (or a sponsor other-name),
                # not merely linked to the arm in AACT's intervention list - a multi-cohort arm lists every cohort's drugs (cobicistat in NCT04065399).
                text = f"{a['title']} {a.get('desc', '')}".lower()
                named = {x: any(term_in(t2, text) for t2 in (di.arm_terms(x) | src[x])) for x in roots}
                arm_text_named = {x for x, v in named.items() if v}
                linked_only = sorted(x for x, v in named.items() if not v) if arm_text_named else []   # only meaningful when the arm text names SOME components
            key = (tuple(sorted(roots)), tuple(sorted(unmapped)))
            reg = regimens.setdefault(key, dict(roots=frozenset(roots), unmapped=tuple(sorted(unmapped)), trials={}, flags={}))
            reg["trials"].setdefault(nct, []).append(f"{a['title']} [{a['type']}]")
            if arm_text_named is not None:
                reg["flags"].setdefault(nct, []).append(dict(arm=a["title"], named=sorted(arm_text_named), linked_only=linked_only,
                                                             uninformative=not arm_text_named))
            placed = True
        if not placed:
            buckets["control_or_non_drug_only"].add(nct)
    return regimens, buckets, comparator, seen_names


# ============================================================================ build
def build(args):
    db_path = os.path.join(BRIDGE_IN, "clinical_tracker.db")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.cursor()
    t0 = time.time()
    di = DrugIndex()
    onto = Ontology(args.scope_id)
    scope_name = onto.name.get(args.scope_id, args.scope_id)
    ev_reg = load_regulatory(di, onto)
    moa_targets, moa_group, moa_ensembl = load_mechanisms(di)
    ema_idx, ema_prov = load_ema(getattr(args, "refresh_ema", False))
    # EMA texts use British spelling ('leukaemia'); same disease terms as the US label regex otherwise
    ema_scope_re = re.compile(args.label_regex.replace("leukemia", "leuka?emia"), re.I)
    print(f"[ema] authorised human substances={len(ema_idx)} report={ema_prov.get('report_timestamp')}", flush=True)
    print(f"[load] drugs={len(di.name)} roots={len(di.members)} ontology ancestors={len(onto.ancestor_dist)-1} reg-evidence roots={len(ev_reg)} "
          f"mechanism-of-action roots={len(moa_targets)} (of which target-grouped={len(moa_group)}) ({time.time()-t0:.0f}s)", flush=True)

    # ---- CT: indications at the scope node, merged to ROOT drugs
    rows = cur.execute("""SELECT i.indication_id, i.chembl_id, d.name, i.max_clinical_stage, i.is_approved
                          FROM indications i JOIN drugs d USING(chembl_id) WHERE i.disease_id=?""", (args.scope_id,)).fetchall()
    roots = {}
    for ind_id, cid, nm, stage, appr in rows:
        r = di.root_of.get(cid, cid)
        d = roots.setdefault(r, dict(root=r, members=[], best_stage="UNKNOWN", ot_approved=False))
        d["members"].append(dict(chembl_id=cid, name=nm, stage=stage, approved=bool(appr), merge=di.merge.get(cid, "self"), indication_id=ind_id))
        if STAGE_RANK.get(stage, 0) > STAGE_RANK.get(d["best_stage"], 0):
            d["best_stage"] = stage
        d["ot_approved"] = d["ot_approved"] or bool(appr)
    for r, d in roots.items():
        d["name"] = di.root_name(r)
        d["evidence"] = ev_reg.get(r, [])
    approved_member_names = {m["name"].upper() for d in roots.values() for m in d["members"] if m["approved"]}

    # ---- outside sources (tools/source_index.py): DrugCentral + IUPHAR joined on identifiers, never on guessed names
    # (exact-name equality is the weakest key, used only when every identifier failed, and is labelled as such).
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
    openfda_key = os.environ.get("OPENFDA_API_KEY")
    if not os.path.exists(SOURCE_DB):
        raise SystemExit("source_index.db missing - run tools/source_index.py first")
    si = F.SourceIndex(SOURCE_DB, os.path.join(BULK, "drug_molecule.parquet"))
    onto_regex, n_onto_terms, n_onto_nodes = F.scope_term_regex(os.path.join(BULK, "disease.parquet"), args.scope_id)
    onto_re = re.compile(onto_regex, re.I)
    src_meta = dict(si.con.execute("SELECT k, v FROM meta"))
    for d in roots.values():
        ids = {m["chembl_id"] for m in d["members"]} | set(di.members.get(d["root"], [])) | {d["root"]}
        dc_all, gt_all = si.match(ids)
        # identifier joins earn credit; an exact-name-only candidate is shown for review but never counted as an origin
        weak = lambda meth: meth.startswith("exact name")
        dc = {k: v for k, v in dc_all.items() if not weak(v)}
        gt = {k: v for k, v in gt_all.items() if not weak(v)}
        d["src"] = si.evidence(dc, gt, onto_re)
        d["src_weak"] = dict(drugcentral=sorted(k for k, v in dc_all.items() if weak(v)), iuphar=sorted(k for k, v in gt_all.items() if weak(v)))
        d["ot_ensembl"] = sorted(moa_ensembl.get(d["root"], set()))
        d["ot_symbols"] = sorted({si.ot_symbol[e].upper() for e in d["ot_ensembl"] if e in si.ot_symbol})
    print(f"[sources] DrugCentral matched {sum(1 for d in roots.values() if d['src']['dc_structs'])}/{len(roots)} drugs, IUPHAR {sum(1 for d in roots.values() if d['src']['gt_ligands'])}/{len(roots)}; "
          f"ontology disease terms={n_onto_terms} from {n_onto_nodes} nodes; openFDA key {'set' if openfda_key else 'NOT set'} ({time.time()-t0:.0f}s)", flush=True)

    # ---- live FDA labels for candidate drugs
    labels = LabelChecker(os.path.join(ROOT, "data", "label_cache.json"), api_key=openfda_key)
    cand = [d for d in roots.values() if d["ot_approved"] or STAGE_RANK.get(d["best_stage"], 0) >= STAGE_RANK["PHASE_2_3"] or d["evidence"]]
    for d in cand:
        recs = [labels.fetch(m["name"]) for m in d["members"][:3]]
        if d["src"]["unii"]:
            recs.append(labels.fetch_unii(d["src"]["unii"]))
        merged, seen = [], set()
        for rec in recs:
            for lab in rec.get("labels", []) if rec.get("status") == "found" else []:
                if lab["set_id"] not in seen:
                    seen.add(lab["set_id"]); merged.append(lab)
        rec = dict(status="found" if merged else ("not_checked" if any(r.get("status") == "not_checked" for r in recs) else "no_label"), labels=merged)
        d["label_status"] = rec["status"]
        d["label"] = parse_label_regimens(rec, args.label_regex, di, d["root"])
        # same labels, disease terms taken from the Open Targets ontology (scope + all descendants) instead of a typed regex:
        # used as EVIDENCE only - it does not move a drug between categories (see meta.fidelity.label_mention_proposals).
        d["label_onto"] = parse_label_regimens(rec, onto_regex, di, d["root"])
        d["label_moa_text"] = " ".join(l.get("moa", "") for l in merged)
        d["label_apps"] = sorted({a for l in merged for a in l.get("application_numbers", [])})
    labels.save()
    print(f"[labels] candidates={len(cand)} network calls={labels.network_calls} ({time.time()-t0:.0f}s)", flush=True)

    # ---- live Drugs@FDA: US brand names + original approval dates (any indication) for the same candidates
    approvals = ApprovalChecker(os.path.join(ROOT, "data", "approval_cache.json"), api_key=openfda_key)
    for d in cand:
        d["approval"] = approvals.summarize([m["name"] for m in d["members"]] + [di.root_name(d["root"])])
    approvals.save()
    for d in cand:
        # a product is 'scope-relevant' only if its CURRENT US label names the scope disease as a treatment (verifiable from the label sentence)
        sb = {lr["brand"].upper() for lr in d["label"]["regimens"] if lr.get("role") == "treatment" and lr.get("brand")} if d.get("label") else set()
        ap = d["approval"]
        for b in ap["brands"]:
            b["names_scope_in_us_label"] = b["brand"].upper() in sb
        sc = sorted((b["first_approval"], b["brand"], b["application"]) for b in ap["brands"] if b["names_scope_in_us_label"] and b["first_approval"])
        ap["us_approval_scope_labeled"] = dict(date=sc[0][0], brand=sc[0][1], application=sc[0][2]) if sc else None
        scope_apps = {a for src in ("label", "label_onto") for lr in d[src]["regimens"] if lr.get("role") == "treatment" for a in lr.get("application_numbers", [])}
        ap["scope_linked_records"] = sorted((dict(application=a["application"], first_approval=a["first_approval"], brands=a["brands"])
                                             for a in ap.get("applications", []) if a["application"] in scope_apps), key=lambda r: r["application"])
        ap["brands"].sort(key=lambda b: (not b["names_scope_in_us_label"], b["brand"]))
    print(f"[approvals] Drugs@FDA network calls={approvals.network_calls} found={sum(1 for d in cand if d['approval']['status']=='found')}/{len(cand)} ({time.time()-t0:.0f}s)", flush=True)

    # ---- independent reference-source cross-check: public approval pages / press releases,
    # fetched raw and keyword-searched (see ReferenceSources) - a check against a second,
    # independent source, never merged into the OT/AACT/FDA-label evidence above.
    refs = ReferenceSources(REF_DIR)
    refs.load()
    pr_claims = json.load(open(os.path.join(REF_DIR, "pr_expectations.json"), encoding="utf-8"))
    claim_group_by_name = {c["drug"].upper(): c["group"] for c in pr_claims["claims"]}
    claim_by_drug = defaultdict(list)
    for c in pr_claims["claims"]:
        claim_by_drug[c["drug"].upper()].append(c)
    for d in cand:
        names = {m["name"].upper() for m in d["members"]} | {d["name"].upper()}
        seen_ids, pages = set(), []
        for nm in names:
            for p in refs.for_drug(nm):
                if p["id"] not in seen_ids:
                    seen_ids.add(p["id"]); pages.append(p)
        d["reference_pages"] = [dict(id=p["id"], url=p["url"], kind=p["kind"], status=p["status"],
                                     approval_date=p.get("approval_date"), approval_sentence=p.get("approval_sentence"),
                                     partners_claimed=p.get("partners", [])) for p in pages]
        d["reference_claims"] = [dict(id=c["id"], claim=c["claim"], expected_group=c["group"], expected_approval_date=c["approval_date"],
                                      page_id=c["source_id"], page_approval_date=(refs.pages.get(c["source_id"]) or {}).get("approval_date"),
                                      date_matches=(refs.pages.get(c["source_id"]) or {}).get("approval_date") == c["approval_date"])
                                 for nm in names for c in claim_by_drug.get(nm, [])]
    print(f"[reference] pages fetched={refs.network_calls} of {len(refs.pages)} configured; claims to verify={len(pr_claims['claims'])} ({time.time()-t0:.0f}s)", flush=True)
    # Drug pairs a reference source explicitly says must NOT be inferred as a combination
    # (e.g. cobicistat+revumenib: AACT links cobicistat to a multi-cohort arm that also names
    # it in the arm title/description, so arm_text_check alone marks it "named" - but neither
    # revuforj.com nor the FDA revumenib pages ever mention cobicistat as part of the regimen).
    forbidden_pairs = set()
    for spec in pr_claims.get("must_not_be_forced", []):
        k1, ids1 = di.lookup(spec["drug"])
        k2, ids2 = di.lookup(spec["must_not_contain"])
        if k1 == "one" and k2 == "one":
            forbidden_pairs.add(frozenset(ids1 | ids2))

    # ---- approved regimens from live US label clauses (source-tagged)
    approved_sets = defaultdict(list)
    for d in cand:
        for lr in d["label"]["regimens"]:
            if lr["roots"] and lr.get("role") == "treatment":
                approved_sets[lr["roots"]].append(dict(brand=lr["brand"], set_id=lr["set_id"], effective_time=lr["effective_time"],
                                                       sentence=lr["sentence"], parse=lr["parse"], setting=lr["setting"],
                                                       setting_verified=False))

    # ---- categorise each root drug (precedence documented in meta.rules)
    def resolve_group(d):
        """Mutation/mechanism group. Precedence: (1) a reference-verified approval claim that names a SPECIFIC mutation group;
        (2) a precision target from mechanism evidence, gated by POTENCY RANK - the candidate symbol must be among the
        drug's own top-2 most potent CURATED targets (DrugCentral's own curated MoA potency + IUPHAR, pKi/pKd/pIC50/
        pEC50 - deliberately not DrugCentral's raw uncurated screening-panel activity, see fidelity.potency_rank), or
        the drug must have no curated potency data at all; (3) General. Found live: without the gate, sunitinib and
        pexidartinib - drugs whose real, well-known identity is elsewhere (sunitinib: KIT/RET/PDGFRB-driven, approved
        for kidney/GIST; pexidartinib: a CSF1R inhibitor for tenosynovial giant cell tumor) - were placed in the SAME
        'FLT3' bucket as gilteritinib and midostaurin purely because ChEMBL lists an FLT3 interaction among many. A
        candidate that fails the potency rank, or is listed by only one origin family with no potency data to check,
        is never assigned - recorded as a proposal for review instead. A claim of 'General' means 'approved without a
        mutation restriction' - it does NOT block (2), so venetoclax (BCL2) and gemtuzumab (CD33) sit with their
        mechanism class beside investigational drugs of that class."""
        names = {m["name"].upper() for m in d["members"]} | {d["name"].upper()}
        claim = next((claim_group_by_name[n] for n in names if n in claim_group_by_name), None)
        fam = si.mechanism_families(d["ot_ensembl"], d["src"], d.get("label_moa_text"))
        act = F.activity_families(d["src"])
        tier, support = F.mechanism_tier(fam, act)
        d["mech_families"] = {k: sorted(v) for k, v in fam.items()}
        d["mech_activity"] = {k: dict(sorted(v.items(), key=lambda kv: -kv[1])[:6]) for k, v in act.items()} if tier == "activity-only" else {}
        # potency-derived symbols (activity-only) are measurements, not mechanism claims: they never feed group assignment
        support = support if tier in ("corroborated", "single-family") else {}
        d["mech_tier"], d["mech_support"] = tier, support

        candidates = set(d["ot_symbols"]) | {sym for sym, fs in support.items() if len(fs) >= 2}
        origin_syms, demoted = set(), {}
        for sym in candidates:
            rank, n = F.potency_rank(d["src"], sym)
            if rank is None or rank < 2:
                origin_syms.add(sym)
            else:
                demoted[sym] = dict(rank=rank, of_n_targets=n)
        d["mech_potency_demoted"] = demoted or None
        mech_group, mech_sym = F.group_from_symbols(origin_syms)
        prop_pool = (set(support) - origin_syms) | set(demoted)
        prop_group, prop_sym = F.group_from_symbols(prop_pool)
        if prop_group and prop_group != mech_group:
            reason = (f"ranked #{demoted[prop_sym]['rank'] + 1} of {demoted[prop_sym]['of_n_targets']} measured targets by potency, not top-2"
                      if prop_sym in demoted else f"listed by only one origin family ({', '.join(support.get(prop_sym, []))}), no potency data to rank it")
            d["group_proposal"] = dict(group=prop_group, symbol=prop_sym, families=support.get(prop_sym), n_targets=len(support), reason=reason)
        else:
            d["group_proposal"] = None
        if claim and claim != "General":
            return claim, "approval claim (pr_expectations.json, reference-source verified)"
        if mech_group:
            note = "; approval claim confirms no mutation restriction" if claim == "General" else ""
            rank, n = F.potency_rank(d["src"], mech_sym)
            pot = f"; ranked #{rank + 1} of {n} measured targets by potency" if rank is not None else "; no potency data (accepted by default)"
            return mech_group, f"mechanism: target {mech_sym}, listed by {', '.join(support.get(mech_sym, []) or ['Open Targets'])}{pot}{note}"
        if claim == "General":
            return "General", "approval claim: not mutation-restricted; no precision target"
        return "General", "no mutation/antigen-restricted approval claim or matched precision-oncology target"

    for d in roots.values():
        exact = [e for e in d["evidence"] if e["relation"] in ("exact", "descendant")]
        near = [e for e in d["evidence"] if e["relation"] == "ancestor" and e["distance"] <= NEAR_ANCESTOR_MAX]
        us_mention = bool(d.get("label", {}).get("mentions"))
        us_treatment = bool(d.get("label", {}).get("treatment_mentions"))
        if d["ot_approved"] or exact:
            d["category"] = "APPROVED_AT_SCOPE"
            d["jurisdictions"] = sorted({e["jurisdiction"] for e in exact})
        elif near:
            d["category"], d["jurisdictions"] = "APPROVED_BROADER_TERM", sorted({e["jurisdiction"] for e in near})
        elif us_treatment:
            d["category"], d["jurisdictions"] = "LABEL_ONLY", ["US"]
        else:
            d["category"], d["jurisdictions"] = "INVESTIGATIONAL", []
        d["us_label_scope_mention"] = us_mention if d.get("label") else None
        d["us_label_supportive_only"] = bool(us_mention and not us_treatment)
        d["partner_in"] = sorted({" + ".join(sorted(di.root_name(x) for x in rs)) for rs in approved_sets if d["root"] in rs and len(rs) > 1})
        d["mutation_group"], d["mutation_group_basis"] = resolve_group(d)
        d["mechanism_targets"] = sorted(f"{t} ({a})" for t, a in moa_targets.get(d["root"], set()))
        # EMA authorised products for this drug, joined on the normalised substance name (root name or any merged member name)
        keys = {substance_key(d["name"])} | {substance_key(m["name"]) for m in d["members"]}
        prods = {p["medicine"]: p for k in keys for p in ema_idx.get(k, [])}
        d["ema_indications"] = []
        sents = []
        for p in sorted(prods.values(), key=lambda p: p["medicine"] or ""):
            hits = [s.strip() for s in re.split(r"(?<=[.;])\s+", p["indication"]) if ema_scope_re.search(s)]
            d["ema_indications"].append(dict(medicine=p["medicine"], url=p["url"], substances=p["substances"], authorised=p["authorised"],
                                             names_scope=bool(hits), sentence=(hits[0] if hits else p["indication"])[:420]))
            sents += [(f"EMA {p['medicine']}", s) for s in hits]
        sents += [(f"US label {r['brand']}", r["sentence"]) for r in (d.get("label") or {}).get("regimens", []) if r.get("role") == "treatment"]
        # Which regulator / curated source names the scope disease itself (not a broader ontology term) for this drug
        named = []
        if d["category"] == "APPROVED_AT_SCOPE":
            named.append("Open Targets regulatory report at scope (" + "/".join(d["jurisdictions"] or ["OT approved"]) + ")")
        if (d.get("label") or {}).get("treatment_mentions"):
            named.append("US label text")
        if any(e["names_scope"] for e in d["ema_indications"]):
            named.append("EMA indication text")
        if d["src"]["indications"]:
            named.append("DrugCentral curated indication")
        d["scope_named_by"] = named
        d["therapeutic_role"] = therapeutic_role(sents) if d["category"] != "INVESTIGATIONAL" else None
        # Approved-for tags: only from regulator text that names the scope disease (EMA indication hits + US label treatment sentences).
        d["approved_tags"] = None
        if d["category"] != "INVESTIGATIONAL" and sents:
            basis = defaultdict(list)
            for who, s in sents:
                for t in quick_tags(s):
                    basis[t].append(who)
            d["approved_tags"] = {t: sorted(set(w))[:4] for t, w in sorted(basis.items(), key=lambda kv: TAG_ORDER.index(kv[0]))} or None

    # ---- FDA approval letters (Drugs@FDA application_docs). Only applications that a scope-naming label points to (application-number
    # join) are read, so a letter is never attributed to a drug by name.
    letters = LetterChecker(os.path.join(ROOT, "data", "letter_cache.json"))
    for d in roots.values():
        d["letters"], d["scope_first_letter"] = [], None
        ap = d.get("approval") or {}
        if d["category"] == "INVESTIGATIONAL" or not ap:
            continue
        linked = {r["application"] for r in ap.get("scope_linked_records", [])}
        seen_urls = set()
        for a in ap.get("applications", []):
            if a["application"] not in linked:
                continue
            for doc in a.get("docs", []):
                if doc["type"] != "Letter" or doc["url"] in seen_urls:
                    continue
                seen_urls.add(doc["url"])
                r = letters.check(doc["url"], args.scope_id, onto_re)
                d["letters"].append(dict(application=a["application"], submission=doc["sub"], cls=doc["cls"], date=doc["date"] or doc["doc_date"], url=doc["url"],
                                         status=r["status"], mentions=bool(r.get("mentions")), term=r.get("term"), snippet=r.get("snippet")))
        named = sorted((l["date"], l["application"], l["term"], l["url"]) for l in d["letters"] if l["mentions"] and l["date"])
        d["scope_first_letter"] = dict(date=named[0][0], application=named[0][1], term=named[0][2], url=named[0][3]) if named else None
    letters.save()
    n_letters = sum(len(d["letters"]) for d in roots.values())
    print(f"[letters] approval letters read={n_letters} (network fetches this run={letters.network_calls}); "
          f"naming the scope={sum(1 for d in roots.values() for l in d['letters'] if l['mentions'])} ({time.time()-t0:.0f}s)", flush=True)

    def record_dates(d):
        """Approval dates in Drugs@FDA for the applications the drug's scope-naming label ties to (else any application of the drug)."""
        ap = d.get("approval") or {}
        linked = {r["application"] for r in ap.get("scope_linked_records", [])}
        out = set()
        for a in ap.get("applications", []):
            if linked and a["application"] not in linked:
                continue
            if a.get("first_approval"):
                out.add(a["first_approval"])
            out.update(a.get("efficacy_supplements", []))
        return out

    for d in cand:
        rd = record_dates(d)
        for c in d.get("reference_claims") or []:
            c["record_date_matches"] = c["expected_approval_date"] in rd
            c["letter_date_matches"] = any(l["mentions"] and l["date"] == c["expected_approval_date"] for l in d["letters"])

    # ---- US AML gate. Approved = a current US label names the scope as a treatment indication AND an FDA approval letter in
    # Drugs@FDA names the scope. APL-only drugs fail the first test (US labels say 'promyelocytic', not AML). Everything else
    # that had an approval signal becomes SUPPLEMENTAL (backbone, supportive/conditioning, approved-regimen partner) or EXCLUDED.
    for d in roots.values():
        d["source_category"], d["reference_jurisdictions"] = d["category"], sorted(set(d["jurisdictions"]) - {"US"})
        us_label = bool((d.get("label") or {}).get("treatment_mentions"))
        us_letter = bool(d.get("scope_first_letter"))
        role = (d.get("therapeutic_role") or {}).get("tag") or ""
        if d["category"] == "INVESTIGATIONAL" and not (d["name"] in BACKBONE_DRUGS | SUPPORTIVE_DRUGS):
            d["us_gate"] = None
            continue
        if d["name"] in SUPPORTIVE_DRUGS or role.startswith(("supportive", "conditioning")):
            d["us_gate"] = "supportive or conditioning agent (shown only inside approved-drug regimens)"
        elif d["name"] in BACKBONE_DRUGS:
            d["us_gate"] = "backbone chemotherapy (shown only inside approved-drug regimens)"
        elif us_label and us_letter:
            d["us_gate"] = "US label + FDA approval letter name the scope"
        elif d["partner_in"]:
            d["us_gate"] = "partner in a US-approved regimen only (no own US scope approval)"
        else:
            d["us_gate"] = "no US approval for the scope (" + ("no US label naming the scope" if not us_label else "no FDA letter naming the scope") + ")"
        if d["us_gate"].startswith("US label"):
            d["category"], d["jurisdictions"] = "APPROVED_AT_SCOPE", ["US"]
        elif d["us_gate"].startswith("no US"):
            d["category"] = "EXCLUDED_NOT_US_SCOPE"
        else:
            d["category"] = "SUPPLEMENTAL"
    print("[us-gate] " + "; ".join(f"{c}={sum(1 for d in roots.values() if d['category'] == c)}"
                                    for c in ("APPROVED_AT_SCOPE", "SUPPLEMENTAL", "EXCLUDED_NOT_US_SCOPE")), flush=True)
    for d in sorted(roots.values(), key=lambda x: (x["category"], x["name"])):
        if d.get("us_gate"):
            print(f"[us-gate]   {d['category']:<22} {d['name']:<28} {d['source_category']:<22} {d['us_gate']}", flush=True)

    # ---- fidelity: tiers from independent origin families (rules in tools/fidelity.py). Each tier is computed twice - with the
    # sources that existed before (baseline) and with everything - so the benchmark movement is measured, not asserted.
    sym_of_group = {lab: sym for sym, lab in F.SYMBOL_GROUP_RULES}
    near_ok = lambda e: e["relation"] in ("exact", "descendant") or (e["relation"] == "ancestor" and e["distance"] <= NEAR_ANCESTOR_MAX)

    def approval_fidelity(d, integrated, use_letters=True):
        ot_j = {e["jurisdiction"] for e in d["evidence"] if near_ok(e)}
        label_hit = bool(d.get("label", {}).get("treatment_mentions")) or (integrated and bool(d.get("label_onto", {}).get("treatment_mentions")))
        fam = set()
        if "US" in ot_j or label_hit:
            fam.add("US label (DailyMed SPL: Open Targets US + openFDA label)")
        if "EU" in ot_j:
            fam.add("EU regulator (EMA)")
        if "JP" in ot_j:
            fam.add("JP regulator (PMDA)")
        if any(c.get("date_matches") for c in d.get("reference_claims") or []):
            fam.add("reference page (date-verified)")
        if integrated and use_letters and any(l["mentions"] for l in d.get("letters", [])):
            fam.add("FDA approval letter (Drugs@FDA; same regulator as the label, a different document)")
        conf, ap, agencies = [], d.get("approval") or {}, {a["agency"] for a in d["src"]["approvals"]}
        if any(f.startswith("US label") for f in fam):
            if ap.get("us_approval_scope_labeled"):
                conf.append("Drugs@FDA record via a brand whose label names the scope")
            elif integrated and ap.get("scope_linked_records"):
                conf.append("Drugs@FDA record via the label's application number")
            elif integrated and "FDA" in agencies:
                conf.append("DrugCentral FDA approval record (drug-level)")
        if integrated:
            for fam_name, ag in (("EU regulator (EMA)", "EMA"), ("JP regulator (PMDA)", "PMDA")):
                if fam_name in fam and ag in agencies:
                    conf.append(f"DrugCentral {ag} approval record (drug-level)")
            if d["src"]["indications"]:
                conf.append("curated AML indication (DrugCentral)")
        return dict(tier=F.approval_tier(fam, len(conf)), families=sorted(fam), confirmations=conf)

    def group_fidelity(d):
        g = d["mutation_group"]
        if g == "General":
            return dict(level="deliberate" if d["mech_tier"] in ("corroborated", "single-family") else "default (no curated mechanism)", kinds=[])
        kinds, names = [], {m["name"].upper() for m in d["members"]} | {d["name"].upper()}
        if any(claim_group_by_name.get(n) == g for n in names):
            kinds.append("approval claim")
        if any(rx.search(t) and lab == g for t in d["src"]["indications"] for rx, lab in F.INDICATION_GROUP_TOKENS):
            kinds.append("curated indication names the mutation (DrugCentral)")
        fs = d["mech_support"].get(sym_of_group.get(g), [])
        if fs:
            kinds.append(f"mechanism target {sym_of_group[g]} ({len(fs)} origin famil{'y' if len(fs) == 1 else 'ies'})")
        return dict(level="high" if len(kinds) >= 2 else "medium", kinds=kinds)

    for d in roots.values():
        d["mech_tier_baseline"] = "single-family" if d["ot_symbols"] else "no data"
        d["group_fid"] = group_fidelity(d)
        if d["category"] != "INVESTIGATIONAL":
            d["fid_approval_base"], d["fid_approval"] = approval_fidelity(d, False), approval_fidelity(d, True)
            d["fid_approval_noletter"] = approval_fidelity(d, True, use_letters=False)

    # ---- trials at the scope, live metadata, arms -> regimens
    # label-defined multi-substance products let the resolver settle e.g. 'CPX-351' (ambiguous between two components)
    di.combo_products = set().union(*[d["label"]["products"] for d in cand if d.get("label")])
    aact = aact_client()
    scope_ncts = sorted({r[0] for r in cur.execute("""SELECT DISTINCT cr.nct_id FROM indication_reports ir JOIN indications i USING(indication_id)
        JOIN clinical_reports cr USING(report_id) WHERE i.disease_id=? AND cr.nct_id IS NOT NULL""", (args.scope_id,)) if NCT_RE.match(r[0] or "")})
    # APL-only trials are held out of the AML tree (separate APL chart later); listed in the audit branch.
    apl_ncts = apl_only_trials(aact, scope_ncts)
    scope_ncts = [n for n in scope_ncts if n not in apl_ncts]
    print(f"[apl] held out {len(apl_ncts)} APL-only scope trials", flush=True)
    meta = aact_trial_meta(aact, scope_ncts)
    arms = aact_arms(aact, scope_ncts)
    # sponsor-supplied alternate names (AACT) for strings that still do not resolve; source-tagged in di.alias_via
    def unresolved_names(arms_dict):
        return {n for a in arms_dict.values() for g in a.values() for t, n in g["ints"]
                if t in DRUG_LIKE and not re.search("placebo", n, re.I) and di.lookup(n)[0] not in ("one", "combo")}
    di.load_aact_aliases(aact, unresolved_names(arms))
    regimens, buckets, comparator, seen_names = build_regimens(scope_ncts, arms, di)
    print(f"[aact] scope trials={len(scope_ncts)} meta={len(meta)} regimens={len(regimens)} ({time.time()-t0:.0f}s)", flush=True)
    _f = [f for rg in regimens.values() for fl in rg["flags"].values() for f in fl]
    print(f"[arm-text] multi-drug arm instances={len(_f)}; arm text names a smaller set={sum(1 for f in _f if f['linked_only'])}; arm text names none (uninformative)={sum(1 for f in _f if f['uninformative'])}", flush=True)

    # ---- NPM1 (text-defined) subtype trials
    pat = f"%{args.subtype_text}%"
    trial_sql = """SELECT s.nct_id, s.brief_title, s.phase, s.overall_status, s.start_date, s.enrollment,
               bool_or(c.name ILIKE %s) AS m_condition, bool_or(k.name ILIKE %s) AS m_keyword, bool_or(s.brief_title ILIKE %s) AS m_title
        FROM ctgov.studies s LEFT JOIN ctgov.conditions c ON c.nct_id = s.nct_id LEFT JOIN ctgov.keywords k ON k.nct_id = s.nct_id
        WHERE c.name ILIKE %s OR k.name ILIKE %s OR s.brief_title ILIKE %s
        GROUP BY s.nct_id, s.brief_title, s.phase, s.overall_status, s.start_date, s.enrollment ORDER BY s.nct_id"""
    sub_trials = aact.run_query(trial_sql, (pat,) * 6, limit=5000)
    sub_meta = {t["nct_id"]: t for t in sub_trials}
    sub_ncts = sorted(t["nct_id"] for t in sub_trials if NCT_RE.match(t["nct_id"]))
    sub_apl = apl_only_trials(aact, sub_ncts)
    sub_ncts = [n for n in sub_ncts if n not in sub_apl]
    apl_ncts |= sub_apl
    sub_arms = aact_arms(aact, sub_ncts)
    di.load_aact_aliases(aact, unresolved_names(sub_arms))
    sub_reg, sub_buckets, sub_comp, sub_seen = build_regimens(sub_ncts, sub_arms, di)
    aact_hash = hashlib.sha256(trial_sql.encode()).hexdigest()[:12]
    aact_enrich(aact, meta)
    aact_enrich(aact, sub_meta)
    sponsor_of = lambda n: ((meta.get(n) or sub_meta.get(n) or {}).get("lead_sponsor") or "").strip().casefold()
    xlinks = aact_links(aact, set(scope_ncts) | set(sub_ncts), sponsor_of)
    print(f"[aact] enriched dates+sponsors; explicit trial links={len(xlinks)} ({time.time()-t0:.0f}s)", flush=True)

    # ------------------------------------------------------------------ tree assembly helpers
    def arm_text_check(rg):
        """Keyword lookup of every component of a multi-drug regimen in the ARM TEXT of the trials that produced it."""
        fl = [f for v in rg.get("flags", {}).values() for f in v]
        if not fl:
            return dict(status="single agent (no multi-drug arm)", named=[], linked_only_never_named=[])
        named = set().union(*[set(f["named"]) for f in fl])
        if not any(not f["uninformative"] for f in fl):
            return dict(status="arm text names none of the drugs (uninformative titles like 'Cohort 2'); AACT intervention links only", named=[], linked_only_never_named=[])
        never = sorted(di.root_name(x) for x in rg["roots"] - named)
        return dict(status="verified" if not never else "partial", named=sorted(di.root_name(x) for x in named), linked_only_never_named=never)

    def regimen_category(rg):
        if rg["unmapped"]:
            return "UNRESOLVED_COMPONENT", None
        if rg["roots"] in approved_sets:
            return "APPROVED_REGIMEN_US_LABEL", approved_sets[rg["roots"]][0]
        backbone = [a for a in approved_sets if a < rg["roots"]]
        if backbone:
            a = max(backbone, key=len)
            ev = dict(backbone=[di.root_name(x) for x in sorted(a)], **approved_sets[a][0])
            if any(pair <= rg["roots"] for pair in forbidden_pairs):
                return "APPROVED_BACKBONE_PLUS_DISPUTED_ADDON", dict(ev, reference_dispute="A reference source (pr_expectations.json must_not_be_forced) "
                    "explicitly does not mention this drug as part of the approved regimen - not treated as a verified combination even though AACT links it to the arm.")
            extras = {di.root_name(x) for x in rg["roots"] - a}
            if extras & set(arm_text_check(rg)["linked_only_never_named"]):
                return "APPROVED_DRUG_ADDON_UNVERIFIED", ev
            return "APPROVED_BACKBONE_PLUS_INVESTIGATIONAL", ev
        return "INVESTIGATIONAL_REGIMEN", None

    dstr = lambda v: str(v) if v else None

    def trial_node(prefix, nct, m, arms_of, flags=()):
        return dict(id=f"{prefix}:{nct}", kind="trial", name=nct,
                    attrs=dict(linked_only_components=sorted({di.root_name(x) for f in flags for x in f["linked_only"]}) or None,
                               title=m.get("brief_title"), phase=m.get("phase"), status=m.get("overall_status"),
                               start_date=dstr(m.get("start_date")), start_year=(dstr(m.get("start_date")) or "")[:4] or None,
                               start_date_type=m.get("start_date_type"), primary_completion_date=dstr(m.get("primary_completion_date")),
                               completion_date=dstr(m.get("completion_date")), first_submitted=dstr(m.get("first_submitted")),
                               lead_sponsor=m.get("lead_sponsor"), sponsor_class=m.get("sponsor_class"), enrollment=m.get("enrollment"),
                               arms=arms_of[:4], comparator_arms=comparator_lookup.get(nct, [])[:3],
                               tags=m.get("quick_tags"), tag_basis=m.get("quick_tag_basis")),
                    provenance=dict(source="live AACT", table="ctgov.studies + sponsors + design_groups", key=nct), children=[])

    PHASE_RANK = {"EARLY_PHASE1": 0, "PHASE1": 1, "PHASE1/PHASE2": 1.5, "PHASE2": 2, "PHASE2/PHASE3": 2.5, "PHASE3": 3, "PHASE4": 4}
    PHASE_SHORT = {"EARLY_PHASE1": "Ph0/1", "PHASE1": "Ph1", "PHASE1/PHASE2": "Ph1/2", "PHASE2": "Ph2", "PHASE2/PHASE3": "Ph2/3", "PHASE3": "Ph3", "PHASE4": "Ph4"}

    def build_programs(prefix, tl, metas, trial_nodes):
        """Program = trials of ONE regimen that are linked by (a) explicit AACT evidence, else (b) the same lead sponsor (inferred, flagged).
        Sits between the regimen and the NCT layer so the Ph1 -> Ph3 sequence of a development effort reads in date order."""
        parent = {n: n for n in tl}
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        explicit = defaultdict(list)
        by_sp = defaultdict(list)
        for n in tl:
            if sponsor_of(n):
                by_sp[sponsor_of(n)].append(n)
        for ns in by_sp.values():
            for x in ns[1:]:
                parent[find(x)] = find(ns[0])
        tls = set(tl)
        for a, b, via in xlinks:
            if a in tls and b in tls:
                parent[find(a)] = find(b)
                explicit[a].append(via)
                explicit[b].append(via)
        comps = defaultdict(list)
        for n in tl:
            comps[find(n)].append(n)
        out = []
        for members in comps.values():
            def key(n):
                m = metas.get(n) or {}
                return (dstr(m.get("start_date")) or "9999-99-99", PHASE_RANK.get(m.get("phase"), 9), n)
            members.sort(key=key)
            first_by_phase = {}
            for n in members:
                m = metas.get(n) or {}
                ph = PHASE_SHORT.get(m.get("phase"), "Ph n/a")
                yr = (dstr(m.get("start_date")) or "")[:4] or "n/a"
                first_by_phase.setdefault(ph, yr)
            path = " → ".join(f"{ph} {yr}" for ph, yr in sorted(first_by_phase.items(), key=lambda kv: (kv[1] == "n/a", kv[1], kv[0])))
            spons = sorted({(metas.get(n) or {}).get("lead_sponsor") for n in members} - {None})
            evid = sorted({v for n in members for v in explicit.get(n, [])})
            basis = "explicit" if evid else ("inferred: same lead sponsor + same regimen" if len(members) > 1 else "single trial")
            best = max((PHASE_RANK.get((metas.get(n) or {}).get("phase"), -1) for n in members), default=-1)
            n_act = sum(1 for n in members if (metas.get(n) or {}).get("overall_status") in ACTIVE)
            pkey = hashlib.sha1("|".join(members).encode()).hexdigest()[:8]
            out.append((best, key(members[0]), dict(
                id=f"{prefix}:p:{pkey}", kind="program",
                name=f"{(spons[0] if spons else 'sponsor n/a')[:38]} · {path}  ({len(members)} trial{'s' if len(members) != 1 else ''}, {n_act} active)",
                attrs=dict(lead_sponsors=spons, sponsor_class=sorted({(metas.get(n) or {}).get("sponsor_class") or "n/a" for n in members}),
                           link_basis=basis, link_evidence=evid[:6], phase_path=path,
                           trials=[dict(nct=n, phase=(metas.get(n) or {}).get("phase"), start=dstr((metas.get(n) or {}).get("start_date")),
                                        status=(metas.get(n) or {}).get("overall_status")) for n in members],
                           trials_total=len(members), trials_active=n_act, highest_phase_rank=best,
                           first_start_year=(dstr((metas.get(members[0]) or {}).get("start_date")) or "")[:4] or None),
                provenance=dict(source="live AACT (ctgov.sponsors, id_information, description text)", rule="union by explicit link, else same lead sponsor within one regimen"),
                children=[trial_nodes[n] for n in members])))
        out.sort(key=lambda t: (-t[0], t[1]))
        return [p for _, _, p in out]

    def regimen_node(prefix, rg, metas, rel_to=None):
        cat, ev = regimen_category(rg)
        names = sorted([di.root_name(x) for x in rg["roots"]] + list(rg["unmapped"]), key=str.lower)
        if rel_to is not None:
            partners = sorted([di.root_name(x) for x in rg["roots"] if x != rel_to] + list(rg["unmapped"]), key=str.lower)
            label = "+ " + " + ".join(partners) if partners else "monotherapy"
        else:
            label = " + ".join(names)
        tl = sorted(rg["trials"])
        act = [n for n in tl if (metas.get(n) or {}).get("overall_status") in ACTIVE]
        rkey = hashlib.sha1(("|".join(sorted(rg["roots"])) + "#" + "|".join(rg["unmapped"])).encode()).hexdigest()[:10]
        tnodes = {n: trial_node(f"{prefix}:{rkey}:t", n, metas.get(n) or {}, rg["trials"][n], rg.get("flags", {}).get(n, [])) for n in tl}
        programs = build_programs(f"{prefix}:{rkey}", tl, metas, tnodes)
        # Mechanism, mapped down to the trial: each component drug's ALREADY-COMPUTED mutation_group/mechanism_targets
        # (see resolve_group, potency-rank gated), denormalized onto its regimen so a trial under e.g. Phase 2 shows
        # what it's mechanistically testing without navigating back up to the drug. Never re-derived here - a
        # regimen never claims a mechanism its component drug doesn't itself carry.
        component_mechanisms = [dict(drug=di.root_name(x), mutation_group=roots[x]["mutation_group"], mechanism_targets=roots[x].get("mechanism_targets") or None,
                                     citations=(roots[x].get("src") or {}).get("gt_citations") or None)
                                for x in sorted(rg["roots"]) if x in roots]
        return dict(id=f"{prefix}:reg:{rkey}", kind="regimen", name=f"{label}  ({len(act)} active / {len(tl)})",
                    attrs=dict(regimen=" + ".join(names), category=cat, drugs=[di.root_name(x) for x in sorted(rg["roots"])],
                               component_mechanisms=component_mechanisms or None,
                               programs=len(programs), arm_text_check=arm_text_check(rg),
                               us_first_approval_by_drug=({di.root_name(x): (roots[x].get("approval") or {}).get("first_us_approval") for x in sorted(rg["roots"]) if x in roots}
                                                          if cat in ("APPROVED_REGIMEN_US_LABEL", "APPROVED_BACKBONE_PLUS_INVESTIGATIONAL") else None),
                               unmapped_components=list(rg["unmapped"]), trials_total=len(tl), trials_active=len(act),
                               all_trial_ids=tl, approval_evidence=ev,
                               label_setting=(ev or {}).get("setting"),
                               approval_note=("Drug-set match to a current US label clause ONLY. Line of therapy, population/biomarker and formulation "
                                              "(e.g. oral Onureg vs IV azacitidine) are NOT verified against the trial - compare with the setting tags."
                                              if cat in ("APPROVED_REGIMEN_US_LABEL", "APPROVED_BACKBONE_PLUS_INVESTIGATIONAL") else
                               ("The approved drug is real, but the extra drug(s) are only LINKED to the arm in AACT's intervention list and are never named in the arm text "
                                "(often a multi-cohort arm or a co-medication such as a CYP3A4 booster). Not treated as a verified combination." if cat == "APPROVED_DRUG_ADDON_UNVERIFIED" else None))),
                    provenance=dict(source="live AACT arms + live US label clauses", rule="regimen = drug set of a non-placebo arm"),
                    children=programs)

    comparator_lookup = {}
    comparator_lookup.update(comparator)
    comparator_lookup.update(sub_comp)

    def brands_of(d):
        ap = d.get("approval") or {}
        out = [dict(brand=x["brand"], source=x["source"], application=x["application"], first_us_approval=x["first_approval"],
                    us_label_names_scope=x["names_scope_in_us_label"]) for x in ap.get("brands", [])]
        seen = {x["brand"].upper() for x in out}
        generic = {m["name"].upper() for m in d["members"]} | {d["name"].upper()}
        for up, (lab, src) in sorted(di.trade.get(d["root"], {}).items()):
            if up not in seen and up not in generic and len(out) < 8 and d["category"] != "INVESTIGATIONAL":
                out.append(dict(brand=lab, source=f"Open Targets tradeName ({src or 'n/a'})", application=None, first_us_approval=None, us_label_names_scope=False))
                seen.add(up)
        return out

    def drug_node(d, prefix="drug"):
        regs = [rg for rg in regimens.values() if d["root"] in rg["roots"]]
        ap = d.get("approval") or {}
        brands = brands_of(d)
        fua = ap.get("first_us_approval")
        scoped = [b["brand"] for b in brands if b["us_label_names_scope"]]
        others = [b["brand"] for b in brands if not b["us_label_names_scope"]]
        if scoped:
            bdisp, bbasis = ", ".join(scoped[:3]), f"current US label names {scope_name}"
        elif others:
            bdisp, bbasis = ", ".join(others[:2]) + " *", "no current US label names the scope - brands of other indications/forms (unverified for this disease)"
        else:
            bdisp, bbasis = None, "no brand found"
        tl_years = defaultdict(lambda: defaultdict(int))
        for rg in regimens.values():
            if d["root"] in rg["roots"]:
                for n in rg["trials"]:
                    m = meta.get(n) or {}
                    tl_years[(dstr(m.get("start_date")) or "n/a")[:4]][PHASE_SHORT.get(m.get("phase"), "Ph n/a")] += 1
        timeline = [dict(year=y, trials_started=dict(sorted(ph.items()))) for y, ph in sorted(tl_years.items(), key=lambda kv: (kv[0] == "n/a", kv[0]))]
        # Investigated-for tags: rolled up from this drug's scope trials (per-trial tags, counted; live = active statuses).
        inv = defaultdict(lambda: dict(trials=0, live=0, example_ncts=[]))
        for n in sorted({n for rg in regs for n in rg["trials"]}):
            m = meta.get(n) or {}
            for t in m.get("quick_tags") or []:
                inv[t]["trials"] += 1
                inv[t]["live"] += m.get("overall_status") in ACTIVE
                if len(inv[t]["example_ncts"]) < 5:
                    inv[t]["example_ncts"].append(n)
        inv = {t: inv[t] for t in sorted(inv, key=TAG_ORDER.index)}
        appr = d.get("approved_tags") or {}
        tags = [f"approved: {t}" for t in appr] + [f"investigated: {t}" for t in inv] + sorted(set(appr) | set(inv), key=TAG_ORDER.index)
        rnodes = sorted((regimen_node(f"{prefix}:{d['root']}", rg, meta, rel_to=d["root"]) for rg in regs),
                        key=lambda n: (n["attrs"]["category"] != "APPROVED_REGIMEN_US_LABEL", -n["attrs"]["trials_active"], n["name"]))
        return dict(id=f"{prefix}:{d['root']}", kind="drug", name=d["name"],
                    attrs=dict(chembl_root=d["root"], category=d["category"], jurisdictions=d["jurisdictions"],
                               us_gate=d.get("us_gate"), source_category=d.get("source_category"),
                               reference_jurisdictions=d.get("reference_jurisdictions", []),
                               ot_best_stage=d["best_stage"], ot_approved_at_scope=d["ot_approved"],
                               us_label_scope_mention=d.get("us_label_scope_mention"), approved_as_partner_in=d.get("partner_in"),
                               brands=brands, brand_display=bdisp, brand_basis=bbasis,
                               us_approval_scope_labeled=ap.get("us_approval_scope_labeled"),
                               first_us_approval=fua, first_us_approval_status=ap.get("status", "not_looked_up"),
                               development_timeline=timeline,
                               label_expansion_candidates=ap.get("efficacy_supplements") or None,
                               approval_date_note=("FDA original approval of this molecule for ANY indication (Drugs@FDA, live) - not the date of the "
                                                   f"{scope_name} indication. 'label_expansion_candidates' are FDA efficacy-supplement dates; Drugs@FDA does not tag them by indication. "
                                                   "Open Targets regulatory reports carry no date (year/countries are null for every regulatory row) and EU/JP dates are not integrated."
                                                   if (fua or d["category"] != "INVESTIGATIONAL") else None),
                               members=[f"{m['name']} ({m['chembl_id']}, {m['stage']}, merge={m['merge']})" for m in d["members"]],
                               approval_evidence=d["evidence"][:8],
                               scope_named_by=d.get("scope_named_by"), therapeutic_role=d.get("therapeutic_role"),
                               tags=tags or None, approved_for=appr or None, investigated_for=inv or None,
                               tag_basis=("approved_for = quick-tag regex over regulator sentences naming the disease (source listed per tag). "
                                          "investigated_for = per-trial tags from AACT title, conditions, keywords and the inclusion part of eligibility, "
                                          "counted over this drug's scope trials. '1L'/'2L+' are inferred from newly diagnosed / relapsed-refractory, not stated by the source.")
                               if tags else None,
                               ema_indications=d.get("ema_indications") or None,
                               mutation_group=d.get("mutation_group"), mutation_group_basis=d.get("mutation_group_basis"),
                               mechanism_targets=d.get("mechanism_targets") or None,
                               fidelity=dict(approval=d.get("fid_approval"), approval_baseline=d.get("fid_approval_base"), mechanism=d.get("mech_tier"),
                                             mechanism_families=d.get("mech_families"), mechanism_activity=d.get("mech_activity") or None, group=d.get("group_fid"),
                                             group_proposal=d.get("group_proposal"), approval_without_letters=d.get("fid_approval_noletter")) if "src" in d else None,
                               approval_letters=d.get("letters") or None, scope_indication_first_letter=d.get("scope_first_letter"),
                               outside_sources=dict(
                                   drugcentral=dict(match=d["src"]["dc_match"], structures=d["src"]["dc_structs"][:3], unii=d["src"]["unii"], approvals=d["src"]["approvals"],
                                                    curated_indications=d["src"]["indications"][:8], mechanism=d["src"]["dc_moa"][:8]),
                                   iuphar=dict(match=d["src"]["gt_match"], ligands=d["src"]["gt_ligands"][:3], approved=d["src"]["gt_approved"], primary_targets=d["src"]["gt_targets"][:8],
                                               citations=d["src"].get("gt_citations") or None),
                                   openfda=dict(label_application_numbers=d.get("label_apps"), scope_linked_records=(d.get("approval") or {}).get("scope_linked_records")),
                                   name_only_candidates_not_credited=d.get("src_weak")) if "src" in d else None,
                               reference_pages=d.get("reference_pages") or None,
                               reference_claims=d.get("reference_claims") or None,
                               trials_active=sum(n["attrs"]["trials_active"] for n in rnodes),
                               regimens=len(rnodes)),
                    provenance=dict(source="OT clinical_report + clinical_tracker.db + openFDA", table="indications", key=[m["indication_id"] for m in d["members"]][:3]),
                    children=rnodes)

    byc = defaultdict(list)
    for d in sorted(roots.values(), key=lambda x: x["name"]):
        byc[d["category"]].append(d)

    def branch(bid, title, note, children, prov=None):
        return dict(id=bid, kind="branch", name=title, attrs=dict(note=note), provenance=prov or {}, children=children)

    at_scope = byc["APPROVED_AT_SCOPE"]
    us_ok = [d for d in at_scope if "US" in d["jurisdictions"]]
    non_us = [d for d in at_scope if "US" not in d["jurisdictions"]]
    # A broader-term approval (e.g. "acute leukemia", "leukemia") counts as approved for the scope only when some regulator text or
    # curated indication names the scope disease itself; the rest are approved for another disease and are cut from the tree
    # (listed by name in meta.excluded_approved_elsewhere so the cut stays auditable).
    elsewhere_ds = [d for d in byc["APPROVED_BROADER_TERM"] if not d["scope_named_by"]] + byc["EXCLUDED_NOT_US_SCOPE"]
    supp_ds = byc["SUPPLEMENTAL"]
    approved_ds = sorted(at_scope + [d for d in byc["APPROVED_BROADER_TERM"] if d["scope_named_by"]] + byc["LABEL_ONLY"], key=lambda x: x["name"])
    inv_ds = sorted(byc["INVESTIGATIONAL"], key=lambda x: (-STAGE_RANK.get(x["best_stage"], 0), x["name"]))

    DISCONTINUED_STATUS = {"TERMINATED", "WITHDRAWN", "SUSPENDED"}

    def drug_discontinued(d):
        """True if, at this drug's most-advanced scope-trial phase, every trial is Terminated/
        Withdrawn/Suspended and none remain active. Drug-level, live AACT overall_status."""
        trials = [(PHASE_RANK.get(meta.get(n, {}).get("phase"), -1), meta.get(n, {}).get("overall_status"))
                  for rg in regimens.values() if d["root"] in rg["roots"] for n in rg["trials"]]
        if not trials:
            return False
        best = max(r for r, _ in trials)
        at_best = [s for r, s in trials if r == best]
        return not any(s in ACTIVE for s in at_best) and any(s in DISCONTINUED_STATUS for s in at_best)

    # Top-level categorization is Approved, then the AML trial landscape by clinical stage (Phase 3 down to Early
    # Phase 1, sorted most-advanced first) - not mutation/mechanism group. The mechanism-group layer was removed
    # from the tree structure: mechanism data proved unreliable as a CATEGORY (see resolve_group's potency-rank
    # gate, added after sorafenib/sunitinib - broad multi-kinase inhibitors, not FLT3-directed AML therapy - were
    # found bucketed as "FLT3" solely because ChEMBL lists that interaction among 9+ others). The underlying
    # mechanism data (mutation_group, mechanism_targets, fidelity) is still computed and still shown on every drug
    # node - it is a claim about that DRUG, not a claim about which branch of the tree it belongs in.
    STAGE_LABEL = {"PHASE_3": "Phase 3", "PHASE_2_3": "Phase 2/3", "PHASE_2": "Phase 2", "PHASE_1_2": "Phase 1/2",
                  "PHASE_1": "Phase 1", "EARLY_PHASE_1": "Early Phase 1", "UNKNOWN": "Unknown stage"}
    branches = [
        branch("branch:approved", f"Approved ({len(approved_ds)})",
               f"US approval for {scope_name}: a current US label names it as a treatment indication AND an FDA approval letter names it. "
               "Other regulators are reference only (reference_jurisdictions). Backbone chemotherapy and supportive/conditioning agents are "
               "in the Supplemental branch.",
               [drug_node(d) for d in approved_ds], dict(source="openFDA label + Drugs@FDA approval letters (live); US gate rules")),
        branch("branch:supplemental", f"Supplemental - backbone, supportive, conditioning, regimen partners ({len(supp_ds)})",
               "Not approved drugs in their own right for this network. Referenced only as components of approved-drug regimens.",
               [drug_node(d) for d in sorted(supp_ds, key=lambda x: x["name"])], dict(source="US gate rules (BACKBONE_DRUGS, SUPPORTIVE_DRUGS, partner_in)")),
    ]
    stage_map = defaultdict(list)
    disc_map = defaultdict(list)
    for d in inv_ds:
        stage = d["best_stage"] if d["best_stage"] in STAGE_LABEL else "UNKNOWN"
        (disc_map if drug_discontinued(d) else stage_map)[stage].append(d)
    for stage in STAGE_ORDER:
        ds = stage_map.get(stage)
        if ds:
            branches.append(branch(f"branch:stage:{stage}", f"{STAGE_LABEL[stage]} ({len(ds)})",
                                   f"No approval evidence at the scope, on an ancestor term, or in a live US label. Open Targets' best clinical "
                                   f"stage for this drug at {scope_name} specifically.",
                                   [drug_node(d) for d in sorted(ds, key=lambda x: x["name"])], dict(source="clinical_tracker.db")))
        dds = disc_map.get(stage)
        if dds:
            branches.append(branch(f"branch:stage:{stage}:discontinued", f"{STAGE_LABEL[stage]} – Discontinued ({len(dds)})",
                                   f"Reached {STAGE_LABEL[stage]} in a {scope_name} scope trial, then stopped: every trial at that phase is now "
                                   "Terminated, Withdrawn or Suspended (live AACT overall_status), none still active. Phase label is the stage "
                                   "reached before stopping, not a live trial.",
                                   [drug_node(d) for d in sorted(dds, key=lambda x: x["name"])],
                                   dict(source="clinical_tracker.db + live AACT overall_status")))

    # drugs seen in scope-trial arms but not among OT scope rows
    seen_roots = set().union(*[rg["roots"] for rg in regimens.values()]) if regimens else set()
    extra = sorted(seen_roots - set(roots), key=lambda r: di.root_name(r))
    extra_nodes = []
    for r in extra:
        d = dict(root=r, name=di.root_name(r), members=[dict(chembl_id=m, name=di.name.get(m, m), stage="n/a", approved=False, merge=di.merge.get(m, "self"), indication_id=None) for m in di.members[r][:2]],
                 best_stage="n/a", ot_approved=False, evidence=ev_reg.get(r, []), category="NOT_IN_OT_SCOPE_SET", jurisdictions=[], partner_in=[])
        extra_nodes.append(drug_node(d))
    branches.append(branch("branch:notinot", f"Seen in {scope_name} trial arms but not in Open Targets' scope set ({len(extra_nodes)})",
                           "Drugs found by parsing AACT arms that Open Targets does not list at this node - a coverage check in the other direction.", extra_nodes))
    ugroups = defaultdict(list)
    for rg in regimens.values():
        if rg["unmapped"]:
            n = regimen_node("unres", rg, meta)
            n["attrs"]["unmapped_class"] = di.classify_unresolved(rg["unmapped"][0])
            ugroups[n["attrs"]["unmapped_class"]].append(n)
    n_unres = sum(len(v) for v in ugroups.values())
    ustages = [dict(id=f"unres:{i}", kind="stage", name=f"{cls} ({len(v)})", attrs=dict(unmapped_class=cls),
                    provenance=dict(source="DrugIndex.classify_unresolved (deterministic)"),
                    children=sorted(v, key=lambda n: -n["attrs"]["trials_total"]))
               for i, (cls, v) in enumerate(sorted(ugroups.items(), key=lambda kv: -len(kv[1])))]
    branches.append(branch("branch:unresolved", f"Regimens with unmapped components ({n_unres})",
                           "Intervention strings that resolve to no unique Open Targets drug, grouped by WHY. Only 'regimen descriptor' and 'internal code' need curation; nothing is dropped.",
                           ustages))

    # NPM1 branch: regimens (all trials in this set listed)
    sub_reg_nodes = [regimen_node("npm1", rg, sub_meta) for rg in sub_reg.values()]
    act_nodes = [n for n in sub_reg_nodes if n["attrs"]["trials_active"] > 0]
    off_nodes = [n for n in sub_reg_nodes if n["attrs"]["trials_active"] == 0]
    key = lambda n: (n["attrs"]["category"] == "INVESTIGATIONAL_REGIMEN", -n["attrs"]["trials_total"], n["name"])
    npm1 = branch("branch:subtype", f"{args.subtype_text}-tagged trials in AACT ({len(sub_ncts)}) - by regimen",
                  f"Text match on condition/keyword/title for '{args.subtype_text}'. Regimens come from arm contents; categories compare each regimen to live US label clauses.",
                  [dict(id="npm1:active", kind="stage", name=f"Regimens in currently ACTIVE trials ({len(act_nodes)})", attrs={}, provenance=dict(source="live AACT"), children=sorted(act_nodes, key=key)),
                   dict(id="npm1:inactive", kind="stage", name=f"Regimens only in completed / terminated / other trials ({len(off_nodes)})", attrs={}, provenance=dict(source="live AACT"), children=sorted(off_nodes, key=key))],
                  dict(source="live AACT", query_hash=aact_hash))
    branches.append(npm1)

    def bucket_node(prefix, title, ids, source_meta):
        return dict(id=f"{prefix}", kind="note", name=f"{title} ({len(ids)})", attrs=dict(ncts=sorted(ids)[:400]), provenance=dict(source="live AACT"), children=[])
    audit = branch("branch:audit", "Trial-placement audit (nothing dropped)",
                   "Every scope trial is either inside a regimen node above or listed in one of these buckets.",
                   [bucket_node("audit:no_arms", "scope trials with no design groups (arms)", buckets["no_arms"], meta),
                    bucket_node("audit:no_links", "scope trials whose arms have no linked interventions", buckets["arms_without_intervention_links"], meta),
                    bucket_node("audit:ctl", "scope trials with only placebo-containing or non-drug arms", buckets["control_or_non_drug_only"], meta),
                    bucket_node("audit:apl_only", "APL-only trials held out of the AML tree (separate APL chart)", apl_ncts, meta),
                    bucket_node("audit:npm1_unplaced", f"{args.subtype_text} trials with no arms / no links / control-only",
                                sub_buckets["no_arms"] | sub_buckets["arms_without_intervention_links"] | sub_buckets["control_or_non_drug_only"], sub_meta)])
    branches.append(audit)

    # categorisation review = every place sources disagree (mirrors, never mutates)
    review = []
    for d in roots.values():
        reasons = []
        if d["category"] == "APPROVED_BROADER_TERM":
            reasons.append(f"BROADER_TERM_APPROVAL ({'/'.join(d['jurisdictions'])})" + (" - US label has no scope mention" if d.get("us_label_scope_mention") is False else ""))
        if d["category"] == "LABEL_ONLY":
            reasons.append("LABEL_ONLY_OT_MISSING")
        if d["category"] == "APPROVED_AT_SCOPE" and "US" in d["jurisdictions"] and d.get("us_label_scope_mention") is False:
            reasons.append("OT_US_APPROVED_LABEL_LACKS_TERM")
        if d["category"] == "APPROVED_AT_SCOPE" and "US" not in d["jurisdictions"]:
            reasons.append("NON_US_APPROVAL_ONLY")
        if d.get("us_label_supportive_only"):
            reasons.append("SUPPORTIVE_CARE_LABEL_MENTION (US label names the disease only as a supportive-care setting; not treated as approval)")
        if d["category"] == "INVESTIGATIONAL" and d["partner_in"]:
            reasons.append("APPROVED_ONLY_AS_PARTNER: " + "; ".join(d["partner_in"])[:120])
        for r in reasons:
            review.append(dict(id=f"review:{d['root']}:{r[:14]}", kind="drug", name=f"{d['name']}  \u00b7  {r}",
                               attrs=dict(category=d["category"], jurisdictions=d["jurisdictions"], reason=r, mirrors=f"drug:{d['root']}",
                                          us_label_scope_mention=d.get("us_label_scope_mention"), approved_as_partner_in=d.get("partner_in"),
                                          approval_evidence=d["evidence"][:6]),
                               provenance=dict(source="categorisation rules"), children=[]))
    branches.append(branch("branch:review", f"Categorisation review - where sources disagree ({len(review)})",
                           "Shown, never resolved automatically.", sorted(review, key=lambda n: n["name"])))

    # High-level categorization is Open Targets-linked AML drugs only: Approved vs Investigational.
    # branch:subtype (raw AACT text-search for the subtype term, never gated by an OT indication link)
    # and the notinot/unresolved/audit/review branches (drugs or components Open Targets does not list,
    # plus pure trial-placement bookkeeping) are not a trusted drug category - if it isn't in Open Targets'
    # scope set, it doesn't belong in the categorization shown here. They're still computed above and
    # checked by the invariants below (nothing about correctness-checking is lost), just not rendered.
    non_top_level_ids = {"branch:notinot", "branch:unresolved", "branch:audit", "branch:review", "branch:subtype"}
    branches = [b for b in branches if b["id"] not in non_top_level_ids]
    tree = dict(id="root", kind="root", name=args.label, attrs={}, provenance={}, children=branches)

    # ------------------------------------------------------------------ fidelity summary (measured: baseline sources vs everything)
    ap_roots = [d for d in roots.values() if d["category"] != "INVESTIGATIONAL"]
    inv_roots = [d for d in roots.values() if d["category"] == "INVESTIGATIONAL"]
    ref_ok = lambda d: any(c.get("date_matches") for c in d.get("reference_claims") or [])
    dated_record = lambda d: bool(d["src"]["approvals"]) or bool((d.get("approval") or {}).get("first_us_approval"))

    def prior_group(d):
        names = {m["name"].upper() for m in d["members"]} | {d["name"].upper()}
        return next((claim_group_by_name[n] for n in names if n in claim_group_by_name), moa_group.get(d["root"], "General"))

    tier_count = lambda key: dict(sorted(Counter(d[key]["tier"] for d in ap_roots).items()))
    claims_all = {c["id"]: c for d in cand for c in d.get("reference_claims") or []}
    claims_summary = dict(
        total=len(claims_all), verified_by_page=sum(1 for c in claims_all.values() if c["date_matches"]),
        verified_by_drugsfda_record=sum(1 for c in claims_all.values() if c.get("record_date_matches")),
        verified_by_approval_letter=sum(1 for c in claims_all.values() if c.get("letter_date_matches")),
        verified_by_any=sum(1 for c in claims_all.values() if c["date_matches"] or c.get("record_date_matches") or c.get("letter_date_matches")),
        unverified=[c["id"] for c in claims_all.values() if not (c["date_matches"] or c.get("record_date_matches") or c.get("letter_date_matches"))])
    all_letters = [l for d in roots.values() for l in d["letters"]]
    letters_summary = dict(read=len(all_letters), text_ok=sum(1 for l in all_letters if l["status"] == "text"), no_text_layer=sum(1 for l in all_letters if l["status"] == "no_text"),
                           fetch_failed=sum(1 for l in all_letters if l["status"] == "fetch_failed"), naming_scope=sum(1 for l in all_letters if l["mentions"]),
                           drugs_with_a_letter_naming_scope=sum(1 for d in roots.values() if any(l["mentions"] for l in d["letters"])),
                           approved_with_linked_application_but_no_readable_letter=sorted(d["name"] for d in ap_roots if (d.get("approval") or {}).get("scope_linked_records") and not any(l["mentions"] for l in d["letters"])))
    ge2 = lambda key: sum(1 for d in ap_roots if len(d[key]["families"]) >= 2)
    group_deltas = [dict(drug=d["name"], before=prior_group(d), after=d["mutation_group"], basis=d["mutation_group_basis"]) for d in roots.values() if prior_group(d) != d["mutation_group"]]
    dc_meths = lambda ds: dict(Counter(m for d in ds for m in d["src"]["dc_match"]))
    gt_meths = lambda ds: dict(Counter(m for d in ds for m in d["src"]["gt_match"]))
    fid_summary = dict(
        approved_drugs=len(ap_roots), investigational_drugs=len(inv_roots),
        approval_tier_baseline=tier_count("fid_approval_base"), approval_tier_integrated=tier_count("fid_approval"),
        approval_tier_integrated_without_letters=tier_count("fid_approval_noletter"),
        approved_with_2plus_origin_families=dict(baseline=ge2("fid_approval_base"), integrated=ge2("fid_approval"), integrated_without_letters=ge2("fid_approval_noletter")),
        claims=claims_summary, approval_letters=letters_summary,
        reference=dict(
            curated_page_claim=sum(1 for d in ap_roots if d.get("reference_claims")),
            curated_page_date_verified=sum(1 for d in ap_roots if ref_ok(d)),
            outside_dated_regulator_record=sum(1 for d in ap_roots if dated_record(d)),
            curated_indication_agrees=sum(1 for d in ap_roots if d["src"]["indications"]),
            any_reference_grade_evidence=sum(1 for d in ap_roots if ref_ok(d) or dated_record(d) or d["src"]["indications"]),
            indication_level_without_letters=sum(1 for d in ap_roots if ref_ok(d) or d["src"]["indications"]),
            indication_level_with_letters=sum(1 for d in ap_roots if ref_ok(d) or d["src"]["indications"] or any(l["mentions"] for l in d["letters"])),
            scope_indication_first_approval_date_from_letters=sum(1 for d in ap_roots if d.get("scope_first_letter"))),
        mechanism_tier=dict(
            all_drugs=dict(baseline=dict(Counter(d["mech_tier_baseline"] for d in roots.values())), integrated=dict(Counter(d["mech_tier"] for d in roots.values()))),
            approved=dict(baseline=dict(Counter(d["mech_tier_baseline"] for d in ap_roots)), integrated=dict(Counter(d["mech_tier"] for d in ap_roots))),
            investigational=dict(baseline=dict(Counter(d["mech_tier_baseline"] for d in inv_roots)), integrated=dict(Counter(d["mech_tier"] for d in inv_roots)))),
        general_group_without_mechanism_data=dict(
            baseline=sum(1 for d in roots.values() if d["mutation_group"] == "General" and d["mech_tier_baseline"] == "no data"),
            integrated=sum(1 for d in roots.values() if d["mutation_group"] == "General" and d["mech_tier"] == "no data")),
        general_group_without_curated_mechanism=dict(
            baseline=sum(1 for d in roots.values() if d["mutation_group"] == "General" and d["mech_tier_baseline"] == "no data"),
            integrated=sum(1 for d in roots.values() if d["mutation_group"] == "General" and d["mech_tier"] in ("no data", "activity-only"))),
        activity_only_drugs=sorted(d["name"] for d in roots.values() if d["mech_tier"] == "activity-only"),
        group_fidelity=dict(Counter(d["group_fid"]["level"] for d in roots.values())),
        group_deltas=group_deltas,
        group_proposals=[dict(drug=d["name"], proposed=d["group_proposal"]["group"], symbol=d["group_proposal"]["symbol"], families=d["group_proposal"]["families"],
                              total_targets_listed=d["group_proposal"]["n_targets"], reason=d["group_proposal"]["reason"]) for d in roots.values() if d.get("group_proposal")],
        excluded_approved_elsewhere=sorted(d["name"] for d in elsewhere_ds),
        excluded_not_us_scope=[dict(name=d["name"], source_category=d.get("source_category"), reason=d.get("us_gate"),
                                    reference_jurisdictions=d.get("reference_jurisdictions", [])) for d in byc["EXCLUDED_NOT_US_SCOPE"]],
        supplemental=[dict(name=d["name"], reason=d.get("us_gate"), partner_in=d.get("partner_in", [])) for d in supp_ds],
        label_mention=dict(
            approved_gained_by_ontology_terms=sorted(d["name"] for d in cand if d["category"] != "INVESTIGATIONAL" and not d["label"]["treatment_mentions"] and d["label_onto"]["treatment_mentions"]),
            investigational_that_would_become_label_only=sorted(d["name"] for d in cand if d["category"] == "INVESTIGATIONAL" and d["label_onto"]["treatment_mentions"])),
        matches=dict(
            drugcentral=dict(approved=f"{sum(1 for d in ap_roots if d['src']['dc_structs'])}/{len(ap_roots)}", investigational=f"{sum(1 for d in inv_roots if d['src']['dc_structs'])}/{len(inv_roots)}",
                             methods=dc_meths(roots.values())),
            iuphar=dict(approved=f"{sum(1 for d in ap_roots if d['src']['gt_ligands'])}/{len(ap_roots)}", investigational=f"{sum(1 for d in inv_roots if d['src']['gt_ligands'])}/{len(inv_roots)}",
                        methods=gt_meths(roots.values())),
            unmatched_approved_in_drugcentral=[d["name"] for d in ap_roots if not d["src"]["dc_structs"]],
            name_only_candidates_not_credited=dict(drugcentral=sum(1 for d in roots.values() if d["src_weak"]["drugcentral"]), iuphar=sum(1 for d in roots.values() if d["src_weak"]["iuphar"]))),
        sources=dict(drugcentral_fetched_at=src_meta.get("drugcentral_fetched_at"), drugcentral_latest_approval_by_agency=json.loads(src_meta.get("drugcentral_latest_approval_by_agency", "{}")),
                     gtopdb_version=(src_meta.get("gtopdb_version") or "").strip('# ').strip(), ontology_terms=n_onto_terms, ontology_nodes=n_onto_nodes,
                     openfda_key_used=bool(openfda_key), openfda_label_calls_this_run=labels.network_calls, openfda_approval_calls_this_run=approvals.network_calls),
    )

    # ------------------------------------------------------------------ invariants + expectations
    all_nodes = []
    def walk(n):
        all_nodes.append(n)
        for c in n["children"]:
            walk(c)
    walk(tree)
    placed = set()
    for rg in regimens.values():
        placed |= set(rg["trials"])
    accounted = placed | buckets["no_arms"] | buckets["arms_without_intervention_links"] | buckets["control_or_non_drug_only"]
    n_members = sum(len(d["members"]) for d in roots.values())
    cat_sum = sum(len(v) for v in byc.values())
    # US gate: the named regulator source is the FDA approval letter plus the US label treatment mention (OT evidence is optional).
    no_src = [d["name"] for d in roots.values() if d["category"] == "APPROVED_AT_SCOPE"
              and not (d.get("scope_first_letter") and (d.get("label") or {}).get("treatment_mentions"))]
    bad_nct = [n["name"] for n in all_nodes if n["kind"] == "trial" and not NCT_RE.match(n["name"])]
    golden = json.load(open(os.path.join(TESTS_IN, "aml_golden.json"), encoding="utf-8"))
    dup_ids = len(all_nodes) - len({n["id"] for n in all_nodes})
    struct_bad, part_bad = [], []
    def check_struct(n, parent):
        if n["kind"] == "trial" and not (parent and parent["kind"] == "program"):
            struct_bad.append(n["id"])
        if n["kind"] == "program" and not (parent and parent["kind"] == "regimen"):
            struct_bad.append(n["id"])
        if n["kind"] == "regimen":
            listed = sorted(t["name"] for p in n["children"] for t in p["children"])
            if listed != sorted(n["attrs"]["all_trial_ids"]):
                part_bad.append(n["id"])
        for c in n["children"]:
            check_struct(c, n)
    check_struct(tree, None)
    no_year = [n["name"] for n in all_nodes if n["kind"] == "trial" and n["attrs"]["start_date"] and not n["attrs"]["start_year"]]
    top_ids = [b["id"] for b in tree["children"]]

    def root_by_name(nm):
        return next((d for d in roots.values() if d["name"].upper() == nm.upper()), None)
    ven, dec, ced, ena = root_by_name("VENETOCLAX"), root_by_name("DECITABINE"), root_by_name("CEDAZURIDINE"), root_by_name("ENASIDENIB")
    def one_root(n):
        k, ids = di.lookup(n)
        assert k == "one" and len(ids) == 1, f"expectation helper: '{n}' did not resolve uniquely ({k})"
        return next(iter(ids))
    rk = lambda names: frozenset(one_root(n) for n in names)
    r_ven_aza = rk(["venetoclax", "azacitidine"])
    def regs_with(nct):
        return [rg for rg in regimens.values() if nct in rg["trials"]] + [rg for rg in sub_reg.values() if nct in rg["trials"]]
    astx_regs = regs_with("NCT04657081")
    viale = regs_with("NCT02993523")
    expectations = [
        ("decitabine_partner_only_eu_reference", bool(dec) and dec["category"] == "SUPPLEMENTAL" and "EU" in dec["reference_jurisdictions"],
         f"category={dec and dec['category']} ref_jur={dec and dec.get('reference_jurisdictions')} gate={dec and dec.get('us_gate')}"),
        ("cedazuridine_us_approved", bool(ced) and ced["category"] == "APPROVED_AT_SCOPE" and ced["jurisdictions"] == ["US"],
         f"category={ced and ced['category']} jur={ced and ced['jurisdictions']} gate={ced and ced.get('us_gate')}"),
        ("venetoclax_us_approved", bool(ven) and ven["category"] == "APPROVED_AT_SCOPE" and r_ven_aza in approved_sets,
         f"category={ven and ven['category']} approved venetoclax+azacitidine regimen from label={r_ven_aza in approved_sets}"),
        ("enasidenib_parent_salt_merge", bool(ena) and len(ena["members"]) >= 2 and ena["category"] == "APPROVED_AT_SCOPE",
         f"members={ena and [m['name'] for m in ena['members']]} category={ena and ena['category']}"),
        ("ascertain_v_regimen", any(rg["roots"] == frozenset(di.lookup("ASTX727")[1]) | rk(["venetoclax"]) and regimen_category(rg)[0] == "APPROVED_REGIMEN_US_LABEL" for rg in astx_regs),
         f"regimens for NCT04657081={[ ' + '.join(sorted(di.root_name(x) for x in rg['roots'])) + ' -> ' + regimen_category(rg)[0] for rg in astx_regs]}"),
        ("viale_a_placebo_arm_excluded", any(rg["roots"] == r_ven_aza for rg in viale) and not any(rg["roots"] == rk(["azacitidine"]) for rg in viale),
         f"regimens for NCT02993523={[ ' + '.join(sorted(di.root_name(x) for x in rg['roots'])) for rg in viale]}"),
        ("resolver_rejects_lornoxicam_noise",
         di.lookup("Cyclophosphamide")[0] == "one" and di.root_name(next(iter(di.lookup("Cyclophosphamide")[1]))).upper().startswith("CYCLOPHOSPHAMIDE"),
         f"'Cyclophosphamide' -> {di.lookup('Cyclophosphamide')[0]} {[di.root_name(x) for x in di.lookup('Cyclophosphamide')[1]]}"),
        ("decitabine_and_cedazuridine_string_resolves",
         di.lookup("Decitabine and Cedazuridine")[0] == "combo" and di.lookup("Decitabine and Cedazuridine")[1] == frozenset(di.lookup("ASTX727")[1]) | set(),
         f"'Decitabine and Cedazuridine' -> {[di.root_name(x) for x in di.lookup('Decitabine and Cedazuridine')[1]]}"),
        ("supportive_care_not_an_approved_regimen", rk(["filgrastim"]) not in approved_sets,
         f"filgrastim regimen in approved sets = {rk(['filgrastim']) in approved_sets}"),
        ("oral_azacitidine_setting_is_tagged",
         rk(["azacitidine"]) in approved_sets and any("maintenance" in " ".join(e.get("setting", [])) for e in approved_sets[rk(["azacitidine"])]),
         f"azacitidine label evidence settings={[e.get('setting') for e in approved_sets.get(rk(['azacitidine']), [])]} brands={[e.get('brand') for e in approved_sets.get(rk(['azacitidine']), [])]}"),
        ("cpx351_resolves_to_vyxeos_components",
         di.lookup("CPX-351")[0] == "combo" and di.lookup("CPX-351")[1] == set(rk(["daunorubicin", "cytarabine"])),
         f"'CPX-351' -> {di.lookup('CPX-351')[0]} {[di.root_name(x) for x in di.lookup('CPX-351')[1]]}; label products={[sorted(di.root_name(x) for x in p) for p in di.combo_products][:4]}"),
        ("apg115_resolves_via_aact_other_name",
         di.lookup("APG-115")[0] == "one" and "ALRIZOMADLIN" in {di.root_name(x).upper() for x in di.lookup("APG-115")[1]},
         f"'APG-115' -> {di.lookup('APG-115')[0]} via AACT other name '{di.alias_via.get('APG-115')}'"),
    ]
    cri, his, rev = root_by_name("CRISANTASPASE"), root_by_name("HISTAMINE"), root_by_name("REVUMENIB")
    his_role = (his or {}).get("therapeutic_role") or {}
    approved_names = {d["name"].upper() for d in approved_ds}
    expectations += [
        ("crisantaspase_approved_elsewhere_not_scope", bool(cri) and cri in elsewhere_ds and cri not in approved_ds,
         f"crisantaspase category={cri and cri['category']} scope_named_by={cri and cri['scope_named_by']} "
         f"ema={[e['medicine'] + ': ' + e['sentence'][:80] for e in (cri or {}).get('ema_indications', [])]}"),
        ("histamine_maintenance_with_interleukin2",
         his_role.get("setting") == "maintenance" and his_role.get("combination") == "combination required"
         and any("interleukin-2" in p.lower() for p in (his_role.get("partners") or [])),
         f"histamine role={his_role}"),
        ("revumenib_cedazuridine_approved_decitabine_supplemental", {"REVUMENIB", "CEDAZURIDINE"} <= approved_names
         and "DECITABINE" in {d["name"] for d in supp_ds},
         f"missing={sorted({'REVUMENIB', 'CEDAZURIDINE'} - approved_names)}; decitabine supplemental={'DECITABINE' in {d['name'] for d in supp_ds}}"),
    ]
    expectations += [
        ("inqovi_brand_and_2020_us_approval",
         bool(ced) and any(b["brand"].upper() == "INQOVI" for b in ced["approval"]["brands"]) and (ced["approval"]["first_us_approval"] or {}).get("date") == "2020-07-07",
         f"cedazuridine brands={[b['brand'] for b in (ced or {}).get('approval', {}).get('brands', [])]} first_us_approval={(ced or {}).get('approval', {}).get('first_us_approval')}"),
        ("decitabine_dacogen_2006_us_approval",
         bool(dec) and any(b["brand"].upper() == "DACOGEN" for b in dec["approval"]["brands"]) and (dec["approval"]["first_us_approval"] or {}).get("date") == "2006-05-02",
         f"decitabine brands={[b['brand'] for b in (dec or {}).get('approval', {}).get('brands', [])][:4]} first_us_approval={(dec or {}).get('approval', {}).get('first_us_approval')}"),
        ("inqovi_is_the_scope_labeled_product_for_cedazuridine",
         bool(ced) and (ced["approval"].get("us_approval_scope_labeled") or {}).get("brand", "").upper() == "INQOVI" and ced["approval"]["us_approval_scope_labeled"]["date"] == "2020-07-07",
         f"cedazuridine us_approval_scope_labeled={(ced or {}).get('approval', {}).get('us_approval_scope_labeled')}"),
        ("histamine_products_not_claimed_as_scope_brands",
         root_by_name("HISTAMINE") is not None and not root_by_name("HISTAMINE")["approval"].get("us_approval_scope_labeled"),
         f"histamine scope-labeled={(root_by_name('HISTAMINE') or {}).get('approval', {}).get('us_approval_scope_labeled')} (Drugs@FDA histamine products are allergen/diagnostic, not AML)"),
        ("venetoclax_venclexta_2016_us_approval",
         bool(ven) and any(b["brand"].upper() == "VENCLEXTA" for b in ven["approval"]["brands"]) and (ven["approval"]["first_us_approval"] or {}).get("date") == "2016-04-11",
         f"venetoclax brands={[b['brand'] for b in (ven or {}).get('approval', {}).get('brands', [])][:3]} first_us_approval={(ven or {}).get('approval', {}).get('first_us_approval')}"),
    ]
    # Each claim's date is corroborated by up to 3 independent openFDA-native documents (Drugs@FDA application record,
    # its approval letter) in addition to the hand-curated reference page - see claims_all / the "approval letter"
    # rule in meta.rules. A dead or reorganised press page (fda_ivosidenib/olutasidenib/gemtuzumab genuinely 404;
    # fda_onureg serves FDA's "Page Not Found" template under HTTP 200, a soft-404 now detected in ReferenceSources)
    # no longer fails this check on its own when the SAME date is independently confirmed by the regulator's own
    # application record or its own approval letter - chasing replacement press-release URLs would be weaker
    # evidence than what openFDA already supplies for exactly this fact.
    ref_date_checks = []
    for c in pr_claims["claims"]:
        cc = claims_all.get(c["id"], {})
        page_date = (refs.pages.get(c["source_id"]) or {}).get("approval_date")
        verified_by = [n for n, ok in (("page", cc.get("date_matches")), ("Drugs@FDA record", cc.get("record_date_matches")), ("approval letter", cc.get("letter_date_matches"))) if ok]
        ref_date_checks.append((
            f"pr_claim_{c['id']}_approval_date",
            bool(verified_by),
            f"expected={c['approval_date']}; verified by: {', '.join(verified_by) or 'none'}; page {c['source_id']} approval_date={page_date}",
        ))
    forced = []
    for spec in pr_claims.get("must_not_be_forced", []):
        d1, bad = root_by_name(spec["drug"]), root_by_name(spec["must_not_contain"])
        if d1 and bad:
            for rg in regimens.values():
                if d1["root"] in rg["roots"] and bad["root"] in rg["roots"] and regimen_category(rg)[0] in ("APPROVED_REGIMEN_US_LABEL", "APPROVED_BACKBONE_PLUS_INVESTIGATIONAL"):
                    forced.append(f"{spec['id']}: {sorted(di.root_name(x) for x in rg['roots'])} -> {regimen_category(rg)[0]}")
    expectations += ref_date_checks + [("reference_pages_never_force_unclaimed_combination", not forced, "; ".join(forced) or "none")]

    got_golden, want = approved_member_names, set(golden["approved_names"])
    # mapping coverage over every drug-like intervention string seen in the arms we parsed
    all_seen = seen_names | sub_seen
    ok_exact = sum(1 for n in all_seen if n.upper() in di.exact_names)
    ok_new = sum(1 for n in all_seen if di.lookup(n)[0] in ("one", "combo"))

    def tag_nodes(kind, node=None):
        node = node or tree
        return ([node] if node.get("kind") == kind else []) + [x for c in node.get("children") or [] for x in tag_nodes(kind, c)]
    inv = [
        dict(name="drug indications at scope == sum of merged-drug members", ok=n_members == len(rows), detail=f"{n_members} members vs {len(rows)} OT indication rows"),
        dict(name="every drug lands in exactly one category", ok=cat_sum == len(roots), detail=f"{cat_sum} vs {len(roots)}"),
        dict(name=f"golden: OT-approved records equal your declared {len(want)} drugs", ok=got_golden == want,
             detail=f"missing: {sorted(want - got_golden)}; extra: {sorted(got_golden - want)}"),
        dict(name="every approved drug has a US label treatment mention and an FDA approval letter naming the scope", ok=not no_src, detail=str(no_src[:6] or "none")),
        dict(name="trial conservation: every scope trial is in a regimen or an audit bucket", ok=accounted == set(scope_ncts),
             detail=f"{len(accounted)} accounted vs {len(scope_ncts)} scope trials"),
        dict(name=f"{args.subtype_text} trial conservation", ok=(set().union(*[set(r['trials']) for r in sub_reg.values()]) | sub_buckets['no_arms'] | sub_buckets['arms_without_intervention_links'] | sub_buckets['control_or_non_drug_only']) == set(sub_ncts),
             detail=f"{len(sub_ncts)} trials"),
        dict(name="all trial node ids are valid NCT numbers", ok=not bad_nct, detail=str(bad_nct[:5] or "none")),
        dict(name="no duplicate node ids", ok=dup_ids == 0, detail=f"{dup_ids} duplicates"),
        dict(name="name mapping improves on exact-name matching", ok=ok_new >= ok_exact, detail=f"exact {ok_exact}/{len(all_seen)} -> resolved {ok_new}/{len(all_seen)}"),
        dict(name="openFDA corroboration completed", ok=not labels.rate_limited, detail=f"{labels.network_calls} network calls; rate-limited={labels.rate_limited}"),
        dict(name="Drugs@FDA lookup completed for every candidate", ok=not approvals.rate_limited and all(d["approval"]["status"] != "not_checked" for d in cand),
             detail=f"{approvals.network_calls} network calls; not_checked={[d['name'] for d in cand if d['approval']['status']=='not_checked'][:5]}"),
        dict(name="top-level categorization is Approved then clinical stage only (Open Targets-linked drugs; mutation/mechanism group, non-OT-scope and diagnostic branches are computed and checked but not shown as categories)",
             ok=top_ids[:2] == ["branch:approved", "branch:supplemental"] and all(b["id"].startswith("branch:stage:") for b in tree["children"][2:]), detail=str(top_ids)),
        dict(name="every approved-branch drug is one of the three approval categories and has a source naming the scope disease; no approved-elsewhere drug is in the tree; every stage-branch drug is INVESTIGATIONAL at that stage",
             ok=all(n["attrs"]["category"] == "APPROVED_AT_SCOPE" and n["attrs"]["jurisdictions"] == ["US"] for n in tree["children"][0]["children"]) and
                all(n["attrs"]["category"] == "SUPPLEMENTAL" for n in tree["children"][1]["children"]) and
                not ({d["root"] for d in elsewhere_ds} & {n["attrs"]["chembl_root"] for b in tree["children"] for n in b["children"]}) and
                all(n["attrs"]["category"] == "INVESTIGATIONAL" and (n["attrs"]["ot_best_stage"] in b["id"] or (n["attrs"]["ot_best_stage"] not in STAGE_LABEL and b["id"].endswith("UNKNOWN")))
                    for b in tree["children"][2:] for n in b["children"]),
             detail="category/stage tags vs branch"),
        dict(name="stage split partitions every OT-linked scope drug except the cut approved-elsewhere drugs exactly once (no drug lost or duplicated across branches)",
             ok=sorted(n["attrs"]["chembl_root"] for b in tree["children"] for n in b["children"]) == sorted(d["root"] for d in roots.values() if d not in elsewhere_ds),
             detail=f"{sum(len(b['children']) for b in tree['children'])} placed vs {len(roots)} total"),
        dict(name="discontinued split matches live AACT status: every drug in a ':discontinued' branch tests discontinued, every drug in its sibling stage branch does not",
             ok=all(drug_discontinued(d) for d in [x for v in disc_map.values() for x in v]) and not any(drug_discontinued(d) for d in [x for v in stage_map.values() for x in v]),
             detail=f"{sum(len(v) for v in disc_map.values())} discontinued vs {sum(len(v) for v in stage_map.values())} active-stage investigational drugs"),
        dict(name="hierarchy: regimen > program > NCT, programs partition each regimen's trials", ok=not struct_bad and not part_bad, detail=f"misplaced={struct_bad[:3]} non-partitioning regimens={part_bad[:3]}"),
        dict(name="every trial with an AACT start date carries a start year", ok=not no_year, detail=str(no_year[:5] or "none")),
        dict(name="outside-source credit is identifier-based only: no credited match came from a name",
             ok=all(not m.startswith("exact name") for d in roots.values() for m in d["src"]["dc_match"] + d["src"]["gt_match"]),
             detail=f"DrugCentral methods {dc_meths(roots.values())}; IUPHAR methods {gt_meths(roots.values())}"),
        dict(name="symbol-keyed mechanism groups reproduce the Open Targets name-keyed groups (no silent regrouping)",
             ok=not [d["name"] for d in roots.values() if d["root"] in moa_group and F.group_from_symbols(set(d["ot_symbols"]))[0] != moa_group[d["root"]]],
             detail=str([d["name"] for d in roots.values() if d["root"] in moa_group and F.group_from_symbols(set(d["ot_symbols"]))[0] != moa_group[d["root"]]][:5] or "none")),
        dict(name="group precedence only moves a drug OUT of a non-General group when the potency-rank gate explains why (never a silent overwrite)",
             ok=all(x["before"] == "General" or (root_by_name(x["drug"]) and sym_of_group.get(x["before"]) in (root_by_name(x["drug"]).get("mech_potency_demoted") or {})) for x in group_deltas),
             detail=str([x for x in group_deltas if x["before"] != "General"] or f"{len(group_deltas)} drugs moved out of General, all listed in meta.fidelity.group_deltas")),
        dict(name="adding sources never lowers an approval tier",
             ok=all(d["fid_approval"]["tier"] <= d["fid_approval_base"]["tier"] for d in ap_roots), detail=str([d["name"] for d in ap_roots if d["fid_approval"]["tier"] > d["fid_approval_base"]["tier"]][:5] or "none")),
        dict(name="every curated approval claim date is verified by at least one source (reference page, Drugs@FDA record or approval letter)",
             ok=not claims_summary["unverified"], detail=str(claims_summary["unverified"] or f"{claims_summary['verified_by_any']}/{claims_summary['total']} verified "
                                                              f"(page {claims_summary['verified_by_page']}, Drugs@FDA record {claims_summary['verified_by_drugsfda_record']}, letter {claims_summary['verified_by_approval_letter']})")),
        dict(name="potency-derived (activity-only) mechanism never originates a mutation/mechanism group",
             ok=not [d["name"] for d in roots.values() if d["mech_tier"] == "activity-only" and d["mutation_group_basis"].startswith("mechanism:")],
             detail=f"{sum(1 for d in roots.values() if d['mech_tier'] == 'activity-only')} activity-only drugs, none grouped by mechanism"),
        dict(name="openFDA API key never written to a cache file or the output",
             ok=not (openfda_key and (any(openfda_key in open(os.path.join(ROOT, "data", f), encoding="utf-8").read() for f in ("label_cache.json", "approval_cache.json")) or openfda_key in json.dumps(tree, default=str))),
             detail="key used for requests only" if openfda_key else "no key configured"),
        dict(name="quick tags: every drug tag is a known tag (optionally prefixed approved:/investigated:)",
             ok=all(re.sub(r"^(approved|investigated): ", "", t) in TAG_ORDER for n in tag_nodes("drug") for t in n["attrs"].get("tags") or []),
             detail=f"{sum(1 for n in tag_nodes('drug') if n['attrs'].get('tags'))} tagged drugs, {sum(1 for n in tag_nodes('trial') if n['attrs'].get('tags'))} tagged trial nodes"),
        dict(name="quick tags: investigational drugs carry no approved_for tags",
             ok=not [n["name"] for n in tag_nodes("drug") if n["attrs"].get("category") == "INVESTIGATIONAL" and n["attrs"].get("approved_for")],
             detail="approved_for comes only from regulator text of non-investigational drugs"),
        dict(name="quick tags: regex self-test (unfit never tagged fit, negated biomarker/setting dropped, exclusion text ignored)",
             ok=(quick_tags("newly diagnosed AML ineligible for intensive induction chemotherapy") == ["newly diagnosed", "unfit for intensive (non-intensive)", "1L / first line (inferred)"]
                 and "FLT3" not in quick_tags("FLT3-ITD negative AML") and "FLT3" not in quick_tags(inclusion_part("Inclusion Criteria: AML. Exclusion Criteria: FLT3 mutation"))
                 and not any("APL" in quick_tags(s) for s in ("AML (excluding APL)", "non-APL AML", "AML other than acute promyelocytic leukemia"))
                 and "APL" in quick_tags("newly diagnosed APL with PML-RARA")),
             detail="fixed sentences"),
    ] + [dict(name=f"expectation: {i}", ok=bool(ok), detail=d) for i, ok, d in expectations]

    tree["meta"] = dict(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        scope=dict(id=args.scope_id, name=scope_name), label=args.label, label_subtype=args.subtype_text,
        sources=dict(clinical_tracker_db_mtime=iso_mtime(db_path), open_targets_snapshot=iso_mtime(os.path.join(BULK, "clinical_indication.parquet")),
                     aact="live ctgov via AACTClient", ema=ema_prov, aact_query_hash=aact_hash, openfda="live label text, cached (v%d)" % LABEL_CACHE_VERSION),
        counts=dict(ot_indication_rows=len(rows), merged_drugs=len(roots), approved_us=len(us_ok), approved_non_us_only=len(non_us),
                    approved_broader_term=len(byc["APPROVED_BROADER_TERM"]), approved_elsewhere_not_scope=len(elsewhere_ds), label_only=len(byc["LABEL_ONLY"]), investigational=len(byc["INVESTIGATIONAL"]),
                    scope_trials=len(scope_ncts), scope_regimens=len(regimens), approved_regimens_from_labels=len(approved_sets),
                    subtype_trials=len(sub_ncts), unresolved_regimens=n_unres,
                    unresolved_by_class={c: len(v) for c, v in ugroups.items()},
                    programs=sum(1 for n in all_nodes if n["kind"] == "program"), explicit_trial_links=len(xlinks),
                    resolved_via_aact_other_names=len(di.aliases), label_defined_combination_products=len(di.combo_products),
                    name_mapping=f"exact {ok_exact}/{len(all_seen)} -> resolved {ok_new}/{len(all_seen)}",
                    reference_pages_configured=len(refs.pages), reference_pages_fetched_live=refs.network_calls,
                    reference_pr_claims=len(pr_claims["claims"]),
                    stage_branches={STAGE_LABEL.get(s, s): len(ds) for s, ds in stage_map.items()},
                    investigational_discontinued=sum(len(v) for v in disc_map.values()),
                    discontinued_by_stage={STAGE_LABEL.get(s, s): len(ds) for s, ds in disc_map.items()},
                    mutation_groups=(lambda: {g: dict(approved=sum(1 for d in approved_ds if d["mutation_group"] == g), investigational=sum(1 for d in inv_ds if d["mutation_group"] == g))
                                              for g in {d["mutation_group"] for d in roots.values()}})()),
        rules=[
            "1. Drugs merge by OT parentId, then a salt-suffix rule (e.g. DAUNORUBICIN HYDROCHLORIDE -> DAUNORUBICIN). Merge method is shown per member.",
            f"2. Approval evidence = OT regulatory/label reports (stage APPROVAL) tagged by regulator (DailyMed/FDA=US, EMA=EU, PMDA=JP) and by ontology relation to the scope (exact / descendant / ancestor within {NEAR_ANCESTOR_MAX} hops).",
            "3. Category precedence: APPROVED_AT_SCOPE > APPROVED_BROADER_TERM > LABEL_ONLY (live US label text) > INVESTIGATIONAL. Raw Open Targets stage is always shown.",
            "3b. An APPROVED_BROADER_TERM drug stays in Approved only if a US label, an authorised EMA product's indication text, or a DrugCentral curated indication names the scope disease; "
            "otherwise it goes to 'Approved for another disease' with the indication it is actually approved for. Each approved drug carries a therapeutic role "
            "(conditioning / supportive care / maintenance; monotherapy or combination, partner quoted) parsed from those scope-naming sentences.",
            "4. Approved regimens are parsed from live US label sentences naming the scope disease ('in combination with ...'); the sentence is kept as evidence.",
            "5. A trial's regimens come from arm CONTENTS. Arms containing a placebo are treated as controls, not regimens. Arm type labels are ignored (unreliable).",
            "6. Names resolve via OT names/synonyms/trade names/'component of' combo labels; ambiguity is refused. Unresolved strings are shown, not dropped.",
            "7. Top-level categorization is Approved, then the trial landscape by clinical stage (Phase 3 down to Early Phase 1, most-advanced first). Mutation/mechanism group (General, or a precision "
            "target such as FLT3/IDH1/IDH2/NPM1/BCL2/CD33/CD123/Hedgehog-SMO) is a tag shown on every drug, not a branch of the tree - see rule 11 and the potency-rank gate that decides it.",
            "8. Hierarchy: drug > regimen > program > NCT. A program groups the trials of one regimen linked by explicit AACT evidence (shared non-NIH ID under the same sponsor, or one description citing another NCT); otherwise by the same lead sponsor, flagged 'inferred'. Programs read in start-date order (Ph1 -> Ph3). Membership is a grouping aid: unless a program's link_basis is 'explicit' it means only that one sponsor ran these trials of this regimen, not a verified continuation.",
            "9. Years: trial start/completion dates and lead sponsor come from live AACT. Approval dates and US brand names come from live Drugs@FDA (original NDA/BLA approval, ANY indication, US only). Open Targets regulatory reports carry no date at all. Approval date for the scope indication itself is NOT available from these sources.",
            "10. Nothing in this file calls an LLM.",
            "11. A drug's mutation/mechanism group comes from (a) a reference-source PR/FDA-page approval claim (pr_expectations.json, independently fetched and keyword-searched, never merged into the OT/AACT/label evidence itself) if one is on file, else (b) a molecular target against a curated target list, gated by POTENCY RANK - the target must rank in the drug's own top-2 most potent measured targets (pooled DrugCentral + IUPHAR pKi/pKd/pIC50/pEC50), or the drug must have no potency data at all - else (c) General. A candidate that fails the gate is never assigned; it is recorded as a review proposal (mech_potency_demoted / group_proposal) with the reason and its actual rank. Basis is shown per drug (mutation_group_basis). It is a tag on the drug, not a branch of the tree (see rule 7).",
            "12. Fidelity counts independent ORIGIN FAMILIES, not databases: Open Targets US approval, openFDA label text and DailyMed are ONE family (the FDA SPL label); Open Targets mechanism and DrugCentral's moa_source=CHEMBL rows are ONE family (ChEMBL curation). "
            "Approval tier A = >=2 families (or 1 family + 2 confirmations), B = 1 family + 1 confirmation, C = 1 family alone; confirmations are a curated AML indication in DrugCentral and a dated regulator record for the same regulator. Mechanism is 'corroborated' when >=2 families list the same gene symbol. "
            "Outside sources join on ChEMBL ID, then InChIKey, then DrugBank ID; identical normalised name is the weakest key, used only when all identifiers fail. Each tier is computed with the pre-existing sources (baseline) and with everything (integrated); meta.fidelity holds both.",
            "13. Approval letters: for each approved drug, the Drugs@FDA applications that a scope-naming label points to (application-number join) have their approval letters (URLs taken from the database) fetched as PDF and keyword-searched with the ontology-derived scope terms. A letter naming the scope is its own origin family (same regulator as the label, a different document) and every tier is reported with and without it. "
            "Curated claim dates are also checked against Drugs@FDA application dates and letter dates, so a dead reference page no longer leaves a claim unverifiable. 'activity-only' is the lowest mechanism tier: no curated mechanism exists, but DrugCentral/IUPHAR potency rows at pActivity >= 7 do; it is never a mechanism claim and never assigns a group.",
            "14. Each stage branch under Investigational has a sibling ':discontinued' branch (drug level, live AACT). A drug is discontinued when, at the most-advanced phase any of its scope trials reached, every trial there is Terminated, Withdrawn or Suspended and none remain active (RECRUITING/NOT_YET_RECRUITING/ACTIVE_NOT_RECRUITING/ENROLLING_BY_INVITATION); Completed and Unknown status do not count. The stage label shown is the phase reached before stopping, not a claim the trial is still running."],
        alias_resolutions=dict(sorted(di.alias_via.items())),
        fidelity=fid_summary, invariants=inv, llm_in_data_path=False, runtime_seconds=round(time.time() - t0))
    attach_semantic(tree, aact, di)
    from lineage_verify import attach_lineage
    attach_lineage(tree)
    out = os.path.join(ROOT, "data", "tree.json")
    if os.path.exists(out):
        shutil.copy2(out, out + ".prev")
    json.dump(tree, open(out, "w", encoding="utf-8"), indent=1, ensure_ascii=False, default=str)
    print(f"wrote {out} ({os.path.getsize(out)//1024} KB)")
    print(json.dumps(tree["meta"]["counts"]))
    for i in inv:
        print(("PASS " if i["ok"] else "FAIL ") + i["name"] + ("" if i["ok"] else f"  -> {i['detail']}"))
    return 0 if all(i["ok"] for i in inv) else 2


STUDY_GROUP_CAP = 40
RELATION_ORDER = ["approval_lead", "approval_co_basis", "companion", "efficacy+safety", "efficacy", "safety", "long_term", "pk", "drug_interaction", "supportive", "mixed_heme", "supplemental", "related", "investigational_combination"]
LAYER_ORDER = {"aml_specific": 4, "myeloid": 3, "mixed_heme": 2, "basket": 1, "unscoped": 0}
PHASE_ORDER = {"PHASE4": 6, "PHASE3": 5, "PHASE2/PHASE3": 4, "PHASE2": 3, "PHASE1/PHASE2": 2, "PHASE1": 1, "EARLY_PHASE1": 0}


def study_node(pid, p):
    attrs = {k: p.get(k) for k in ("nct_id", "title", "phase", "status", "start_date", "relation", "expansion", "central_nct", "decision", "decided_by",
                                    "layer", "study_role", "arm_role", "regimen_key", "primary_outcomes", "regimen_prior", "regimen_next",
                                    "regimen_outcome", "why_stopped", "human_note", "reasons", "flagship_fails",
                                    "approval_basis", "approved_regimen", "central_exclusion", "approval_conflict", "demoted_from_approved")}
    attrs["tier"] = p.get("tier")
    return dict(id=f"{pid}:{p['nct_id']}", kind="study", name=f"{p['nct_id']}  ·  {p.get('study_role') or '?'}  ·  {p.get('layer') or '?'}",
                attrs=attrs, provenance=dict(source="tools/match_pipeline.py run (data/match.json)", basis="hard filters + BM25 + semantic layer V2 + human labels"))


# Approved-regimen fit: a companion trial stays under "Approved regimens" only if it tests the US-labelled setting and product.
FIT_SETTINGS = {"newly diagnosed", "relapsed/refractory", "maintenance", "consolidation"}
MAINT_RX = re.compile(r"mainten|post[- ]?remission", re.I)
# Drugs whose US AML label covers one product of the molecule only (Onureg = oral azacitidine; injectable Vidaza has no US AML label).
FORMULATION_RX = {"CHEMBL1489": re.compile(r"\boral\b|CC-?486|onureg|tablet", re.I)}


def approved_fit(p, drug_attrs, arm_texts):
    """-> None when the companion trial fits the drug's US-labelled AML use, else the reason it does not."""
    us = {k for k, src in (drug_attrs.get("approved_for") or {}).items() if any(s.startswith("US label") for s in src)} & FIT_SETTINGS
    if us:
        tags = set(p.get("setting_tags") or [])
        if MAINT_RX.search(p.get("title") or ""):
            tags.add("maintenance")
        if not tags & FIT_SETTINGS:
            return f"setting not stated (US label: {', '.join(sorted(us))})"
        if not tags & us:
            return f"setting {', '.join(sorted(tags & FIT_SETTINGS))} (US label: {', '.join(sorted(us))})"
    frx = FORMULATION_RX.get(drug_attrs.get("chembl_root"))
    if frx and not frx.search(f"{p.get('title') or ''} {arm_texts}"):
        return f"other formulation (US AML label: {drug_attrs.get('brand_display') or '?'})"
    return None


STANDARD_CHEMO_RX = re.compile(r"^\s*(standard|intensive|induction|consolidation|conventional|cytotoxic|\s|-|of|care)*\s*(chemotherapy|chemo)\b"
                               r"|^\s*standard of care\s*$|\b7\s*\+\s*3\b", re.I)


def text_roots(text, di):
    """Single-drug roots named anywhere in free text ('Ivosidenib combined with venetoclax and azacitidine'): exact index hits on 1-4 word n-grams."""
    words = raw(text).split()
    found = set()
    for size in (4, 3, 2, 1):
        for i in range(len(words) - size + 1):
            g = " ".join(words[i:i + size])
            if len(g) < 5:
                continue
            k, ids = di.lookup(g)
            if k == "one":
                found |= ids
    return found


def unlisted_partners(nct, croot, regimen_roots, arms, di, skip_roots):
    """Drug-like interventions sharing an arm with this drug that are not in its regimen key (the key lists recognised scope drugs only).
    A free-text intervention that names several drugs is scanned for known drug names, so its drugs count like separate interventions."""
    extra = set()
    for a in (arms.get(nct) or {}).values():
        ids_by_name = {}
        for t, n in a["ints"]:
            if t not in DRUG_LIKE or re.search(r"placebo|supportive care", n, re.I):
                continue
            k, ids = di.lookup(n)
            if k not in ("one", "combo") and not STANDARD_CHEMO_RX.search(n):
                ids = text_roots(n, di)
                k = "text" if ids else k
            ids_by_name[n] = (k, ids)
        if not any(croot in ids for k, ids in ids_by_name.values() if k in ("one", "combo", "text")):
            continue
        for n, (k, ids) in ids_by_name.items():
            if STANDARD_CHEMO_RX.search(n):  # generic standard chemo counts as a listed partner
                continue
            if k in ("one", "combo"):
                if ids - regimen_roots - skip_roots:
                    extra.add(n.strip())
            elif k == "text":
                extra |= {di.name.get(r, r).title() for r in ids - regimen_roots - skip_roots}
            else:
                extra.add(n.strip())
    return sorted(extra)


def attach_semantic(tree, aact=None, di=None):
    """Hang the semantic-layer view (data/match.json) under each drug: the central investigational study, then its supplemental
    studies grouped by relation. Add a Graveyard branch (data/graveyard.json) of pairs that failed a level of the logic.
    With aact + di, companions of an approved regimen are checked against the US label (setting, product) and the trial's arms
    (unlisted partner drugs) before they are shown as approved-regimen trials."""
    mpath, gpath = os.path.join(ROOT, "data", "match.json"), os.path.join(ROOT, "data", "graveyard.json")
    if not os.path.exists(mpath):
        return
    m = json.load(open(mpath, encoding="utf-8"))
    by_root, exp_root = defaultdict(list), defaultdict(list)
    for p in m["pairs"]:
        if apl_pair(p):
            continue
        if p.get("relation"):
            by_root[p["chembl_root"]].append(p)
        elif p.get("expansion"):
            exp_root[p["chembl_root"]].append(p)
    rank = lambda p: (LAYER_ORDER.get(p.get("layer"), 0), PHASE_ORDER.get(p.get("phase") or "", -1), p.get("start_date") or "")
    attached = 0
    comp_arms, skip_roots, fit_stats = {}, set(), Counter()
    # label attrs per drug, taken from whichever node carries them (pathway copies of a drug node may not)
    label_attrs = {}

    def collect(x):
        at = x.get("attrs") or {}
        if x.get("kind") == "drug" and at.get("approved_for"):
            label_attrs.setdefault(at.get("chembl_root"), at)
        for c in x.get("children") or []:
            collect(c)
    collect(tree)
    if aact is not None and di is not None:
        comp_arms = aact_arms(aact, sorted({p["nct_id"] for v in by_root.values() for p in v if p["relation"] == "companion"}))
        for name in SUPPORTIVE_DRUGS:
            k, ids = di.lookup(name)
            if k in ("one", "combo"):
                skip_roots |= ids

    def visit(n):
        nonlocal attached
        for c in n.get("children") or []:
            visit(c)
        if n.get("kind") != "drug":
            return
        croot = (n.get("attrs") or {}).get("chembl_root")
        exp = sorted(exp_root.get(croot, []), key=rank, reverse=True)
        if exp:
            eid = n["id"] + ":exp"
            eg = defaultdict(list)
            for p in exp:
                eg[p["expansion"]].append(p)
            n["children"] = [dict(id=eid, kind="study_set", name=f"Expansion ({len(exp)}): transplant / basket data for the larger thesis",
                                  attrs=dict(note="Not supplemental: these trials do not test this drug regimen's safety or efficacy in AML. "
                                                  "Kept because each has a registered primary outcome and the drug in its interventions.",
                                             by_kind={k: len(v) for k, v in eg.items()}),
                                  provenance=dict(source="data/match.json"),
                                  children=[dict(id=f"{eid}:{k}", kind="study_set", name=f"{k}  ({len(v)}{', top ' + str(STUDY_GROUP_CAP) if len(v) > STUDY_GROUP_CAP else ''})",
                                                 attrs=dict(studies=len(v), ncts=[p["nct_id"] for p in v]),
                                                 children=[study_node(f"{eid}:{k}", p) for p in v[:STUDY_GROUP_CAP]]) for k, v in sorted(eg.items())])] + (n.get("children") or [])
        rows = by_root.get(croot, [])
        central = next((p for p in rows if p["relation"] == "central"), None)
        if not central:
            return
        sid = n["id"] + ":sem"
        groups = defaultdict(list)
        for p in rows:
            if p is not central:
                groups[p["relation"]].append(p)
        fda = central.get("approval_basis")
        tag = f"FDA {fda['approval_date']} {central.get('approved_regimen')}" if fda else central.get("layer")
        cnode = dict(study_node(sid + ":central", central), name=f"CENTRAL  {central['nct_id']}  ·  {central.get('study_role')}  ·  {tag}")
        kids, cid = [cnode], sid + ":central"
        # Regimen groups hang under the central trial: approved regimens (approval trials + companions, one subgroup per
        # FDA-approved regimen, dated by its first approval), then never-approved combinations, one subgroup per regimen.
        appr, other_use, no_arms = defaultdict(list), defaultdict(list), []
        for rel in ("approval_lead", "approval_co_basis", "companion"):
            for p in groups.pop(rel, []):
                key = p.get("approved_regimen") or p.get("regimen_key") or "?"
                if rel == "companion" and di is not None:
                    if not any(a["ints"] for a in (comp_arms.get(p["nct_id"]) or {}).values()):
                        # No arm-to-intervention links in AACT: partner drugs cannot be checked, so the trial is not approval-regimen evidence.
                        no_arms.append(dict(p, demoted_from_approved="arm composition not recorded in AACT"))
                        fit_stats["no_arm_links"] += 1
                        continue
                    reg_roots = set().union(*[di.lookup(x)[1] for x in key.split(" + ") if di.lookup(x)[0] in ("one", "combo")], {croot})
                    extra = unlisted_partners(p["nct_id"], croot, reg_roots, comp_arms, di, skip_roots)
                    if extra:
                        groups["investigational_combination"].append(dict(p, regimen_key=f"{p.get('regimen_key') or key} + unlisted: {', '.join(extra)}",
                                                                          approved_regimen=None, demoted_from_approved="unlisted partner drug(s) in the drug's arm"))
                        fit_stats["unlisted_partner"] += 1
                        continue
                    arm_txt = " ".join(f"{a['title']} {a.get('desc', '')} " + " ".join(n for _, n in a["ints"]) + " " + " ".join(sum(a["others"].values(), []))
                                       for a in (comp_arms.get(p["nct_id"]) or {}).values())
                    why = approved_fit(p, label_attrs.get(croot) or n.get("attrs") or {}, arm_txt)
                    if why:
                        other_use[why].append(dict(p, demoted_from_approved=why))
                        fit_stats["other_setting_or_formulation"] += 1
                        continue
                    fit_stats["kept"] += 1
                appr[key].append(p)
        first = {k: min((p["approval_basis"]["approval_date"] for p in v if p.get("approval_basis")), default=None) for k, v in appr.items()}
        if fda:
            first.setdefault(central.get("approved_regimen"), fda["approval_date"])
            first[central.get("approved_regimen")] = min(filter(None, [first[central.get("approved_regimen")], fda["approval_date"]]))
        rel_rank = {"approval_lead": 0, "approval_co_basis": 1, "companion": 2}
        cnode["children"] = []
        if appr:
            cnode["children"].append(dict(id=f"{cid}:approved", kind="study_set", name=f"Approved regimens  ({sum(map(len, appr.values()))} trials, {len(appr)} regimens)",
                attrs=dict(note="Trials of regimens in an FDA approval of this drug: the approval trial(s) first, then companions of the same regimen.",
                           by_regimen={k: len(v) for k, v in appr.items()}),
                children=[dict(id=f"{cid}:approved:{k}", kind="study_set",
                               name=f"{k}  ·  {'FDA ' + first[k] if first.get(k) else 'not in an FDA approval (central regimen)'}  ({len(v)})",
                               attrs=dict(regimen=k, first_approval=first.get(k), studies=len(v), ncts=[p["nct_id"] for p in v]),
                               children=[study_node(f"{cid}:approved:{k}", p) for p in sorted(v, key=lambda p: (rel_rank[p["relation"]], p.get("start_date") or ""))[:STUDY_GROUP_CAP]])
                          for k, v in sorted(appr.items(), key=lambda kv: (first.get(kv[0]) or "9999", kv[0]))]))
        if other_use:
            cnode["children"].append(dict(id=f"{cid}:other_use", kind="study_set",
                name=f"Same regimen, other setting/formulation  ({sum(map(len, other_use.values()))} trials)",
                attrs=dict(note="Companion trials of an approved regimen that do not test the US-labelled AML use (setting or product). "
                                "Not approval evidence; kept for context.", by_reason={k: len(v) for k, v in other_use.items()}),
                children=[dict(id=f"{cid}:other_use:{i}", kind="study_set", name=f"{k}  ({len(v)}{', top ' + str(STUDY_GROUP_CAP) if len(v) > STUDY_GROUP_CAP else ''})",
                               attrs=dict(reason=k, studies=len(v), ncts=[p["nct_id"] for p in v]),
                               children=[study_node(f"{cid}:other_use:{i}", p) for p in sorted(v, key=rank, reverse=True)[:STUDY_GROUP_CAP]])
                          for i, (k, v) in enumerate(sorted(other_use.items(), key=lambda kv: -len(kv[1])))]))
        if no_arms:
            cnode["children"].append(dict(id=f"{cid}:no_arms", kind="study_set",
                name=f"Same regimen, arm composition not recorded  ({len(no_arms)}{', top ' + str(STUDY_GROUP_CAP) if len(no_arms) > STUDY_GROUP_CAP else ''})",
                attrs=dict(note="Companion trials of an approved regimen whose AACT record links no interventions to arms, so partner drugs "
                                "cannot be checked. Not approval evidence; kept for context.", studies=len(no_arms), ncts=[p["nct_id"] for p in no_arms]),
                children=[study_node(f"{cid}:no_arms", p) for p in sorted(no_arms, key=rank, reverse=True)[:STUDY_GROUP_CAP]]))
        g = sorted(groups.pop("investigational_combination", []), key=rank, reverse=True)
        if g:
            by_reg = defaultdict(list)
            for p in g:
                by_reg[p.get("regimen_key") or "?"].append(p)
            cnode["children"].append(dict(id=f"{cid}:investigational_combination", kind="study_set", name=f"investigational_combination  ({len(g)} trials, {len(by_reg)} regimens)",
                attrs=dict(relation="investigational_combination", studies=len(g), note="Regimens not in any FDA approval of this drug.",
                           by_regimen={k: len(v) for k, v in by_reg.items()}),
                children=[dict(id=f"{cid}:investigational_combination:{k}", kind="study_set", name=f"{k}  ({len(v)})",
                               attrs=dict(regimen=k, studies=len(v), ncts=[p["nct_id"] for p in v]),
                               children=[study_node(f"{cid}:investigational_combination:{k}", p) for p in v[:STUDY_GROUP_CAP]])
                          for k, v in sorted(by_reg.items(), key=lambda kv: (-len(kv[1]), kv[0]))]))
        if not cnode["children"]:
            del cnode["children"]
        for rel in sorted(groups, key=lambda r: RELATION_ORDER.index(r) if r in RELATION_ORDER else 99):
            g = sorted(groups[rel], key=rank, reverse=True)
            kids.append(dict(id=f"{sid}:{rel}", kind="study_set", name=f"{rel}  ({len(g)}{', top ' + str(STUDY_GROUP_CAP) if len(g) > STUDY_GROUP_CAP else ''})",
                             attrs=dict(relation=rel, studies=len(g), ncts=[p["nct_id"] for p in g]),
                             children=[study_node(f"{sid}:{rel}", p) for p in g[:STUDY_GROUP_CAP]]))
        n["children"] = [dict(id=sid, kind="study_set", name=f"Central study + {len(rows) - 1} supplemental (semantic V2)",
                              attrs=dict(central_nct=central["nct_id"], regimen=central.get("regimen_key"), regimen_outcome=central.get("regimen_outcome"),
                                         central_basis="FDA approval package (data/central_anchor.json)" if fda else "ranking key (no FDA anchor)",
                                         by_relation=dict(Counter(p["relation"] for p in rows if p is not central))),
                              provenance=dict(source="data/match.json"), children=kids)] + (n.get("children") or [])
        attached += 1

    visit(tree)
    if fit_stats:
        print(f"[approved-fit] companion placements (per drug node, all branches): {dict(fit_stats)}", flush=True)
    grave = json.load(open(gpath, encoding="utf-8"))["rows"] if os.path.exists(gpath) else []
    grave = [g for g in grave if not apl_pair(g)]
    if grave:
        lv_nodes = []
        for lv in ("semantic", "hard", "human"):
            rows = [g for g in grave if g["fail_level"] == lv]
            if not rows:
                continue
            per = defaultdict(list)
            for g in rows:
                per[g["drug"]].append(g)
            dnodes = [dict(id=f"grave:{lv}:{d}", kind="study_set", name=f"{d}  ({len(v)})", attrs=dict(studies=len(v)),
                           children=[dict(id=f"grave:{lv}:{d}:{g['nct_id']}", kind="grave", name=f"{g['nct_id']}  ·  {(g['reasons'] or ['?'])[-1][:70]}",
                                          attrs=g, provenance=dict(source="data/graveyard.json")) for g in v])
                      for d, v in sorted(per.items(), key=lambda kv: -len(kv[1]))]
            lv_nodes.append(dict(id=f"grave:{lv}", kind="branch", name=f"failed {lv} logic ({len(rows)})", attrs=dict(fail_level=lv), children=dnodes))
        tree["children"] = (tree.get("children") or []) + [dict(id="branch:graveyard", kind="branch", name=f"Graveyard ({len(grave)})",
            attrs=dict(note="Pairs from the broad universe search that failed a level of the logic. salvage_as marks rows that can still supplement a central study."),
            provenance=dict(source="data/graveyard.json"), children=lv_nodes)]
    tree.setdefault("meta", {}).setdefault("counts", {})["semantic"] = dict(drugs_with_central=attached, graveyard=len(grave),
                                                                            summary=m["meta"].get("summary"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope-id", default="MONDO_0018874")
    ap.add_argument("--label", default="Acute myeloid leukemia (NPM1-mutated focus)")
    ap.add_argument("--subtype-text", default="NPM1")
    ap.add_argument("--refresh-ema", action="store_true", help="re-download the EMA medicines report even if the cache is fresh")
    ap.add_argument("--label-regex", default=r"acute myeloid leukemia|acute myelogenous leukemia|acute myeloblastic leukemia|acute myelocytic leukemia|acute non-?lymphocytic leukemia|\bAML\b|\bANLL\b")
    sys.exit(build(ap.parse_args()))
