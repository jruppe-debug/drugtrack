"""Progress report for the scrubber and contrarian validators. Read-only: it never writes a file.

Joins validation/scrubber_proposals.jsonl to validation/contrarian_verdicts.jsonl and reports, per agent,
work done, backlog, last activity, herdr liveness and violations of the agent output contract.

Run from DrugTrack/:  ../.venv/Scripts/python.exe tools/validator_status.py [--json] [--disease aml_gen]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AGENTS = ("scrubber", "contrarian")


def read_jsonl(path):
    if not path.exists():
        return []
    with open(path, encoding="utf8") as f:
        return [json.loads(line) for line in f if line.strip()]


def mtime(path):
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def summary_events(path):
    """Parse '<ISO time> <agent> <task>: ...' lines. Other lines are ignored."""
    if not path.exists():
        return []
    pattern = re.compile(r"^(\S+Z)\s+(scrubber|contrarian)\s+([SC]\d):\s*(.*)$")
    with open(path, encoding="utf8") as f:
        return [{"time": m[1], "agent": m[2], "task": m[3], "text": m[4]} for m in map(pattern.match, f) if m]


def herdr_liveness():
    """Names of validators herdr reports, or None when herdr cannot be asked."""
    if shutil.which("herdr"):
        command = ["herdr", "agent", "list"]
    elif shutil.which("wsl.exe"):
        command = ["wsl.exe", "-e", "sh", "-lc", "herdr agent list"]
    else:
        return None
    try:
        output = subprocess.run(command, capture_output=True, timeout=30).stdout
        agents = json.loads(output.replace(b"\x00", b"").decode("utf8"))["result"]["agents"]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return None
    text = json.dumps(agents).lower()
    return [name for name in AGENTS if name in text]


def contract_violations(proposals, verdicts):
    out = []
    counts = Counter(p.get("id") for p in proposals)
    out += [f"duplicate proposal id: {key}" for key, n in counts.items() if n > 1]
    for p in proposals:
        if p.get("tier") == 1 and not p.get("ids", {}).get("mesh"):
            out.append(f"{p.get('id')}: tier 1 claims an exact MeSH match but ids.mesh is empty")
        if not p.get("evidence") or not all(e.get("url") and e.get("quote") for e in p["evidence"]):
            out.append(f"{p.get('id')}: evidence needs a url and a verbatim quote")
    known = {p.get("id") for p in proposals}
    for v in verdicts:
        missing_quote = any(e.get("quote_found") is False for e in v.get("evidence", []))
        if v.get("task") == "C2" and v.get("target") not in known:
            out.append(f"{v.get('target')}: C2 verdict targets an unknown proposal")
        if v.get("verdict") == "survives" and missing_quote:
            out.append(f"{v.get('target')}: survives with a quote that was not found")
    return out


def build_report(disease):
    validation = REPO / "validation"
    disease_dir = REPO / "data" / "diseases" / disease
    proposals = read_jsonl(validation / "scrubber_proposals.jsonl")
    verdicts = read_jsonl(validation / "contrarian_verdicts.jsonl")
    events = summary_events(validation / "summary.md")
    judged = {v.get("target") for v in verdicts if v.get("task") == "C2"}
    backlog = [p["id"] for p in proposals if p.get("id") not in judged]
    review_input, current = (disease_dir / "contrarian_review.json", disease_dir / "regimen_verification.json")
    last = {a: max((e["time"] for e in events if e["agent"] == a), default=None) for a in AGENTS}
    running = herdr_liveness()
    tasks = sorted({e["task"] for e in events} | {p.get("task") for p in proposals} | {v.get("task") for v in verdicts})
    return {
        "herdr_running": running,
        "last_activity": last,
        "tasks": {
            t: {
                "passes": sum(e["task"] == t for e in events),
                "proposals": dict(Counter(p.get("kind") for p in proposals if p.get("task") == t)),
                "verdicts": dict(Counter(v.get("verdict") for v in verdicts if v.get("task") == t)),
            }
            for t in tasks
        },
        "proposals": len(proposals),
        "c2_verified": len(proposals) - len(backlog),
        "c2_backlog": backlog,
        "playwright_queue": sum(1 for _ in open(validation / "playwright_queue.txt", encoding="utf8"))
        if (validation / "playwright_queue.txt").exists() else 0,
        "c1_input": {"contrarian_review": mtime(review_input), "regimen_verification": mtime(current)},
        "c1_input_stale": bool(review_input.exists() and current.exists()
                               and review_input.stat().st_mtime < current.stat().st_mtime),
        "violations": contract_violations(proposals, verdicts),
    }


def print_report(r):
    if r["herdr_running"] is None:
        print("herdr: unknown (could not query)")
    else:
        for a in AGENTS:
            print(f"herdr: {a} {'running' if a in r['herdr_running'] else 'NOT running'}")
    for a, t in r["last_activity"].items():
        print(f"last activity: {a} {t}")
    print()
    for t, row in r["tasks"].items():
        print(f"{t}: passes={row['passes']} proposals={row['proposals']} verdicts={row['verdicts']}")
    print(f"\nC2 verified: {r['c2_verified']}/{r['proposals']} (backlog {len(r['c2_backlog'])})")
    print(f"playwright queue: {r['playwright_queue']}")
    if r["c1_input_stale"]:
        print(f"C1 input STALE: {r['c1_input']}")
    print(f"\ncontract violations: {len(r['violations'])}")
    for line in r["violations"]:
        print(f"  {line}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--disease", default="aml_gen")
    args = ap.parse_args()
    report = build_report(args.disease)
    print(json.dumps(report, indent=2) if args.json else "", end="" if not args.json else "\n")
    if not args.json:
        print_report(report)


if __name__ == "__main__":
    main()
