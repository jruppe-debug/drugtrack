"""Approval-anchored central trials: for every FDA-approved drug in the AML match set, find the trial(s) the FDA approval
package names as efficacy evidence, so the drug's central study is the trial the approval rests on.

Per drug (approved = any Drugs@FDA application with an approval event on or after the ClinicalTrials.gov launch):
  1. Candidates = every AML pair of the drug in data/match.json (all decisions, all layers).
  2. Documents, in date order per approval event: Label PDF; the Review (MedR/SumR/Multidiscipline) only for an original
     approval whose label names no candidate; the FDA oncology approval announcement page when the notifications index links
     one for the drug (index covers recent years only; URLs are never guessed).
  3. Hits: candidate NCT IDs named in the text, plus study IDs written as 'Study <ID>' / '(<ID>)' tokens that resolve to a
     candidate through the ClinicalTrials.gov record (org study ID, secondary IDs, acronym, curated aliases). Exact matches only.
     A trial named only in postmarketing-requirement context is a confirmatory_pmr hit, not efficacy evidence.
  4. Each trial is credited to the earliest approval event whose documents name it as efficacy evidence.
  5. Year-sort validation: the trial must start before that approval date; a violation is kept but flagged needs_review.
  6. Approved regimen: the drug plus the trial's registered partner drugs named within the text around the trial's mention.
     'monotherapy' / 'single agent' wording there, with no partner named, means monotherapy.

Output: data/central_anchor.json (read by tools/match_pipeline.py to pick central studies).
"""
import json, os, re, sys, time
from collections import defaultdict
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from approval_anchor import (DATA, REF, REGISTRY_START, PMR_RX, approval_records, ctgov, events_for, fetch_doc, find_ids, get,
                             id_context, norm_id, review_pdfs)

OUT = os.path.join(DATA, "central_anchor.json")
PARTIAL = OUT + ".partial"
INDEX_URL = "https://www.fda.gov/drugs/resources-information-approved-drugs/oncology-cancer-hematologic-malignancies-approval-notifications"
PHASE_RANK = {"PHASE4": 6, "PHASE3": 5, "PHASE2/PHASE3": 4, "PHASE2": 3, "PHASE1/PHASE2": 2, "PHASE1": 1, "EARLY_PHASE1": 0}
STUDY_TOKEN_RX = re.compile(r"(?:\b(?:Study|Trial|study|trial)\s+|\()([A-Z0-9][A-Za-z0-9]*(?:[-_/.][A-Za-z0-9]+)+|[A-Z][A-Z0-9]{3,}-?[A-Z0-9]*)")
MONO_RX = re.compile(r"monotherapy|single[- ]agent|as a single", re.I)
# Scope gate for approval events: the approval letter (or an FDA announcement within 7 days) must name AML.
# Older labels say acute nonlymphocytic / myeloblastic leukemia; APL is an AML subtype.
SCOPE_RX = re.compile(r"acute myel\w* leuk|\bAML\b|acute non-?lymphocytic leuk|\bANLL\b|acute promyelocytic leuk|\bAPL\b|\bacute leukemias?\b", re.I)
# A hit whose every context says effectiveness was not shown (e.g. 8.4 Pediatric Use) is not an efficacy basis.
NOT_EST_RX = re.compile(r"not (?:been )?established|Pediatric Use", re.I)
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"


def resolve_token(tok):
    """ClinicalTrials.gov full-text search for a study ID token; cached. Returns NCT IDs of the first page."""
    d = os.path.join(REF, "ctgov_search")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, re.sub(r"[^A-Za-z0-9_-]", "_", tok) + ".json")
    if not os.path.exists(p):
        r = get("https://clinicaltrials.gov/api/v2/studies", params={"query.term": f'"{tok}"', "pageSize": 20, "fields": "NCTId"})
        ids = [s["protocolSection"]["identificationModule"]["nctId"] for s in r.json().get("studies", [])] if r.status_code == 200 else []
        json.dump(ids, open(p, "w", encoding="utf-8"))
        time.sleep(0.3)
    return json.load(open(p, encoding="utf-8"))


def announcements():
    """FDA oncology approval notifications: slug -> page text (cached). Only pages linked from the index."""
    d = os.path.join(REF, "fda", "announcements")
    os.makedirs(d, exist_ok=True)
    idx = os.path.join(d, "_index.html")
    if not os.path.exists(idx):
        r = get(INDEX_URL)
        open(idx, "w", encoding="utf-8").write(r.text if r.status_code == 200 else "")
    links = sorted(set(re.findall(r'href="(/drugs/resources-information-approved-drugs/[^"#?]+)"', open(idx, encoding="utf-8").read())))
    return [l for l in links if "approval-notifications" not in l]


def announcement_text(slug):
    p = os.path.join(REF, "fda", "announcements", slug.rsplit("/", 1)[-1] + ".txt")
    if not os.path.exists(p):
        r = get("https://www.fda.gov" + slug)
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text)) if r.status_code == 200 else ""
        open(p, "w", encoding="utf-8").write(txt)
        time.sleep(0.4)
    return open(p, encoding="utf-8").read()


def announcement_date(text):
    m = re.search(rf"On ({MONTHS}) (\d{{1,2}}), (\d{{4}}), the Food and Drug Administration", text)
    if not m:
        return None
    return f"{m.group(3)}-{['January','February','March','April','May','June','July','August','September','October','November','December'].index(m.group(1)) + 1:02d}-{int(m.group(2)):02d}"


def drug_terms(drugs):
    """Canonical drug name -> lowercase names to look for in FDA text."""
    out = {}
    for v in drugs.values():
        names = {v["drug"], *(v.get("arm_terms") or []), *(v.get("trade_names") or [])}
        out[v["drug"]] = sorted({n.lower() for n in names if n and len(n) > 3})
    return out


def regimen_from_text(ctx, partners, terms):
    """Approved partner drugs = the trial's registered partners named near the trial's mention."""
    blob = " ".join(ctx).lower()
    named = sorted(p for p in partners if any(re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", blob) for t in terms.get(p, [p.lower()])))
    mono = bool(MONO_RX.search(blob))
    return named, ("monotherapy" if mono and not named else "combination" if named else "monotherapy (no partner named)")


def save(out, path):
    """Write via a temp file so a stop mid-write never leaves a truncated JSON."""
    json.dump(out, open(path + ".tmp", "w", encoding="utf-8"), indent=1)
    os.replace(path + ".tmp", path)


def run():
    m = json.load(open(os.path.join(DATA, "match.json"), encoding="utf-8"))
    terms = drug_terms(m["drugs"])
    by_drug = defaultdict(list)
    for p in m["pairs"]:
        by_drug[p["drug"]].append(p)
    recs = approval_records(sorted(by_drug))
    slugs = announcements()
    out = dict(generated=date.today().isoformat(), registry_start=REGISTRY_START, scope=m["meta"].get("scope_name"), drugs={})
    only = {a.upper() for a in sys.argv[1:]}   # optional drug-name filter for pilots; output then holds only those drugs
    if not only:
        # Full runs cover only drugs approved at or near the scope (tree.json category != INVESTIGATIONAL).
        # An investigational drug has no FDA package for this disease; scanning its other approvals only finds
        # basket/safety mentions (e.g. alpelisib's review overdose listing). tree.json comes from the previous build.
        cats = {}
        def walk(n):
            a = n.get("attrs") or {}
            if n.get("kind") == "drug":
                cats[n["name"]] = a.get("category")
            for c in n.get("children") or []:
                walk(c)
        walk(json.load(open(os.path.join(DATA, "tree.json"), encoding="utf-8")))
        only_scope = {d for d, c in cats.items() if c == "APPROVED_AT_SCOPE"}   # US gate: supplemental/excluded drugs are not anchors
        print("approved drugs in scope:", len(only_scope))
        # Full runs save after every drug to OUT.partial and resume from it (and from pilot results in OUT),
        # so a stopped run loses at most one drug.
        for path in (OUT, PARTIAL):
            if os.path.exists(path):
                prev = json.load(open(path, encoding="utf-8"))
                if prev.get("scope") == out["scope"]:
                    out["drugs"].update({d: v for d, v in prev["drugs"].items() if d in only_scope})
        print("resuming:", len(out["drugs"]), "drugs already done")
    saved_n = len(out["drugs"])
    for drug, pairs in sorted(by_drug.items()):
        if only and drug not in only:
            continue
        if not only and (drug not in only_scope or drug in out["drugs"]):
            continue
        if not only and len(out["drugs"]) != saved_n:
            save(out, PARTIAL)
            saved_n = len(out["drugs"])
        rec = recs.get(drug) or {}
        if rec.get("status") != "found":
            continue
        cand = {p["nct_id"]: p for p in pairs}
        evs = [e for e in events_for(rec["apps"]) if (e["date"] or "") >= REGISTRY_START]
        if not evs:
            continue
        # Announcement pages for this drug (index links only), keyed to the nearest approval event within 7 days.
        ann = []
        for s in slugs:
            if any(t.replace(" ", "-") in s for t in terms.get(drug, [drug.lower()])):
                txt = announcement_text(s)
                ann.append(dict(url="https://www.fda.gov" + s, date=announcement_date(txt), text=txt))
        # AML events only: an event counts when its approval letter names AML, or (no letter) an announcement
        # within 7 days does. Events for other diseases (venetoclax CLL, azacitidine MDS) are never walked or credited.
        gate = []
        for e in evs:
            letters = e["docs"].get("Letter") or []
            near = [a for a in ann if a["date"] and abs((date.fromisoformat(a["date"]) - date.fromisoformat(e["date"])).days) <= 7]
            if letters:
                ok = any(SCOPE_RX.search(fetch_doc(u, e["application"])[1] or "") for u in letters)
                verdict = "aml" if ok else "non_aml"
            elif near:
                verdict = "aml" if any(SCOPE_RX.search(a["text"]) for a in near) else "non_aml"
            else:
                verdict = "unknown"
            gate.append(dict(application=e["application"], submission=e["submission"], date=e["date"], verdict=verdict,
                             basis="letter" if letters else "announcement" if near else "none"))
        aml_keys = {(g["application"], g["submission"], g["date"]) for g in gate if g["verdict"] == "aml"}
        evs = [e for e in evs if (e["application"], e["submission"], e["date"]) in aml_keys]
        print(drug, "AML events", len(evs), "of", len(gate), "unknown", sum(g["verdict"] == "unknown" for g in gate))
        if not evs:
            out["drugs"][drug] = dict(chembl_root=pairs[0]["chembl_root"], status="no_aml_event", events=0, first_approval=None, event_gate=gate)
            continue
        credited, ev_log, ids_cache = {}, [], {}
        # Screen first (latest label + original label per application, announcements); walk every event only for
        # applications whose screen names a candidate. Keeps document fetches small for supportive-care drugs.
        screen = {}
        for e in evs:
            if e["docs"].get("Label"):   # evs is date-sorted, so "latest" ends on the newest labelled event
                screen.setdefault(e["application"], {})["ORIG" if e["submission"] == "ORIG" else "latest"] = e
        live_apps = set()
        for app, picks in screen.items():
            texts = [("label", u, *fetch_doc(u, app)[1:]) for e in picks.values() for u in e["docs"]["Label"]]
            if scan(texts, cand, ids_cache):
                live_apps.add(app)
        if not live_apps:   # labels name no candidate: fall back to original-approval reviews
            for e in evs:
                if e["submission"] == "ORIG" and e["docs"].get("Review"):
                    texts = [("review", u, *fetch_doc(u, e["application"])[1:]) for u in review_pdfs(e["docs"]["Review"])]
                    if scan(texts, cand, ids_cache):
                        live_apps.add(e["application"])
        if ann:
            live_apps |= {e["application"] for e in evs}
        for e in evs:
            if e["application"] not in live_apps:
                continue
            texts = []
            for u in e["docs"].get("Label", []):
                path, txt, status = fetch_doc(u, e["application"])
                texts.append(("label", u, txt, status))
            for a in ann:
                if a["date"] and abs((date.fromisoformat(a["date"]) - date.fromisoformat(e["date"])).days) <= 7:
                    texts.append(("fda_announcement", a["url"], a["text"], "ok"))
            hits = scan(texts, cand, ids_cache)
            if not hits and e["submission"] == "ORIG" and e["docs"].get("Review"):
                for u in review_pdfs(e["docs"]["Review"]):
                    path, txt, status = fetch_doc(u, e["application"])
                    texts.append(("review", u, txt, status))
                hits = scan(texts, cand, ids_cache)
            ev_log.append(dict(application=e["application"], submission=e["submission"], date=e["date"], kind=e["kind"],
                               docs=[dict(source=s, url=u, status=st, chars=len(t)) for s, u, t, st in texts],
                               hits=[dict(nct=n, **{k: v for k, v in h.items() if k != "ctx"}) for n, h in hits.items()]))
            for nct, h in hits.items():
                if h["role"] == "efficacy_basis" and nct not in credited:
                    credited[nct] = dict(event=e, hit=h)
        if not credited and not any(ev["hits"] for ev in ev_log):
            out["drugs"][drug] = dict(chembl_root=pairs[0]["chembl_root"], status="no_candidate_named", events=len(ev_log),
                                      first_approval=evs[0]["date"], event_gate=gate)
            continue
        anchors = []
        for nct, c in credited.items():
            p, e, h = cand[nct], c["event"], c["hit"]
            partners, rtype = regimen_from_text(h["ctx"], p.get("regimen") or [], terms)
            checks = []
            if not p.get("start_date"):
                checks.append("no registry start date")
            elif p["start_date"] >= e["date"]:
                checks.append(f"trial starts {p['start_date']}, on/after approval {e['date']}")
            anchors.append(dict(nct=nct, approval_date=e["date"], application=e["application"], submission=e["submission"], kind=e["kind"],
                                sources=h["sources"], ids=h["ids"], phase=p["phase"], start_date=p.get("start_date"), status=p["status"],
                                layer=p.get("layer"), decision=p["decision"], approved_regimen_key=" + ".join(sorted({drug, *partners})),
                                regimen_type=rtype, registered_regimen_key=p["regimen_key"],
                                validation="needs_review" if checks else "verified", checks=checks,
                                snippet=re.sub(r"\s+", " ", h["ctx"][0])[:600] if h["ctx"] else None))
        # Within one approval the lead is the highest phase, then earliest start; the rest are co-basis.
        anchors.sort(key=lambda a: (a["approval_date"], -PHASE_RANK.get(a["phase"] or "", -1), a["start_date"] or "9999", a["nct"]))
        seen_ev = set()
        for a in anchors:
            k = (a["application"], a["approval_date"])
            a["role"] = "co_basis" if k in seen_ev else "lead"
            seen_ev.add(k)
        out["drugs"][drug] = dict(chembl_root=pairs[0]["chembl_root"], status="anchored" if anchors else "pmr_only", first_approval=evs[0]["date"],
                                  anchors=anchors, events=ev_log, event_gate=gate)
        print(drug, [(a["nct"], a["approval_date"], a["role"], a["approved_regimen_key"], a["validation"]) for a in anchors])
    save(out, OUT)
    if not only and os.path.exists(PARTIAL):
        os.remove(PARTIAL)
    print("wrote", OUT, sum(v["status"] == "anchored" for v in out["drugs"].values()), "anchored of", len(out["drugs"]))


def scan(texts, cand, ids_cache):
    """Candidate trials named in the event's documents. Returns nct -> {sources, ids, role, ctx}."""
    hits = {}
    for src, url, txt, _ in texts:
        if not txt:
            continue
        found = {n: [("nct", n)] for n in set(re.findall(r"NCT\d{8}", txt)) if n in cand}
        for tok in set(STUDY_TOKEN_RX.findall(txt)):
            if tok.startswith("NCT") or len(norm_id(tok)) < 5:
                continue
            for n in resolve_token(tok):
                if n in cand:
                    if n not in ids_cache:
                        ids_cache[n] = (ctgov(n) or {}).get("ids", [("nct", n)])
                    # The token must be one of the trial's own registered or curated IDs, not a full-text coincidence.
                    if any(norm_id(v) == norm_id(tok) for _, v in ids_cache[n]):
                        found.setdefault(n, []).append(next((k, v) for k, v in ids_cache[n] if norm_id(v) == norm_id(tok)))
        # Curated aliases (sponsor protocol IDs, acronyms) are not in the registry text, so full-text search misses them.
        for n in cand:
            if n not in ids_cache:
                ids_cache[n] = (ctgov(n) or {}).get("ids", [("nct", n)])
            for k, v in ids_cache[n]:
                if k == "alias" and len(norm_id(v)) >= 5 and re.search(rf"(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])", txt):
                    if (k, v) not in found.setdefault(n, []):
                        found[n].append((k, v))
        for n, ids in found.items():
            ctx = id_context(txt, ids, width=500)
            h = hits.setdefault(n, dict(sources=[], ids=[], ctx=[], role="confirmatory_pmr"))
            h["sources"].append(src)
            h["ids"] += [f"{k}:{v}" for k, v in ids if f"{k}:{v}" not in h["ids"]]
            h["ctx"] += ctx
            if ctx and all(NOT_EST_RX.search(c) for c in ctx):
                if h["role"] == "confirmatory_pmr":
                    h["role"] = "not_established"
            elif not (ctx and all(PMR_RX.search(c) for c in ctx)):
                h["role"] = "efficacy_basis"
    for h in hits.values():
        h["sources"] = sorted(set(h["sources"]))
    return hits


if __name__ == "__main__":
    run()
