"""Load config/defaults.json + config/diseases/<name>.json (disease keys win) and compile the word lists.
The only place disease-specific wording lives. data_dir(name) is where that disease's outputs go
(data/ for AML, the original build; data/diseases/<name>/ for every other disease)."""
import json, os, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_DIR = os.path.join(ROOT, "config", "diseases")
DEFAULTS = os.path.join(ROOT, "config", "defaults.json")
DATA = os.path.join(ROOT, "data")


def data_dir(name):
    d = DATA if name.lower() == "aml" else os.path.join(DATA, "diseases", name.lower())
    os.makedirs(d, exist_ok=True)
    return d


def log(name, tool, msg):
    """One timestamped progress line to stderr and to <data_dir>/run.log (tail it to watch a run live)."""
    import sys, time
    line = f"{time.strftime('%H:%M:%S')} [{tool}] {msg}"
    print(line, file=sys.stderr, flush=True)
    with open(os.path.join(data_dir(name), "run.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load(name="aml"):
    base = json.load(open(DEFAULTS, encoding="utf-8")) if os.path.exists(DEFAULTS) else {}
    c = dict(base)
    c.update(json.load(open(os.path.join(CFG_DIR, f"{name.lower()}.json"), encoding="utf-8")))
    c["_name"] = name.lower()
    c.setdefault("eligibility", {"ineligible": base["eligibility_master"]["ineligible"],
                                 "eligible": base["eligibility_master"]["eligible"], "intensive_regimen": None})
    c.setdefault("settings", base.get("settings_master", {}))
    c.setdefault("class_words", {})
    c.setdefault("biomarkers", [])
    I = re.I
    c["_phases"] = re.compile(r"\b(" + "|".join(re.escape(p) for p in c["phase_words"]) + r")\b", I)
    c["_not_phase"] = re.compile(c["not_phase_context"], I)
    c["_inelig"] = re.compile("|".join(c["eligibility"]["ineligible"]) or r"(?!x)x", I)
    c["_elig"] = re.compile("|".join(c["eligibility"]["eligible"]) or r"(?!x)x", I)
    c["_settings"] = [(k, re.compile(v, I)) for k, v in c["settings"].items()]
    c["_classes"] = re.compile(r"(?<![A-Za-z])(" + ("|".join(re.escape(k) for k in sorted(c["class_words"], key=len, reverse=True))
                                                     or r"(?!x)x") + r")(?![A-Za-z])", I)
    c["_mono"] = re.compile(c["monotherapy_words"], I)
    c["_roles"] = [(k, re.compile(v, I)) for k, v in c["indication_roles"].items()]
    return c
