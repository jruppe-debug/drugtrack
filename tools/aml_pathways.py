"""Clinician pathway view of the AML tree: patient population -> genetic subgroup -> treatment phase -> regimen -> trials.

Logic follows the treatment algorithms of PMC11966364 (figures 2-5: newly diagnosed <60 fit, 60-75 fit,
>=75 or unfit, relapsed/refractory) and the hemonc.org AML treatment lines (high-intensity induction,
lower-intensity induction, consolidation/maintenance, salvage). Each approved regimen is linked to the trials
that FDA label section 14 names for it (data/label_s14.json, tools/label_s14.py): the lead trial of every AML
approval event that labelled the regimen, plus its co-basis trials. Investigational options shown by the paper
(yellow boxes) are kept, marked INVESTIGATIONAL_REGIMEN, with no label trials.

attach_pathways(tree) puts the pathway branch first and moves the existing phase/approval branches under one
'Drug pipeline by phase' branch. Trial nodes here are copies; attrs.tree_nodes lists the original nodes.
"""
import json, os, re

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
S14 = os.environ.get("LABEL_S14_OUT") or os.path.join(DATA, "label_s14.json")

FIG = {2: "PMC11966364 fig 2", 3: "PMC11966364 fig 3", 4: "PMC11966364 fig 4", 5: "PMC11966364 fig 5"}

# Regimen: (display name, status, [(label_s14 drug, label_s14 regimen key), ...], note).
# status: approved (US label, section 14 trial), standard (long-established agents, no section 14 trial),
# investigational (yellow box in the paper figure).
def R(name, status, keys=(), note=""):
    return dict(name=name, status=status, keys=list(keys), note=note)

VEN_HMA = [("VENETOCLAX", "AZACITIDINE + VENETOCLAX"), ("VENETOCLAX", "DECITABINE + VENETOCLAX")]
GO_IC = [("GEMTUZUMAB OZOGAMICIN", "GEMTUZUMAB OZOGAMICIN + INTENSIVE CHEMOTHERAPY")]
CPX = [("DAUNORUBICIN", "DAUNORUBICIN"), ("CYTARABINE", "CYTARABINE")]

def fit_post_remission():
    return [
        ("Response assessment", [
            R("Refractory: go to relapsed/refractory pathway", "standard", note="paper: refractory disease follows fig 5"),
            R("MRD-positive CR: HMA + venetoclax", "investigational"),
        ]),
        ("Post-remission: favorable risk", [
            R("Consolidation: high-dose cytarabine", "standard"),
            R("Consolidation: high-dose cytarabine + gemtuzumab ozogamicin", "approved", GO_IC,
              "label approves GO with induction and consolidation chemotherapy"),
        ]),
        ("Post-remission: transplant eligible", [
            R("Allogeneic SCT", "standard"),
            R("Allogeneic SCT then maintenance", "investigational", note="FLT3 inhibitor or HMA maintenance after alloSCT"),
        ]),
        ("Post-remission: not transplant eligible", [
            R("Consolidation chemotherapy", "standard"),
            R("Maintenance: oral azacitidine (CC-486)", "approved", [("AZACITIDINE", "AZACITIDINE")],
              "QUAZAR AML-001; continued treatment after first CR/CRi following intensive induction"),
        ]),
    ]

FLT3_IC = [
    ("Induction", [
        R("7+3 + midostaurin", "approved", [("MIDOSTAURIN", "INTENSIVE CHEMOTHERAPY + MIDOSTAURIN")], "FLT3-ITD or TKD"),
        R("7+3 + quizartinib", "approved", [("QUIZARTINIB", "INTENSIVE CHEMOTHERAPY + QUIZARTINIB")], "FLT3-ITD only"),
    ]),
    ("Maintenance", [
        R("Quizartinib maintenance after consolidation", "approved",
          [("QUIZARTINIB", "INTENSIVE CHEMOTHERAPY + QUIZARTINIB")], "label: maintenance monotherapy after consolidation"),
    ]),
]

def fit_population(fig, older):
    other_ind = [R("7+3 (cytarabine + daunorubicin or idarubicin)", "standard")]
    mr_ind = [R("CPX-351 (liposomal daunorubicin/cytarabine)", "approved", CPX, "therapy-related AML or AML with myelodysplasia-related changes")]
    if older:
        other_ind += [R("HMA + venetoclax", "investigational"), R("Decitabine 10-day", "investigational")]
        mr_ind += [R("7+3", "standard"), R("HMA + venetoclax", "investigational")]
    return [
        ("Core-binding factor (t(8;21), inv(16)) or NPM1-mutated, FLT3 wild-type", [
            ("Induction", [R("7+3 + gemtuzumab ozogamicin", "approved", GO_IC, "CD33-positive")]
                          + ([R("7+3", "standard")] if older else [])),
        ] + fit_post_remission()),
        ("FLT3-mutated", FLT3_IC + fit_post_remission()),
        ("No targetable lesion", [("Induction", other_ind)] + fit_post_remission()),
        ("AML-MR or therapy-related AML", [("Induction", mr_ind)] + fit_post_remission()),
        ("TP53-mutated", [("Induction", [R("HMA +/- venetoclax", "investigational"), R("Clinical trial", "investigational")])]
                         + fit_post_remission()),
    ]

UNFIT_POST = [("Post-remission", [R("Continue the same lower-intensity therapy", "standard"),
                                  R("Allogeneic SCT in selected patients", "standard")])]
UNFIT = [
    ("IDH1-mutated", [("Lower-intensity induction", [
        R("HMA + venetoclax", "approved", VEN_HMA),
        R("Azacitidine + ivosidenib", "approved", [("IVOSIDENIB", "AZACITIDINE + IVOSIDENIB")]),
        R("Ivosidenib", "approved", [("IVOSIDENIB", "IVOSIDENIB")], "newly diagnosed, age >=75 or comorbidities"),
    ])] + UNFIT_POST),
    ("IDH2-mutated", [("Lower-intensity induction", [R("HMA + venetoclax", "approved", VEN_HMA)])] + UNFIT_POST),
    ("FLT3-mutated", [("Lower-intensity induction", [R("HMA + venetoclax", "approved", VEN_HMA),
                                                    R("HMA + venetoclax + FLT3 inhibitor", "investigational")])] + UNFIT_POST),
    ("No targetable lesion", [("Lower-intensity induction", [
        R("HMA + venetoclax", "approved", VEN_HMA),
        R("Low-dose cytarabine + venetoclax", "approved", [("VENETOCLAX", "LOW-DOSE CYTARABINE + VENETOCLAX")],
          "US label; not drawn in paper fig 4"),
        R("Low-dose cytarabine + glasdegib", "approved", [("GLASDEGIB", "GLASDEGIB + LOW-DOSE CYTARABINE")],
          "US label; not drawn in paper fig 4"),
        R("Oral decitabine/cedazuridine", "approved", [("CEDAZURIDINE", "CEDAZURIDINE")],
          "US label; not drawn in paper fig 4; check label section 14 for the partner"),
        R("Gemtuzumab ozogamicin monotherapy", "approved", [("GEMTUZUMAB OZOGAMICIN", "GEMTUZUMAB OZOGAMICIN")],
          "label: newly diagnosed CD33-positive AML unfit for intensive chemotherapy (AML-19)"),
    ])] + UNFIT_POST),
    ("TP53-mutated", [("Lower-intensity induction", [R("HMA +/- venetoclax", "investigational"),
                                                    R("Clinical trial", "investigational")])] + UNFIT_POST),
]

RR_POST = [("After remission", [R("Allogeneic SCT if eligible", "standard")])]
RR = [
    ("IDH1-mutated", [("Salvage", [R("Ivosidenib", "approved", [("IVOSIDENIB", "IVOSIDENIB")]),
                                   R("Olutasidenib", "approved", [("OLUTASIDENIB", "OLUTASIDENIB")])])] + RR_POST),
    ("IDH2-mutated", [("Salvage", [R("Enasidenib", "approved", [("ENASIDENIB", "ENASIDENIB")])])] + RR_POST),
    ("FLT3-mutated", [("Salvage", [R("Gilteritinib", "approved", [("GILTERITINIB", "GILTERITINIB")])])] + RR_POST),
    ("KMT2A-rearranged", [("Salvage", [R("Revumenib", "approved", [("REVUMENIB", "REVUMENIB")])])] + RR_POST),
    ("NPM1-mutated", [("Salvage", [R("Revumenib", "approved", [("REVUMENIB", "REVUMENIB")]),
                                   R("Ziftomenib", "approved", [("ZIFTOMENIB", "ZIFTOMENIB")])])] + RR_POST),
    ("No targetable lesion", [("Salvage", [
        R("MEC (mitoxantrone, etoposide, cytarabine)", "standard"),
        R("FLAG-IDA", "standard"),
        R("HMA + venetoclax", "investigational", note="off-label in R/R"),
        R("Gemtuzumab ozogamicin monotherapy", "approved", [("GEMTUZUMAB OZOGAMICIN", "GEMTUZUMAB OZOGAMICIN")],
          "label R/R indication rests on MyloFrance-1 (no NCT id); lead shown is the label's ND study"),
    ])] + RR_POST),
    ("TP53-mutated", [("Salvage", [R("Clinical trial", "investigational")])] + RR_POST),
]

APL = [("Any risk", [("Induction and consolidation", [
    R("Arsenic trioxide + tretinoin (ATRA)", "approved", [("ARSENIC TRIOXIDE", "ARSENIC TRIOXIDE + TRETINOIN")],
      "outside the paper figures (APL excluded); hemonc APL page"),
])])]

POPULATIONS = [
    ("nd_lt60", "Newly diagnosed, age <60, fit for intensive chemotherapy", 2, fit_population(2, False)),
    ("nd_60_75", "Newly diagnosed, age 60-75, fit for intensive chemotherapy", 3, fit_population(3, True)),
    ("nd_unfit", "Newly diagnosed, age >=75 or unfit for intensive chemotherapy", 4, UNFIT),
    ("rr", "Relapsed or refractory", 5, RR),
]
# APL is kept for its own chart and stays out of the AML pathway: ("apl", "Acute promyelocytic leukemia (APL)", None, APL)

CATEGORY = {"approved": "APPROVED_REGIMEN_US_LABEL", "standard": "STANDARD_OF_CARE", "investigational": "INVESTIGATIONAL_REGIMEN"}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:40]


def index_tree(tree):
    """NCT -> [trial/study node], drug name -> drug node id."""
    by_nct, drugs = {}, {}
    def walk(n):
        m = re.search(r"(NCT\d{8})$", n["id"])
        if m and n["kind"] in ("trial", "study"):
            by_nct.setdefault(m.group(1), []).append(n)
        if n["kind"] == "drug":
            # product nodes show a display name; drug_key keeps the match.json name. A product node of a
            # non-joint product (Vyxeos) wins over its plain component node, so CPX regimens link to it.
            key = ((n.get("attrs") or {}).get("drug_key") or n["name"]).upper()
            if (n.get("attrs") or {}).get("product"):
                drugs[key] = n["id"]
            else:
                drugs.setdefault(key, n["id"])
        for c in n.get("children") or []:
            walk(c)
    walk(tree)
    return by_nct, drugs


def label_trials(s14, keys):
    """[(nct, role)] across every AML approval event that labelled one of the keys, oldest first."""
    out = {}
    for drug, key in keys:
        for ev in s14.get("drugs", {}).get(drug, {}).get("events", []):
            rl = (ev.get("regimen_leads") or {}).get(key)
            if not rl:
                continue
            for nct, role in [(rl["lead"], "lead")] + [(n, "co_basis") for n in rl.get("co_basis", [])]:
                e = out.setdefault(nct, dict(nct=nct, roles=[]))
                e["roles"].append(f"{role} {ev['date']} {ev['submission'].lower()}: {key}")
    # Lead trials first, each ordered by its first approval date.
    return sorted(out.values(), key=lambda e: (not any(r.startswith("lead") for r in e["roles"]), e["roles"][0].split()[1]))

KEEP = ("title", "phase", "status", "start_date", "start_year", "completion_date", "enrollment", "lead_sponsor", "arms", "tags", "tag_basis")


def trial_copy(prefix, t, by_nct):
    src = sorted(by_nct.get(t["nct"], []), key=lambda n: n["kind"] != "trial")
    base = src[0] if src else None
    attrs = {k: base["attrs"][k] for k in KEEP if base and k in base["attrs"]}
    attrs.update(nct_id=t["nct"], label_role=t["roles"], tree_nodes=[n["id"] for n in src])
    lead = any(r.startswith("lead") for r in t["roles"])
    name = ("LEAD " if lead else "co-basis ") + t["nct"] + (" · " + attrs.get("title", "")[:60] if base else " (not in this disease tree)")
    return dict(id=f"{prefix}:{t['nct']}", kind="trial", name=name, attrs=attrs,
                provenance=dict(source="FDA label section 14 via data/label_s14.json"), children=[])


def attach_pathways(tree, s14_path=S14):
    s14 = json.load(open(s14_path, encoding="utf-8")) if os.path.exists(s14_path) else {}
    by_nct, drugs = index_tree(tree)
    unlinked = []

    def regimen(pid, r):
        trials = label_trials(s14, r["keys"])
        if r["status"] == "approved" and not trials:
            unlinked.append(r["name"])
        dn = sorted({drugs[d] for d, _ in r["keys"] if d in drugs})
        return dict(id=pid, kind="regimen", name=r["name"],
                    attrs=dict(category=CATEGORY[r["status"]], pathway_status=r["status"], note=r["note"],
                               label_regimens=[k for _, k in r["keys"]], tree_nodes=dn),
                    provenance=dict(source="PMC11966364 figs 2-5, hemonc.org AML treatment lines; trials from FDA label section 14"),
                    children=[trial_copy(pid, t, by_nct) for t in trials])

    pops = []
    for pkey, ptitle, fig, groups in POPULATIONS:
        gnodes = []
        for gname, phases in groups:
            gid = f"path:{pkey}:{slug(gname)}"
            phnodes = []
            for phname, regs in phases:
                phid = f"{gid}:{slug(phname)}"
                phnodes.append(dict(id=phid, kind="branch", name=phname, attrs=dict(pathway_level="phase"),
                                    provenance={}, children=[regimen(f"{phid}:{slug(r['name'])}", r) for r in regs]))
            gnodes.append(dict(id=gid, kind="branch", name=gname, attrs=dict(pathway_level="genetic_subgroup"),
                               provenance={}, children=phnodes))
        pops.append(dict(id=f"path:{pkey}", kind="branch", name=ptitle,
                         attrs=dict(pathway_level="population", source=FIG.get(fig, "hemonc.org APL")),
                         provenance={}, children=gnodes))
    pathway = dict(id="branch:pathway", kind="branch", name="Treatment pathway (patient-based)",
                   attrs=dict(note="Pick the patient's population, then genetic subgroup, then treatment phase. "
                                   "Approved regimens list the FDA label section 14 trials; standard = established "
                                   "agents without a label trial; investigational = paper figure yellow boxes.",
                              label_s14=os.path.basename(s14_path) if s14 else "missing"),
                   provenance=dict(source="PMC11966364 figs 2-5; hemonc.org/wiki/Acute_myeloid_leukemia"),
                   children=pops)
    old = [c for c in tree["children"] if c["id"] not in ("branch:pathway", "branch:pipeline")]
    for c in tree["children"]:
        if c["id"] == "branch:pipeline":
            old = c["children"] + old
    pipeline = dict(id="branch:pipeline", kind="branch", name="Drug pipeline by phase",
                    attrs=dict(note="Previous disease -> phase -> drug -> trial view"), provenance={}, children=old)
    tree["children"] = [pathway, pipeline]
    tree.setdefault("meta", {}).setdefault("counts", {})["pathway"] = dict(unlinked_approved=sorted(set(unlinked)))
    return unlinked


if __name__ == "__main__":
    t = json.load(open(os.path.join(DATA, "tree.json"), encoding="utf-8"))
    print("unlinked approved regimens:", attach_pathways(t))
