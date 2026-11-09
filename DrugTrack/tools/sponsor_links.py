"""
Sponsor page linkage for the AML finder, by static fetch only (no browser automation).

Approved drugs: the brand website comes from the FDA label itself (the SPL XML cached by tools/dailymed_label.py
names it, e.g. www.xospata.com). That page is fetched, its links to a healthcare-professional (HCP) site are
followed, and an HCP page counts as validated only when it loads and names the brand, the generic and AML.

Investigational regimens: candidate sponsor pipeline pages are listed in PIPELINE_CANDIDATES (each with the
reason it was chosen). A page counts as validated only when it loads and names the drug code or name.
Pages that need JavaScript to render or block scripted clients stay 'unvalidated' with the HTTP status or reason.

Output: data/sponsor_links.json. attach(fd) copies it onto finder drug entries (drug['sponsor']).
"""
import glob, json, os, re, sys, urllib.error, urllib.parse, urllib.request
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
OUT = os.path.join(DATA, "sponsor_links.json")
VCACHE = os.path.join(DATA, "dailymed", "versions")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DrugTrack-link-check/1.0"
NOT_BRAND = re.compile(r"fda\.gov|dailymed|nlm\.nih|clinicaltrials|osha\.gov|w3\.org|nccn\.org|rems\b|rems\.com", re.I)
HCP_LINK = re.compile(r"\bhcp\b|health ?care professional|for (?:us )?healthcare|medical professional", re.I)
AML = re.compile(r"acute myeloid leukemia|\bAML\b")
# An HCP page must say so in its URL or in an audience statement; a consumer page's "Healthcare Professionals" menu link is not enough.
HCP_URL = re.compile(r"hcp|pro\.com|/pro\b|professional", re.I)
HCP_TEXT = re.compile(r"intended for (?:U\.?S\.? )?(?:health ?care|medical) professionals|for (?:U\.?S\.? )?health ?care professionals only", re.I)

# Investigational agents in the finder without an AML label. url = sponsor pipeline page to check; why = how chosen.
# Chosen from the tree: agents in the Phase 3 and Phase 2/3 branches with an active industry-sponsored trial (AACT lead_sponsor).
_WHY = "active industry trial {nct} (AACT lead sponsor {sponsor}); sponsor pipeline page"
PIPELINE_CANDIDATES = {
    "APG-2575": dict(sponsor="Ascentage Pharma", nct="NCT06389292", url="https://ascentage.com/pipeline/", terms=["APG-2575", "lisaftoclax"]),
    "BLEXIMENIB": dict(sponsor="Janssen (Johnson & Johnson)", nct="NCT06852222", url="https://www.investor.jnj.com/pipeline/Innovative-Medicine-pipeline/default.aspx", terms=["bleximenib", "JNJ-75276617"]),
    "GALINPEPIMUT-S": dict(sponsor="SELLAS Life Sciences", nct="NCT04229979", url="https://www.sellaslifesciences.com/pipeline/", terms=["galinpepimut"]),
    "IODINE I 131 APAMISTAMAB": dict(sponsor="Actinium Pharmaceuticals", nct="NCT02665065", url="https://www.actiniumpharma.com/product-pipeline/iomab-b", terms=["Iomab-B", "apamistamab"]),
    "MOCRAVIMOD": dict(sponsor="Priothera", nct="NCT05429632", url="https://priothera.com/science/our-pipeline/", terms=["mocravimod"]),
    "RANOSIDENIB": dict(sponsor="HUTCHMED", nct="NCT06387069", url="https://www.hutch-med.com/pipeline-and-products/oncology-immunology/", terms=["ranosidenib", "HMPL-306"]),
    "PIVEKIMAB SUNIRINE": dict(sponsor="AbbVie", nct="NCT07306832", url="https://www.abbvie.com/science/pipeline.html", terms=["pivekimab", "ABBV-382", "PVEK"]),
}
for _c in PIPELINE_CANDIDATES.values():
    _c["why"] = _WHY.format(**_c)


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self._a, self.text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._a = [dict(attrs).get("href") or "", ""]

    def handle_data(self, data):
        self.text.append(data)
        if self._a is not None:
            self._a[1] += data

    def handle_endtag(self, tag):
        if tag == "a" and self._a is not None:
            self.links.append((self._a[0], " ".join(self._a[1].split())))
            self._a = None


def fetch(url, timeout=25):
    """(final_url, status, html or '', error or None)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.geturl(), r.status, r.read(3_000_000).decode("utf-8", "ignore"), None
    except urllib.error.HTTPError as e:
        return url, e.code, "", f"HTTP {e.code}"
    except Exception as e:  # DNS, TLS, timeout: recorded, never fatal
        return url, None, "", type(e).__name__ + ": " + str(e)[:120]


def label_sites(setid, version):
    urls = []
    for x in glob.glob(os.path.join(VCACHE, f"{setid}_v{version}*.xml")) + glob.glob(os.path.join(VCACHE, f"{setid}_v{version}", "*.xml")):
        s = open(x, encoding="utf-8", errors="ignore").read()
        for u in re.findall(r"\b(?:https?://)?www\.[a-z0-9.-]+\.[a-z]{2,}(?:/[^\s<\"\]]*)?", s, re.I):
            u = u.rstrip(".]").lower()
            if not NOT_BRAND.search(u):
                urls.append(u if u.startswith("http") else "https://" + u)
    return list(dict.fromkeys(urls))


def page_check(html, terms):
    p = Links()
    try:
        p.feed(html)
    except Exception:
        pass
    text = " ".join(" ".join(p.text).split())
    found = {t: bool(re.search(re.escape(t), text, re.I)) for t in terms if t}
    return p.links, text, found


def check_approved(drug, e):
    brand = re.match(r"([A-Z0-9-]+)", e.get("title") or "")
    brand = brand.group(1).title() if brand else ""
    sites = label_sites(e.get("setid"), e.get("version"))
    sites = list(dict.fromkeys(re.sub(r"^http://", "https://", u).rstrip("/") for u in sites))
    inferred = not sites and bool(brand)
    if inferred:  # label names no website: try the brand.com convention, validated by page content like any other
        sites = [f"https://www.{brand.lower()}.com"]
    rec = dict(site_inferred=inferred,drug=drug, brand=brand, label_sites=sites, label=e.get("page_url"), brand_site=None, hcp=None, tried=[])
    for site in [s for s in sites if brand and brand.lower() in s] or sites:
        final, status, html, err = fetch(site)
        rec["tried"].append(dict(url=site, final=final, status=status, error=err))
        if not html:
            continue
        links, text, found = page_check(html, [brand, drug.split()[0]])
        rec["brand_site"] = dict(url=final, status=status, names_brand=found.get(brand, False))
        cands = []
        for href, txt in links:
            if href and not href.startswith(("javascript:", "mailto:", "tel:", "#")) and (HCP_LINK.search(href) or HCP_LINK.search(txt)):
                cands.append(urllib.parse.urljoin(final, href))
        host = urllib.parse.urlparse(final).netloc
        b = brand.lower()
        # Common patterns, checked last and only validated by their content
        cands += [f"https://www.{b}hcp.com", f"https://www.{b}pro.com", f"https://{host}/hcp"]
        for c in [x for x in dict.fromkeys(cands) if x.rstrip("/") != final.rstrip("/")][:8]:
            f2, st2, h2, err2 = fetch(c)
            rec["tried"].append(dict(url=c, final=f2, status=st2, error=err2))
            if not h2:
                continue
            _, t2, fd2 = page_check(h2, [brand, drug.split()[0]])
            hcpish = bool(HCP_URL.search(f2) or HCP_TEXT.search(t2))
            # the HCP page must not be the brand page and must stay with the brand (no generic sponsor portal)
            on_brand = b in f2.lower() or b in urllib.parse.urlparse(f2).netloc
            if f2.rstrip("/") != final.rstrip("/") and on_brand and fd2.get(brand) and AML.search(t2) and hcpish:
                rec["hcp"] = dict(url=f2, status=st2, validated=True,
                                  evidence=dict(names_brand=True, names_generic=fd2.get(drug.split()[0], False), names_aml=True,
                                                efficacy_words=bool(re.search(r"efficacy|overall survival|complete remission", t2, re.I))))
                break
        if rec["hcp"]:
            break
    rec["status"] = "validated" if rec["hcp"] else ("brand_site_only" if rec["brand_site"] else "unreachable")
    return rec


def check_pipeline(name, c):
    final, status, html, err = fetch(c["url"])
    rec = dict(drug=name, sponsor=c.get("sponsor"), nct=c.get("nct"), url=c["url"], why=c.get("why"), final=final, status=status, error=err)
    if html:
        _, text, found = page_check(html, c.get("terms") or [name])
        rec["found_terms"] = found
        rec["validated"] = any(found.values())
        if not rec["validated"]:
            rec["error"] = "page loaded but does not name the drug (may render with JavaScript)"
    else:
        rec["validated"] = False
    return rec


def attach(fd, path=OUT):
    if not os.path.exists(path):
        return 0
    s = json.load(open(path, encoding="utf-8"))
    by = {k.upper(): v for k, v in s["approved"].items()} | {k.upper(): v for k, v in s["pipeline"].items()}
    fd["sponsor_links"] = dict(method=s["method"], pipeline=list(s["pipeline"].values()))
    for d in fd.get("drug_census") or []:
        hit = by.get(d["name"].upper())
        if hit:
            d["sponsor"] = hit
    n = 0
    for p in fd["populations"].values():
        for g in p["groups"].values():
            for ph in g["phases"]:
                for r in ph["regimens"]:
                    for d in r["drugs"]:
                        hit = by.get(d["name"].upper()) or by.get(d["name"].split()[0].upper())
                        if hit:
                            d["sponsor"] = hit
                            n += 1
    return n


def main():
    le = json.load(open(os.path.join(DATA, "label_evidence.json"), encoding="utf-8"))["drugs"]
    out = dict(method=__doc__.strip().split("\n\n")[1], approved={}, pipeline={})
    for drug, e in le.items():
        if not e.get("approval_basis"):
            continue
        r = check_approved(drug, e)
        out["approved"][drug] = r
        print(f"{drug[:24]:24} {r['brand']:10} {r['status']:16} {(r['hcp'] or r['brand_site'] or {}).get('url', '')}")
    for name, c in PIPELINE_CANDIDATES.items():
        r = check_pipeline(name, c)
        out["pipeline"][name] = r
        print(f"{name[:24]:24} pipeline {'validated' if r['validated'] else 'unvalidated':12} {r['status']} {r['url']} {r.get('error') or ''}")
    json.dump(out, open(OUT, "w", encoding="utf-8"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    sys.exit(main())
