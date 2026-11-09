#!/usr/bin/env python3
"""Sort validation/scrubber_proposals.jsonl + contrarian_verdicts.jsonl into promoted claims
vs. contested ones. Promotion rule (per handoff): scrubber tier 1/2, OR two-source agreement
(a contrarian verdict of 'survives' with every evidence entry quote_found=true) for anything
else. A contrarian 'disproven' verdict always wins and forces contested, regardless of tier.

Idempotent: re-running only appends claims/contested lines not already recorded by
(source, id-or-target).
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRUBBER = ROOT / "validation/scrubber_proposals.jsonl"
CONTRARIAN = ROOT / "validation/contrarian_verdicts.jsonl"
OVERRIDES = ROOT / "data/curated/validation_overrides.json"
CONTESTED = ROOT / "validation/contested.jsonl"


def load_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def all_quotes_found(verdict):
    evidence = verdict.get("evidence") or []
    return bool(evidence) and all(e.get("quote_found") for e in evidence)


def two_source_agreement(verdict):
    return verdict.get("verdict") == "survives" and all_quotes_found(verdict)


def main():
    scrubber = load_jsonl(SCRUBBER)
    contrarian = load_jsonl(CONTRARIAN)

    verdicts_by_target = {}
    for v in contrarian:
        verdicts_by_target.setdefault(v["target"], []).append(v)
    scrubber_ids = {p["id"] for p in scrubber}

    promoted, contested = [], []

    for p in scrubber:
        votes = verdicts_by_target.get(p["id"], [])
        if any(v.get("verdict") == "disproven" for v in votes):
            contested.append({**p, "source": "scrubber", "contested_reason": "disproven"})
        elif p.get("tier", 99) <= 2:
            promoted.append({**p, "source": "scrubber", "promoted_reason": "tier<=2"})
        elif any(two_source_agreement(v) for v in votes):
            promoted.append({**p, "source": "scrubber", "promoted_reason": "two_source_agreement"})
        else:
            contested.append({**p, "source": "scrubber", "contested_reason": "tier>2_uncorroborated"})

    for v in contrarian:
        if v["target"] in scrubber_ids:
            continue  # disposition already folded into the scrubber proposal above
        if two_source_agreement(v):
            promoted.append({**v, "source": "contrarian", "promoted_reason": "two_source_agreement"})
        else:
            contested.append({**v, "source": "contrarian", "contested_reason": v.get("verdict", "unknown")})

    OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
    doc = json.loads(OVERRIDES.read_text(encoding="utf-8")) if OVERRIDES.exists() else {"claims": []}
    known = {(c.get("source"), c.get("id") or c.get("target")) for c in doc["claims"]}
    added = 0
    for c in promoted:
        key = (c["source"], c.get("id") or c.get("target"))
        if key not in known:
            doc["claims"].append(c)
            known.add(key)
            added += 1
    OVERRIDES.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")

    existing_contested = {
        (c.get("source"), c.get("id") or c.get("target")) for c in load_jsonl(CONTESTED)
    }
    new_contested = [
        c for c in contested
        if (c.get("source"), c.get("id") or c.get("target")) not in existing_contested
    ]
    with CONTESTED.open("a", encoding="utf-8") as f:
        for c in new_contested:
            f.write(json.dumps(c) + "\n")

    print(f"promoted {added} new claim(s) -> {OVERRIDES.relative_to(ROOT)} ({len(doc['claims'])} total)")
    print(f"contested {len(new_contested)} new line(s) -> {CONTESTED.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
