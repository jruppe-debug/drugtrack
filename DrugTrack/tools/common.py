"""Shared helpers and hash indexes, so repeated logic runs once.
  scope_regex(cfg)          disease name regex (ontology names + config acronyms) - was rebuilt in 4 tools
  strip_paren, SALT         label-text normalizers - were defined twice
  load_outputs(disease)     universe / records / verification for a disease
  UniverseIndex             built once per universe, then every lookup is a dict read:
      mesh_ncts[mesh]           -> NCTs whose intervention MeSH holds it
      rx_ncts[rxcui]            -> NCTs whose intervention names (or MeSH ids) resolve to that RxNorm ingredient
      heads[heading_lc]         -> MeSH ids (intervention MeSH heading text)
      acr[norm]                 -> NCTs by registry acronym;  title_tok[token] -> NCTs by title token
  DocText                   FDA document text normalized once (separator-free upper string + id set), reused for every id
Name -> RxCUI uses local sources only (DrugCentral synonyms, MeSH entry terms from the bulk MeSH table, and RxNav answers
already cached in the store); nothing here calls the network.
"""
import gzip, json, os, re
from collections import defaultdict
import disease_config

BRIDGE = os.environ.get("AACT_BRIDGE_DIR", r"C:\Users\jwrup\Clinical Tracker\AACT_bridge")
SALT = re.compile(r"\s+(?:phosphate|hydrochloride|dihydrochloride|sodium|disodium|potassium|mesylate|dimesylate|citrate|maleate|"
                  r"acetate|sulfate|fumarate|tartrate|besylate|tosylate|succinate|monohydrate|hydrobromide)$")
SPLIT = re.compile(r"[()+,/;\[\]]|\band\b|\bwith\b|\bassociated with\b|\bplus\b", re.I)
_scope = {}


def strip_paren(t):
    """Drop short parenthetical asides: 'chronic lymphatic (lymphocytic) leukemia' -> 'chronic lymphatic leukemia'."""
    return re.sub(r"\s*\([^()]{1,30}\)", "", t)


def scope_regex(cfg):
    if cfg["scope_id"] not in _scope:
        import fidelity as F
        rx, _, _ = F.scope_term_regex(os.path.join(BRIDGE, "inputs", "opentargets", "disease.parquet"), cfg["scope_id"])
        _scope[cfg["scope_id"]] = re.compile(rx + "".join(r"|(?<![A-Za-z])" + re.escape(t) + r"(?![A-Za-z])"
                                                          for t in cfg.get("label_title_extra_terms", [])), re.I)
    return _scope[cfg["scope_id"]]


def load_outputs(disease, *names):
    d = disease_config.data_dir(disease)
    out = []
    for n in names:
        p = os.path.join(d, n)
        out.append(json.load(gzip.open(p, "rt", encoding="utf-8")) if n.endswith(".gz") else json.load(open(p, encoding="utf-8")))
    return out


def norm_name(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def norm_id(s):
    return re.sub(r"[\s\-_/.]", "", s or "").upper()


class UniverseIndex:
    def __init__(self, U, xw):
        import store
        self.T = U["trials"]
        mi = store.mesh_index()
        self.mesh_ncts, self.rx_ncts, self.heads = defaultdict(set), defaultdict(set), defaultdict(set)
        self.acr, self.title_tok = defaultdict(set), defaultdict(set)
        name_cache = {}

        def name_rx(n):                              # local-only name -> RxNorm ingredients
            k = norm_name(n)
            if k in name_cache:
                return name_cache[k]
            r = set(xw.syn_ingredients(k)) | set(xw.syn_ingredients(SALT.sub("", k)))
            r |= xw.rx_name_cached(k)
            for ui in mi["term"].get(k, []):
                r |= xw.dc_ingredients(ui)
            name_cache[k] = r
            return r

        for n, t in self.T.items():
            for m in t.get("int_mesh", []):
                self.mesh_ncts[m].add(n)
                for r in xw.dc_ingredients(m):
                    self.rx_ncts[r].add(n)
            for m, term in t.get("int_mesh_terms", {}).items():
                self.heads[term.lower()].add(m)
            for iv in t.get("interventions", []):
                for nm in [iv["name"]] + list(iv.get("other_names") or []):
                    for p in [nm] + [x.strip() for x in SPLIT.split(nm) if x and len(x.strip()) >= 3]:
                        for r in name_rx(p):
                            self.rx_ncts[r].add(n)
            if t.get("acronym"):
                self.acr[norm_id(t["acronym"])].add(n)
            for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]*", " ".join(x for x in (t.get("brief_title"), t.get("official_title")) if x)):
                self.title_tok[tok].add(n)

    def ncts_for(self, rxcuis=(), mesh=()):
        out = set()
        for r in rxcuis:
            out |= self.rx_ncts.get(r, set())
        for m in mesh:
            out |= self.mesh_ncts.get(m, set())
        return out

    def by_name(self, names, cands):
        """Label section 14 trial names -> candidate NCTs (registry acronym or whole title token)."""
        out = {}
        for nm in names:
            for n in (self.acr.get(norm_id(nm), set()) | self.title_tok.get(nm, set())) & cands:
                out.setdefault(n, []).append(nm)
        return out


class DocText:
    """One FDA document set, normalized once; id lookups are substring tests on the precomputed string."""

    def __init__(self, text):
        self.text = text
        self.flat = norm_id(text)

    def has(self, kind, val):
        n = norm_id(val)
        if len(n) < 5:
            return False
        if kind in ("acronym", "alias"):
            return re.search(r"(?<![A-Za-z0-9])" + re.escape(val) + r"(?![A-Za-z0-9])", self.text) is not None
        return n in self.flat


# ------------------------------------------------------------------ parsed DailyMed label versions (parse once)
def _label_key(xml_path):
    return os.path.basename(xml_path)[:-4]           # '<setid>_v<version>'


def label_parsed(xml_path):
    """sections + product data of one archived SPL version, parsed once and kept in store table label_parsed.
    Every later reader (scope gate, version search, approval history, records, config derivation) gets the stored copy."""
    import store
    c = store.con()
    c.execute("CREATE TABLE IF NOT EXISTS label_parsed(key VARCHAR PRIMARY KEY, sections VARCHAR, product VARCHAR, route VARCHAR)")
    k = _label_key(xml_path) + "|" + parser_sig()
    row = c.execute("SELECT sections, product, route FROM label_parsed WHERE key = ?", [k]).fetchone()
    if row:
        return json.loads(row[0]), json.loads(row[1]), json.loads(row[2])
    import xml.etree.ElementTree as ET
    from indication_records import sections, product
    root = ET.parse(xml_path).getroot()
    S = sections(root)
    prods, route = product(root)
    c.execute("INSERT OR REPLACE INTO label_parsed VALUES (?, ?, ?, ?)", [k, json.dumps(S), json.dumps(prods), json.dumps(route)])
    return S, prods, route


def parser_sig():
    """Hash of the SPL parser source (sections + product): changing the parser invalidates every stored parse."""
    if "_ps" not in _scope:
        import hashlib, inspect
        from indication_records import sections, product
        _scope["_ps"] = hashlib.sha256((inspect.getsource(sections) + inspect.getsource(product)).encode()).hexdigest()[:10]
    return _scope["_ps"]


def sections_for(xml_path):
    S = label_parsed(xml_path)[0]
    return {k: dict(text=v["text"], subs=[tuple(x) for x in v["subs"]]) for k, v in S.items()}


# ------------------------------------------------------------------ stage fingerprints (skip unchanged work)
TOOLS = os.path.dirname(os.path.abspath(__file__))


def _file_sig(p):
    import hashlib
    if not os.path.exists(p):
        return None
    if p.endswith((".py", ".json")) and os.path.getsize(p) < 50_000_000:
        return hashlib.sha256(open(p, "rb").read()).hexdigest()[:16]
    st = os.stat(p)
    return f"{st.st_size}:{int(st.st_mtime)}"


def fingerprint(disease, stage, inputs, code):
    """Hash of the stage's code files, disease config + defaults, input files and bulk-table load stamps."""
    import hashlib
    import store
    parts = {"stage": stage, "code": {c: _file_sig(os.path.join(TOOLS, c)) for c in sorted(code)},
             "config": [_file_sig(os.path.join(disease_config.CFG_DIR, f"{disease}.json")), _file_sig(disease_config.DEFAULTS)],
             "inputs": {os.path.basename(p): _file_sig(p) for p in sorted(inputs)},
             "bulk": store.con().execute("SELECT source, loaded_at FROM bulk_meta ORDER BY 1").fetchall()}
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20], parts


def stage_current(disease, stage, inputs, outputs, code, force=False):
    """True when every output exists and the fingerprint matches the last successful run (then the stage is skipped)."""
    fp, _ = fingerprint(disease, stage, inputs, code)
    man = os.path.join(disease_config.data_dir(disease), "stage_manifest.json")
    m = json.load(open(man, encoding="utf-8")) if os.path.exists(man) else {}
    ok = not force and m.get(stage, {}).get("fingerprint") == fp and all(os.path.exists(o) for o in outputs)
    disease_config.log(disease, stage, f"{'up to date, skipped' if ok else 'running'} (fingerprint {fp})")
    return ok


def stage_done(disease, stage, inputs, outputs, code):
    from datetime import datetime
    fp, parts = fingerprint(disease, stage, inputs, code)
    man = os.path.join(disease_config.data_dir(disease), "stage_manifest.json")
    m = json.load(open(man, encoding="utf-8")) if os.path.exists(man) else {}
    m[stage] = dict(fingerprint=fp, finished=datetime.now().isoformat(timespec="seconds"), parts=parts,
                    outputs={os.path.basename(o): _file_sig(o) for o in outputs})
    json.dump(m, open(man, "w", encoding="utf-8"), indent=1)


def universe_index(U, xw):
    """UniverseIndex from the store when nothing it depends on changed (universe snapshot, crosswalk, common.py, size of the
    RxNav / MeSH caches it reads); otherwise build and store it. Same content either way - only the build is skipped."""
    import hashlib
    import store
    sig = json.dumps([U.get("scope_id"), U.get("generated"), _file_sig(os.path.join(disease_config.DATA, "id_crosswalk.json.gz")),
                      _file_sig(os.path.join(TOOLS, "common.py")),
                      store.con().execute("SELECT ns, count(*) FROM kv WHERE ns IN ('rxnav','mesh') GROUP BY 1 ORDER BY 1").fetchall()])
    key = hashlib.sha256(sig.encode()).hexdigest()[:20]
    kv = store.KV("uindex")
    hit = kv.get(key)
    idx = UniverseIndex.__new__(UniverseIndex)
    if hit:
        idx.T = U["trials"]
        for f in ("mesh_ncts", "rx_ncts", "heads", "acr", "title_tok"):
            setattr(idx, f, defaultdict(set, {k: set(v) for k, v in hit[f].items()}))
        idx.from_store = True
        return idx
    idx = UniverseIndex(U, xw)
    kv.put(key, {f: {k: sorted(v) for k, v in getattr(idx, f).items()} for f in ("mesh_ncts", "rx_ncts", "heads", "acr", "title_tok")})
    kv.flush()
    idx.from_store = False
    return idx


def prefetch(fn, items, workers=4, label=None, disease=None):
    """Warm file caches in parallel (network-bound fetches only; each item writes its own cache file). Errors are left to the
    sequential pass, which retries and records them, so results never depend on thread timing."""
    from concurrent.futures import ThreadPoolExecutor
    items = list(items)
    if not items:
        return 0
    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(lambda x: _safe(fn, x), items):
            ok += bool(r)
    if disease:
        disease_config.log(disease, label or "prefetch", f"prefetched {ok}/{len(items)} with {workers} workers")
    return ok


def _safe(fn, x):
    try:
        fn(*x) if isinstance(x, tuple) else fn(x)
        return True
    except Exception:
        return False
