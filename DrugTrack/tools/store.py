"""Local DuckDB store (data/drugtrack.duckdb): bulk source tables + one key-value cache + per-disease hash indexes.
Replaces per-request API calls and one-JSON-file-per-request caches.
Bulk tables (python tools/store.py load):
  drugsfda_applications / drugsfda_submissions / drugsfda_docs   Drugs@FDA bulk zip (fda.gov/media/89850/download)
  mesh_record / mesh_term / mesh_registry / mesh_mapped          MeSH desc + supp XML (nlmpubs.nlm.nih.gov)
  dm_setid_rxcui                                                 DailyMed rxnorm_mappings.zip
  kv(ns, key, value)                                             every RxNav / MeSH / openFDA / DailyMed-search / PubMed
                                                                 response; the old JSON cache folders are imported once
Python side: KV preloads a namespace into a dict (one query), so lookups are dict reads; new entries are written in one
batch at exit. Snapshot = the .duckdb file plus bulk_meta (file dates), so a run can be reproduced from one file.
Run:  python tools/store.py load      (bulk tables + cache import; ~1-2 min)
      python tools/store.py stats
"""
import atexit, glob, gzip, io, json, os, re, sys, zipfile
from datetime import datetime
import xml.etree.ElementTree as ET
import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
DB = os.path.join(DATA, "drugtrack.duckdb")
BRIDGE = os.environ.get("AACT_BRIDGE_DIR", os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "AACT_bridge")))
BULK = os.path.join(BRIDGE, "inputs", "bulk")
_con = None


def con():
    global _con
    if _con is None:
        _con = duckdb.connect(DB)
        _con.execute("CREATE TABLE IF NOT EXISTS kv(ns VARCHAR, key VARCHAR, value VARCHAR, PRIMARY KEY(ns, key))")
        _con.execute("CREATE TABLE IF NOT EXISTS bulk_meta(source VARCHAR PRIMARY KEY, file VARCHAR, loaded_at VARCHAR, rows BIGINT)")
    return _con


# ------------------------------------------------------------------ key-value cache
class KV:
    """dict view of one namespace. get() is a dict read; put() buffers; flush() writes the buffer in one statement."""
    _all = []

    def __init__(self, ns):
        self.ns = ns
        self.d = dict(con().execute("SELECT key, value FROM kv WHERE ns = ?", [ns]).fetchall())
        self.new = {}
        KV._all.append(self)

    def get(self, key):
        v = self.new.get(key) or self.d.get(key)
        return json.loads(v) if v is not None else None

    def put(self, key, obj):
        self.new[key] = json.dumps(obj)

    def flush(self):
        if self.new:
            con().execute("DELETE FROM kv WHERE ns = ? AND key IN (SELECT unnest(?))", [self.ns, list(self.new)])
            _bulk_insert("kv", ["ns", "key", "value"], [(self.ns, k, v) for k, v in self.new.items()])
            self.d.update(self.new)
            self.new = {}


@atexit.register
def _flush_all():
    for k in KV._all:
        try:
            k.flush()
        except Exception:
            pass


def _bulk_insert(table, cols, rows):
    """Rows -> Arrow table -> one INSERT ... SELECT (row-by-row executemany is far too slow in DuckDB)."""
    import pyarrow as pa
    if not rows:
        return
    at = pa.table({c: [r[i] for r in rows] for i, c in enumerate(cols)})
    con().register("_bulk", at)
    con().execute(f"INSERT INTO {table} SELECT * FROM _bulk")
    con().unregister("_bulk")


def import_json_caches():
    """One-time import of the old cache folders: references/<ns>/*.json -> kv(ns, file stem)."""
    n = 0
    for ns in ("rxnav", "mesh", "openfda", "dailymed_search", "pubmed", "ctgov_fih"):
        rows = []
        for f in glob.glob(os.path.join(DATA, "references", ns, "*.json")):
            try:
                rows.append((ns, os.path.basename(f)[:-5], open(f, encoding="utf-8").read()))
            except Exception:
                pass
        if rows:
            con().execute("DELETE FROM kv WHERE ns = ?", [ns])
            _bulk_insert("kv", ["ns", "key", "value"], rows)
            n += len(rows)
    return n


# ------------------------------------------------------------------ bulk loaders
def _meta(source, file, rows):
    con().execute("INSERT OR REPLACE INTO bulk_meta VALUES (?, ?, ?, ?)", [source, file, datetime.now().isoformat(timespec="seconds"), rows])


def load_drugsfda(path=os.path.join(BULK, "drugsatfda.zip")):
    z = zipfile.ZipFile(path)
    tmp = os.path.join(BULK, "_drugsfda")
    os.makedirs(tmp, exist_ok=True)
    for n in ("Applications.txt", "Submissions.txt", "ApplicationDocs.txt", "ApplicationsDocsType_Lookup.txt", "SubmissionClass_Lookup.txt"):
        raw = z.read(n).decode("cp1252", errors="replace")
        open(os.path.join(tmp, n), "w", encoding="utf-8", newline="").write(raw.replace("\r\n", "\n"))
    c = con()
    rd = lambda n: f"read_csv('{os.path.join(tmp, n)}', delim='\t', header=true, all_varchar=true, quote='', ignore_errors=true)"
    c.execute(f"""CREATE OR REPLACE TABLE drugsfda_applications AS
                  SELECT ApplType || trim(ApplNo) AS application, ApplType AS appl_type, trim(ApplNo) AS appl_no, SponsorName AS sponsor
                  FROM {rd('Applications.txt')}""")
    c.execute(f"""CREATE OR REPLACE TABLE drugsfda_submissions AS
                  SELECT a.application, trim(s.SubmissionType) AS submission_type, trim(s.SubmissionNo) AS submission_number,
                         trim(s.SubmissionStatus) AS submission_status, strftime(CAST(s.SubmissionStatusDate AS TIMESTAMP), '%Y%m%d') AS submission_status_date,
                         l.SubmissionClassCode AS submission_class_code, s.ReviewPriority AS review_priority
                  FROM {rd('Submissions.txt')} s JOIN drugsfda_applications a ON a.appl_no = trim(s.ApplNo)
                  LEFT JOIN {rd('SubmissionClass_Lookup.txt')} l ON trim(l.SubmissionClassCodeID) = trim(s.SubmissionClassCodeID)""")
    c.execute(f"""CREATE OR REPLACE TABLE drugsfda_docs AS
                  SELECT a.application, trim(d.SubmissionType) AS submission_type, trim(d.SubmissionNo) AS submission_number,
                         t.ApplicationDocsType_Lookup_Description AS doc_type, d.ApplicationDocsURL AS url,
                         strftime(CAST(d.ApplicationDocsDate AS TIMESTAMP), '%Y%m%d') AS doc_date
                  FROM {rd('ApplicationDocs.txt')} d JOIN drugsfda_applications a ON a.appl_no = trim(d.ApplNo)
                  LEFT JOIN {rd('ApplicationsDocsType_Lookup.txt')} t ON trim(t.ApplicationDocsType_Lookup_ID) = trim(d.ApplicationDocsTypeID)""")
    c.execute("CREATE INDEX IF NOT EXISTS i_sub ON drugsfda_submissions(application)")
    c.execute("CREATE INDEX IF NOT EXISTS i_docs ON drugsfda_docs(application)")
    n = c.execute("SELECT count(*) FROM drugsfda_submissions").fetchone()[0]
    _meta("drugsfda", path, n)
    return n


def load_dailymed_rxnorm(path=os.path.join(BULK, "rxnorm_mappings.zip")):
    tmp = os.path.join(BULK, "rxnorm_mappings.txt")
    if not os.path.exists(tmp):
        open(tmp, "wb").write(zipfile.ZipFile(path).read("rxnorm_mappings.txt"))
    c = con()
    c.execute(f"""CREATE OR REPLACE TABLE dm_setid_rxcui AS SELECT SETID AS setid, CAST(SPL_VERSION AS INT) AS spl_version,
                  RXCUI AS rxcui, RXSTRING AS rxstring, RXTTY AS rxtty
                  FROM read_csv('{tmp}', delim='|', header=true, all_varchar=true, quote='')""")
    n = c.execute("SELECT count(*) FROM dm_setid_rxcui").fetchone()[0]
    _meta("dailymed_rxnorm", path, n)
    return n


def load_mesh(desc=os.path.join(BULK, "desc2026.gz"), supp=os.path.join(BULK, "supp2026.gz")):
    rec, term, reg, mapped = [], [], [], []
    for path, kind, top, ui_tag, name_tag in ((desc, "D", "DescriptorRecord", "DescriptorUI", "DescriptorName"),
                                              (supp, "C", "SupplementalRecord", "SupplementalRecordUI", "SupplementalRecordName")):
        for ev, el in ET.iterparse(gzip.open(path), events=("end",)):
            if el.tag != top:
                continue
            ui = el.findtext(ui_tag)
            nm = el.findtext(f"{name_tag}/String")
            rec.append((ui, nm, kind))
            for t in el.iter("Term"):
                s = t.findtext("String")
                if s:
                    term.append((ui, s, s.lower()))
            for c in el.iter("Concept"):
                for r in c.iter("RegistryNumber"):
                    if r.text and r.text != "0":
                        reg.append((ui, r.text.strip()))
            for h in el.iter("HeadingMappedTo"):
                u = h.findtext("DescriptorReferredTo/DescriptorUI")
                if u:
                    mapped.append((ui, u.lstrip("*")))
            el.clear()
    c = con()
    c.execute("CREATE OR REPLACE TABLE mesh_record(ui VARCHAR, name VARCHAR, kind VARCHAR)")
    c.execute("CREATE OR REPLACE TABLE mesh_term(ui VARCHAR, term VARCHAR, term_lc VARCHAR)")
    c.execute("CREATE OR REPLACE TABLE mesh_registry(ui VARCHAR, number VARCHAR)")
    c.execute("CREATE OR REPLACE TABLE mesh_mapped(ui VARCHAR, mapped_ui VARCHAR)")
    _bulk_insert("mesh_record", ["ui", "name", "kind"], rec)
    _bulk_insert("mesh_term", ["ui", "term", "term_lc"], term)
    _bulk_insert("mesh_registry", ["ui", "number"], reg)
    _bulk_insert("mesh_mapped", ["ui", "mapped_ui"], mapped)
    _meta("mesh", f"{desc};{supp}", len(rec))
    return len(rec), len(term), len(reg), len(mapped)


# ------------------------------------------------------------------ query helpers (bulk-table versions of API calls)
_mem = {}


def drugsfda_submissions(app):
    """openFDA drugsfda 'submissions' shape for one application, from the bulk tables (AP and all statuses)."""
    if "subs" not in _mem:
        rows = con().execute("SELECT application, submission_type, submission_number, submission_status, submission_status_date, "
                             "submission_class_code FROM drugsfda_submissions").fetchall()
        docs = con().execute("SELECT application, submission_type, submission_number, doc_type, url FROM drugsfda_docs").fetchall()
        dd = {}
        for a, st, sn, t, u in docs:
            dd.setdefault((a, st, sn), []).append(dict(type=t, url=u))
        subs = {}
        for a, st, sn, ss, sd, cc in rows:
            subs.setdefault(a, []).append(dict(submission_type=st, submission_number=sn, submission_status=ss,
                                               submission_status_date=sd, submission_class_code=cc,
                                               application_docs=dd.get((a, st, sn), []), _app=a))
        _mem["subs"] = subs
    return _mem["subs"].get(app, [])


def mesh_index():
    if "mesh" not in _mem:
        c = con()
        term = {}
        for ui, lc in c.execute("SELECT ui, term_lc FROM mesh_term").fetchall():
            term.setdefault(lc, []).append(ui)
        mapped = {}
        for ui, m in c.execute("SELECT ui, mapped_ui FROM mesh_mapped").fetchall():
            mapped.setdefault(ui, []).append(m)
        names = dict(c.execute("SELECT ui, name FROM mesh_record").fetchall())
        reg = {}
        for ui, n in c.execute("SELECT ui, number FROM mesh_registry").fetchall():
            reg.setdefault(n, []).append(ui)
        _mem["mesh"] = dict(term=term, mapped=mapped, names=names, registry=reg)
    return _mem["mesh"]


def stats():
    c = con()
    for (t,) in c.execute("SELECT table_name FROM information_schema.tables ORDER BY 1").fetchall():
        print(f"  {t:24s} {c.execute(f'SELECT count(*) FROM {t}').fetchone()[0]:>10,}")
    print(c.execute("SELECT ns, count(*) FROM kv GROUP BY 1 ORDER BY 1").fetchall())
    print(c.execute("SELECT * FROM bulk_meta").fetchall())


if __name__ == "__main__":
    if sys.argv[1:] == ["load"]:
        import time
        t = time.time()
        print("drugsfda submissions", load_drugsfda(), f"{time.time() - t:.1f}s")
        print("dailymed setid-rxcui", load_dailymed_rxnorm(), f"{time.time() - t:.1f}s")
        print("mesh records/terms/registry/mapped", load_mesh(), f"{time.time() - t:.1f}s")
        print("json caches imported", import_json_caches(), f"{time.time() - t:.1f}s")
        stats()
    else:
        stats()
