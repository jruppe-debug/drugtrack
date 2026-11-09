"""Regimen key: one identity for a drug combination, built from identifiers.
Ingredient = RxNorm ingredient (TTY IN) RxCUI, reached by two independent routes that must agree:
  DrugCentral   identifier table (ChEMBL_ID / UNII / MeSH descriptor + supplementary / RXNORM per struct_id), pulled to
                data/id_crosswalk.json.gz with structure names, synonyms (code names) and ATC codes
  RxNav         UNII_CODE -> RxCUI -> ingredient (related.json?tty=IN); ATC classes from RxClass (relaSource=ATC)
  NLM MeSH      exact descriptor / term lookup (id.nlm.nih.gov/mesh/lookup) and SPARQL preferredMappedTo, used for drugs that
                DrugCentral does not carry and for combination products (CPX-351 C000629812 -> D003561 + D003630)
Key text: sorted ingredient RxCUIs joined by '+'; a fixed-combination product adds '|P:<MeSH id>'; a label class word that the
label leaves open ('anthracycline') adds '|C:<ATC prefix>' and is filled from the trial arm. Examples:
  1919083+3041+3109            midostaurin + cytarabine + daunorubicin
  3041+3109|P:C000629812       CPX-351 (Vyxeos), not the same regimen as 7+3 (3041+3109)
  2643048+3041|C:L01DB         quizartinib + cytarabine + an anthracycline
Names (trial intervention names, label partners) are accepted only when the RxCUI they give is also reached from a code on
the same record: the trial's intervention MeSH ids, or the label's UNII codes.
Run:  python tools/regimen_key.py pull | show <MeSH|UNII|ChEMBL id>
"""
import gzip, json, os, re, sys, time, urllib.parse, urllib.request
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
XWALK = os.path.join(DATA, "id_crosswalk.json.gz")
RXCACHE = os.path.join(DATA, "references", "rxnav")
MESHCACHE = os.path.join(DATA, "references", "mesh")
RXNAV = "https://rxnav.nlm.nih.gov/REST"
MESH = "https://id.nlm.nih.gov/mesh"
DRUG_COMBINATIONS = "D004338"
DC = dict(host="unmtid-dbs.net", port=5433, dbname="drugcentral", user="drugman", password="dosage")   # DrugCentral's published public read-only login
ID_TYPES = ("ChEMBL_ID", "UNII", "RXNORM", "MESH_DESCRIPTOR_UI", "MESH_SUPPLEMENTAL_RECORD_UI")
PLACEBO = {"placebo", "placebos", "matching placebo", "placebo oral capsule", "placebo oral tablet"}
PLACEBO_RX = re.compile(r"(?<![a-z])(?:placebo|sham|vehicle)(?![a-z])", re.I)   # 'Placebo to match idelalisib' is a placebo
SPLIT = re.compile(r"[()+,/;\[\]]|\band\b|\bwith\b|\bassociated with\b|\bplus\b", re.I)


def pull():
    import psycopg2
    c = psycopg2.connect(connect_timeout=30, **DC)
    cur = c.cursor()
    cur.execute("SELECT struct_id, id_type, identifier FROM identifier WHERE id_type = ANY(%s) AND struct_id IS NOT NULL",
                (list(ID_TYPES),))
    structs = defaultdict(lambda: defaultdict(list))
    for s, t, i in cur.fetchall():
        structs[s][t].append(i.strip())
    cur.execute("SELECT id, name FROM structures")
    names = dict(cur.fetchall())
    cur.execute("SELECT struct_id, atc_code FROM struct2atc")
    atc = defaultdict(list)
    for s, a in cur.fetchall():
        atc[s].append(a)
    cur.execute("SELECT id, name FROM synonyms WHERE id IS NOT NULL")
    syn = defaultdict(list)
    for s, n in cur.fetchall():
        syn[s].append(n)
    cur.execute("SELECT version, dtime FROM dbversion")
    ver = cur.fetchall()
    out = dict(source="DrugCentral identifier/structures/struct2atc/synonyms", dbversion=[list(map(str, v)) for v in ver],
               structs={str(s): dict(name=names.get(s), atc=atc.get(s, []), synonyms=syn.get(s, []), **dict(d))
                        for s, d in structs.items()})
    with gzip.open(XWALK, "wt", encoding="utf-8") as f:
        json.dump(out, f)
    print(f"crosswalk: {len(out['structs'])} DrugCentral structures -> {XWALK}")


def norm_syn(n):
    return re.sub(r"[\s\-_]", "", n.lower())


def key(rxcuis, products=(), classes=()):
    k = "+".join(sorted(set(r for r in rxcuis if r), key=int))
    if products:
        k += "|P:" + "+".join(sorted(set(products)))
    if classes:
        k += "|C:" + "+".join(sorted(set(classes)))
    return k


def parse_key(k):
    parts = k.split("|")
    out = dict(rx=set(x for x in parts[0].split("+") if x), products=set(), classes=set())
    for p in parts[1:]:
        if p.startswith("P:"):
            out["products"] = set(p[2:].split("+"))
        elif p.startswith("C:"):
            out["classes"] = set(p[2:].split("+"))
    return out


class Crosswalk:
    def __init__(self):
        if not os.path.exists(XWALK):
            pull()
        d = json.load(gzip.open(XWALK, "rt", encoding="utf-8"))
        self.structs = d["structs"]
        self.by = defaultdict(set)
        self.syn = defaultdict(set)
        for s, r in self.structs.items():
            for t in ID_TYPES:
                for i in r.get(t, []):
                    self.by[(t, i)].add(s)
            for n in r.get("synonyms", []) + [r.get("name") or ""]:
                if n:
                    self.syn[norm_syn(n)].add(s)
        self._kvs = {}
        os.makedirs(RXCACHE, exist_ok=True)
        os.makedirs(MESHCACHE, exist_ok=True)

    # ---------- cached HTTP
    def _kv(self, ns):
        import store
        if ns not in self._kvs:
            self._kvs[ns] = store.KV(ns)
        return self._kvs[ns]

    def _get(self, url, fn, pause=0.06):
        """Cached GET. Cache = store.kv(ns = cache folder name, key = file stem): one dict read, no file I/O."""
        kv, key = self._kv(os.path.basename(os.path.dirname(fn))), os.path.basename(fn)[:-5]
        hit = kv.get(key)
        if hit is not None:
            return hit
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "clinical-tracker"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    d = json.load(r)
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))
        kv.put(key, d)
        time.sleep(pause)
        return d

    def _rx(self, path, k):
        return self._get(RXNAV + path, os.path.join(RXCACHE, re.sub(r"[^A-Za-z0-9_.-]", "_", k)[:150] + ".json"))

    # ---------- DrugCentral
    def structs_for(self, ident):
        if re.fullmatch(r"[CD]\d{6,9}", ident):
            return self.by[("MESH_DESCRIPTOR_UI", ident)] | self.by[("MESH_SUPPLEMENTAL_RECORD_UI", ident)]
        if ident.upper().startswith("CHEMBL"):
            return set(self.by[("ChEMBL_ID", ident.upper())])
        return set(self.by[("UNII", ident)])

    def dc_ingredients(self, ident):
        out = set()
        for s in self.structs_for(ident):
            for r in self.structs[s].get("RXNORM", []):
                out |= self.to_in(r)
        return out

    def mesh_of(self, structs):
        return sorted({m for s in structs for t in ("MESH_DESCRIPTOR_UI", "MESH_SUPPLEMENTAL_RECORD_UI")
                       for m in self.structs[s].get(t, [])})

    def syn_ingredients(self, name):
        out = set()
        for s in self.syn.get(norm_syn(name), set()):
            for r in self.structs[s].get("RXNORM", []):
                out |= self.to_in(r)
        return out

    # ---------- RxNav
    def to_in(self, rxcui):
        d = self._rx(f"/rxcui/{rxcui}/related.json?tty=IN", f"in_{rxcui}")
        ids = {c["rxcui"] for g in d.get("relatedGroup", {}).get("conceptGroup", []) or []
               for c in g.get("conceptProperties", []) or []}
        if not ids:
            p = self._rx(f"/rxcui/{rxcui}/properties.json", f"prop_{rxcui}")
            if (p.get("properties") or {}).get("tty") == "IN":
                ids = {rxcui}
        return ids

    def rx_unii(self, unii):
        d = self._rx(f"/rxcui.json?idtype=UNII_CODE&id={unii}", f"unii_{unii}")
        return {i for r in d.get("idGroup", {}).get("rxnormId", []) for i in self.to_in(r)}

    def rx_name_cached(self, name):
        """rx_name only when RxNav already answered this name (no network)."""
        n = re.sub(r"\s+", " ", name.strip().lower())
        if len(n) < 3 or self._kv("rxnav").get(re.sub(r"[^A-Za-z0-9_.-]", "_", f"name_{n}")[:150]) is None:
            return set()
        return self.rx_name(n)

    def rx_name(self, name):
        n = re.sub(r"\s+", " ", name.strip().lower())
        if len(n) < 3:
            return set()
        d = self._rx("/rxcui.json?" + urllib.parse.urlencode({"name": n, "search": 1}), f"name_{n}")
        return {i for r in d.get("idGroup", {}).get("rxnormId", []) for i in self.to_in(r)}

    def in_name(self, rxcui):
        p = self._rx(f"/rxcui/{rxcui}/properties.json", f"prop_{rxcui}")
        return (p.get("properties") or {}).get("name")

    def rx_atc(self, rxcui):
        d = self._rx(f"/rxclass/class/byRxcui.json?rxcui={rxcui}&relaSource=ATC", f"atc_{rxcui}")
        return sorted({c["rxclassMinConceptItem"]["classId"] for c in
                       (d.get("rxclassDrugInfoList") or {}).get("rxclassDrugInfo", [])})

    # ---------- NLM MeSH
    def mesh_lookup(self, name):
        """Exact name -> MeSH record: bulk MeSH entry terms first (descriptor before supplementary), then the API."""
        import store
        hits = store.mesh_index()["term"].get((name or "").strip().lower(), [])
        if hits:
            return sorted(hits, key=lambda u: (u[0] != "D", u))[0]
        return self._mesh_lookup_api(name)

    def _mesh_lookup_api(self, name):
        """Exact name -> MeSH record id (descriptor first, else the record holding that exact entry term)."""
        n = name.strip()
        if len(n) < 3:
            return None
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", n.lower())[:120]
        d = self._get(f"{MESH}/lookup/descriptor?" + urllib.parse.urlencode({"label": n, "match": "exact"}),
                      os.path.join(MESHCACHE, f"desc_{safe}.json"), 0.1)
        if d:
            return d[0]["resource"].rsplit("/", 1)[1]
        t = self._get(f"{MESH}/lookup/term?" + urllib.parse.urlencode({"label": n, "match": "exact"}),
                      os.path.join(MESHCACHE, f"term_{safe}.json"), 0.1)
        if not t:
            return None
        tid = t[0]["resource"].rsplit("/", 1)[1]
        q = ("PREFIX meshv: <http://id.nlm.nih.gov/mesh/vocab#> PREFIX mesh: <http://id.nlm.nih.gov/mesh/> "
             f"SELECT DISTINCT ?rec WHERE {{ ?c meshv:term|meshv:preferredTerm mesh:{tid} . "
             "?rec meshv:concept|meshv:preferredConcept ?c . }")
        r = self._get(f"{MESH}/sparql?" + urllib.parse.urlencode({"query": q, "format": "JSON", "limit": 20}),
                      os.path.join(MESHCACHE, f"rec_{tid}.json"), 0.1)
        recs = [b["rec"]["value"].rsplit("/", 1)[1] for b in r["results"]["bindings"]]
        recs = [x for x in recs if re.fullmatch(r"[CD]\d+", x)]
        live = [x for x in recs if not self.mesh_obsolete(x)]
        return (live or recs or [None])[0]

    def mesh_obsolete(self, rec):
        lab = self.mesh_labels([rec]).get(rec, "")
        return lab.startswith("[OBSOLETE]")

    def _sparql_batch(self, ids, what, kind):
        ids = sorted(set(i for i in ids if re.fullmatch(r"[CD]\d+", i)))
        out, todo = {}, []
        import store
        mi = store.mesh_index()                   # bulk MeSH tables answer first; SPARQL only for ids not in the release
        kv = self._kv("mesh")
        for i in ids:
            if i in mi["names"]:
                out[i] = mi["mapped"].get(i, []) if kind == "mapped" else [mi["names"][i]]
                continue
            hit = kv.get(f"{kind}_{i}")
            if hit is not None:
                out[i] = hit
            else:
                todo.append(i)
        for k in range(0, len(todo), 100):
            chunk = todo[k:k + 100]
            vals = " ".join(f"mesh:{i}" for i in chunk)
            q = ("PREFIX meshv: <http://id.nlm.nih.gov/mesh/vocab#> PREFIX mesh: <http://id.nlm.nih.gov/mesh/> "
                 "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
                 f"SELECT ?s ?v WHERE {{ VALUES ?s {{ {vals} }} ?s {what} ?v . }}")
            url = f"{MESH}/sparql?" + urllib.parse.urlencode({"query": q, "format": "JSON", "limit": 1000})
            req = urllib.request.Request(url, headers={"User-Agent": "clinical-tracker"})
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.load(r)
            got = defaultdict(list)
            for b in d["results"]["bindings"]:
                v = b["v"]["value"]
                got[b["s"]["value"].rsplit("/", 1)[1]].append(v.rsplit("/", 1)[1] if v.startswith("http") else v)
            for i in chunk:
                out[i] = got.get(i, [])
                kv.put(f"{kind}_{i}", out[i])
            time.sleep(0.2)
        return out

    def mesh_mapped(self, ids):
        """Supplementary record -> descriptors it is mapped to (meshv:preferredMappedTo)."""
        return self._sparql_batch(ids, "meshv:preferredMappedTo", "mapped")

    def mesh_labels(self, ids):
        return {k: (v[0] if v else "") for k, v in self._sparql_batch(ids, "rdfs:label", "label").items()}

    def is_product(self, rec):
        """A combination product: MeSH maps it to 'Drug Combinations', or to 2+ headings that are themselves RxNorm ingredients
        (CPX-351 -> Cytarabine + Daunorubicin). Idelalisib maps to chemical classes (Purines, Quinazolinones): not a product."""
        m = self.mesh_mapped([rec]).get(rec, [])
        drugs = [x for x in m if x != DRUG_COMBINATIONS and self.dc_ingredients(x)]
        return rec.startswith("C") and (DRUG_COMBINATIONS in m or len(drugs) >= 2)

    def product_ingredients(self, rec):
        out = set()
        for d in self.mesh_mapped([rec]).get(rec, []):
            if d != DRUG_COMBINATIONS:
                out |= self.dc_ingredients(d)
        return out

    # ---------- combined
    def ingredient(self, ident, name_hint=None):
        """One code -> ingredient by both routes. agree = DrugCentral and RxNav give the same IN set.
        mesh comes from DrugCentral; when DrugCentral has none, from an exact NLM MeSH lookup of the RxNorm name."""
        dc = self.dc_ingredients(ident)
        structs = self.structs_for(ident)
        uniis = sorted({u for s in structs for u in self.structs[s].get("UNII", [])}) or (
            [ident] if re.fullmatch(r"[A-Z0-9]{10}", ident) else [])
        rx = set()
        for u in uniis:
            rx |= self.rx_unii(u)
        mesh, mesh_route = self.mesh_of(structs), "drugcentral"
        if not mesh:
            names = [self.in_name(r) for r in sorted(rx)] + ([name_hint] if name_hint else [])
            hits = [m for m in (self.mesh_lookup(n) for n in names if n) if m]
            mesh, mesh_route = sorted(set(hits)), "nlm_mesh_exact_name" if hits else "none"
        return dict(id=ident, dc_structs=sorted(structs), unii=uniis, mesh=mesh, mesh_route=mesh_route,
                    chembl=sorted({c for s in structs for c in self.structs[s].get("ChEMBL_ID", [])}),
                    rxcui_drugcentral=sorted(dc), rxcui_rxnav=sorted(rx), rxcui=sorted(dc | rx),
                    agree="agree" if dc and dc == rx else ("rxnav_only" if rx and not dc else
                                                           ("drugcentral_only" if dc and not rx else
                                                            ("conflict" if dc and rx else "none"))))


def _pieces(name):
    return [p.strip() for p in SPLIT.split(name) if p and len(p.strip()) >= 3]


def arm_keys(xw, trial, label_rx=frozenset(), pk_rx=frozenset()):
    """Per arm: ingredient key from intervention names. A name's RxCUI counts only when a code on the same record reaches it:
    the trial's intervention MeSH ids (DrugCentral, or MeSH heading == RxNorm name), or the label's UNII-derived RxCUIs.
    PK probe drugs (pk_rx) are listed on the arm but kept out of the key."""
    mesh_in, products_in = {}, {}
    terms = trial.get("int_mesh_terms", {})
    for m in trial.get("int_mesh", []):
        got = xw.dc_ingredients(m)
        if not got and terms.get(m):
            got = {r for r in xw.rx_name(terms[m]) if (xw.in_name(r) or "").lower() == terms[m].lower()}
        if not got and m.startswith("C") and xw.is_product(m):
            products_in[m] = xw.product_ingredients(m)
            continue
        for r in got:
            mesh_in[r] = m
    by_label, items = {}, []
    for iv in trial.get("interventions", []):
        if iv["type"] not in ("DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT", "OTHER"):
            continue
        names = [iv["name"]] + list(iv.get("other_names") or [])
        if any(n.strip().lower() in PLACEBO or PLACEBO_RX.search(n) for n in names):
            res = dict(name=iv["name"], placebo=True)
        else:
            cand = names + [p for n in names for p in _pieces(n)]
            hits = set()
            for n in cand:
                hits |= xw.rx_name(n) | xw.syn_ingredients(n)
                for v in {n, re.sub(r"(?<=[a-z])(?=[A-Z])", "-", n)}:   # MeSH entry terms: 'AraC' -> 'Ara-C' -> D003561
                    rec = xw.mesh_lookup(v)
                    if rec:
                        hits |= xw.dc_ingredients(rec)
            prods = set()
            for n in cand:
                rec = next((m for m, t in terms.items() if t.lower() == n.strip().lower()), None) or xw.mesh_lookup(n)
                if rec and (rec in products_in or (rec.startswith("C") and rec in terms and xw.is_product(rec))):
                    prods.add(rec)
            conf, rule = [], None
            ok_mesh = sorted(h for h in hits if h in mesh_in)
            ok_label = sorted(h for h in hits if h in label_rx and h not in mesh_in)
            if ok_mesh:
                conf, rule = ok_mesh, "name -> RxCUI matches trial intervention MeSH"
            if ok_label:
                conf, rule = conf + ok_label, (rule + "; " if rule else "") + "name -> RxCUI matches label UNII"
            for p in prods:
                conf += sorted(xw.product_ingredients(p))
                rule = (rule + "; " if rule else "") + f"product MeSH {p} mapped-to ingredients"
            res = dict(name=iv["name"], rxcui_by_name=sorted(hits), confirmed=sorted(set(conf)), products=sorted(prods), rule=rule)
            if iv["type"] == "OTHER" and not hits and not prods:
                continue
        items.append(res)
        for lab in iv.get("arms", []):
            by_label.setdefault(lab, []).append(res)
    claimed = {r for it in items for r in it.get("confirmed", [])}
    names_in = {r: (xw.in_name(r) or "").lower() for r in mesh_in}
    for it in items:                               # ingredient name written inside the intervention name
        if it.get("placebo") or it.get("confirmed"):
            continue
        hit = sorted(r for r, n in names_in.items() if n and re.search(rf"(?<![a-z]){re.escape(n)}(?![a-z])", it["name"].lower()))
        if hit:
            it.update(confirmed=hit, rule="ingredient name within intervention name")
            claimed |= set(hit)
    left = sorted({it["name"] for it in items if not it.get("placebo") and not it.get("confirmed")})
    free = sorted(set(mesh_in) - claimed)
    if len(left) == 1 and len(free) == 1:
        for it in items:
            if it["name"] == left[0]:
                it.update(confirmed=free, rule="elimination: one unresolved name, one unclaimed MeSH ingredient")
    arms = []
    for a in trial.get("arms", []):
        its = by_label.get(a["label"], [])
        conf = {r for it in its for r in it.get("confirmed", [])}
        probes = sorted(conf & set(pk_rx))
        prods = {p for it in its for p in it.get("products", [])}
        arms.append(dict(label=a["label"], type=a["type"], items=its, key=key(conf - set(pk_rx), prods),
                         pk_probes=probes, placebo=any(it.get("placebo") for it in its),
                         unconfirmed=[it["name"] for it in its if not it.get("placebo") and not it.get("confirmed")]))
    return arms


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "pull":
        pull()
    elif len(sys.argv) > 2 and sys.argv[1] == "show":
        x = Crosswalk()
        r = x.ingredient(sys.argv[2])
        r["names"] = {i: x.in_name(i) for i in r["rxcui"]}
        print(json.dumps(r, indent=1))
    else:
        print(__doc__)
