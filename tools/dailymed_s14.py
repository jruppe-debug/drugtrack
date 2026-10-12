"""DailyMed cross-check of label section 14 (Clinical Studies) against the central trial of each approved AML drug.

For every drug in data/label_s14.json:
  1. Look up the current SPL for each approval application (NDA/BLA) via the DailyMed v2 API.
  2. Fetch the SPL XML and cut the Clinical Studies section by its LOINC code, 34092-7.
     (48780-1 is the "SPL product data elements section", not Clinical Studies.)
  3. Keep the AML subsections (14.x titles naming AML), or the whole section when no subsection names AML.
  4. Collect the NCT ids named there and compare them with the label_s14 trials and the match.json central trial.

DailyMed holds the CURRENT label; label_s14 reads the label of each approval event, so a trial can drop out of
the current label (or be added) without either source being wrong. Mismatches are reported, not resolved.

Output: data/dailymed_s14.json. SPL XML is cached in data/dailymed/.
"""
import json, os, re, sys, time, urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(DATA, "dailymed")
API = "https://dailymed.nlm.nih.gov/dailymed/services/v2"
NS = {"v3": "urn:hl7-org:v3"}
LOINC_CLINICAL_STUDIES = "34092-7"
NCT = re.compile(r"NCT\d{8}")
AML = re.compile(r"acute myeloid leukemia|\bAML\b", re.I)


def get(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": "DrugTrack/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            return body if binary else json.loads(body)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def spls_for(application):
    return get(f"{API}/spls.json?application_number={application}")["data"]


def spl_xml(setid):
    path = os.path.join(CACHE, f"{setid}.xml")
    if not os.path.exists(path):
        os.makedirs(CACHE, exist_ok=True)
        with open(path, "wb") as f:
            f.write(get(f"{API}/spls/{setid}.xml", binary=True))
    return ET.parse(path).getroot()


def text(el):
    return " ".join(" ".join(el.itertext()).split())


def clinical_studies(root):
    """Return [(subsection title, text)] for the AML part of the Clinical Studies section."""
    out = []
    for sec in root.iter("{urn:hl7-org:v3}section"):
        code = sec.find("v3:code", NS)
        if code is None or code.get("code") != LOINC_CLINICAL_STUDIES:
            continue
        subs = []
        for comp in sec.findall("v3:component/v3:section", NS):
            title = comp.find("v3:title", NS)
            subs.append((text(title) if title is not None else "", text(comp)))
        aml = [s for s in subs if AML.search(s[0])]
        out.extend(aml if aml else [("(whole section)", text(sec))])
    return out


def run():
    s14 = json.load(open(os.path.join(DATA, "label_s14.json"), encoding="utf-8"))["drugs"]
    pairs = json.load(open(os.path.join(DATA, "match.json"), encoding="utf-8"))["pairs"]
    central = {p["drug"]: p["nct_id"] for p in pairs if p.get("relation") == "central"}
    report = {}
    for drug in sorted(s14):
        events = s14[drug]["events"]
        s14_ncts = sorted({n for e in events for st in e["studies"] for n in st["ncts"]})
        apps = sorted({e["application"] for e in events})
        labels, dm_ncts = [], set()
        for app in apps:
            for spl in spls_for(app):
                subs = clinical_studies(spl_xml(spl["setid"]))
                ncts = sorted({n for _, t in subs for n in NCT.findall(t)})
                dm_ncts.update(ncts)
                labels.append({"application": app, "setid": spl["setid"], "title": spl["title"],
                               "spl_version": spl["spl_version"], "published": spl["published_date"],
                               "subsections": [s[0] for s in subs], "ncts": ncts})
        c = central.get(drug)
        status = ("no_spl" if not labels else "no_section" if not any(l["subsections"] for l in labels)
                  else "no_nct_in_label" if not dm_ncts
                  else "match" if c in dm_ncts else "central_missing")
        report[drug] = {"central": c, "status": status, "dailymed_ncts": sorted(dm_ncts), "label_s14_ncts": s14_ncts,
                        "only_dailymed": sorted(dm_ncts - set(s14_ncts)),
                        "only_label_s14": sorted(set(s14_ncts) - dm_ncts), "labels": labels}
        print(f"{drug:22} {status:16} central={c} dailymed={sorted(dm_ncts)} only_s14={report[drug]['only_label_s14']}")
    with open(os.path.join(DATA, "dailymed_s14.json"), "w", encoding="utf-8") as f:
        json.dump({"source": f"DailyMed SPL, LOINC {LOINC_CLINICAL_STUDIES}", "drugs": report}, f, indent=1)


if __name__ == "__main__":
    run()
