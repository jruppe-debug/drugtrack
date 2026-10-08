"""PubMed article support for one drug node of the disease tree (data/tree.json), scoped to that tree's disease.

Two branches, both exact-ID joins, no name matching and no LLM:
  trial_linked: for every NCT under the drug node, PubMed records whose secondary source ID is that NCT
                ("NCTxxxxxxxx[si]", i.e. the DataBankList ClinicalTrials.gov accession). Each linked PMID is
                also checked for the scope disease MeSH tag.
  mesh_tagged:  "<drug heading>"[Majr] AND "<disease heading>"[Mesh], counted by publication type. MeSH
                explosion pulls in child headings, so this is background support, not trial evidence.

Usage (from DrugTrack/): ../.venv/Scripts/python tools/scope_drug_lit.py CHEMBL941
Writes data/scope_lit_<chembl>.json, served by src/serveViz.ts at /api/scope-lit.
"""
import json
import os
import re
import sys

from drug_lit import DRUGTRACK, article_support, count, search, summaries

# Scope disease (tree.json meta.scope.id) -> MeSH heading. Only AML for now.
SCOPE_MESH = {"MONDO_0018874": "Leukemia, Myeloid, Acute"}
# ChEMBL parent -> MeSH heading for the drug.
DRUG_MESH = {"CHEMBL941": "Imatinib Mesylate"}
NCT_RE = re.compile(r"NCT\d{8}")


def find_drug(node, chembl):
    if node.get("kind") == "drug" and (node.get("attrs") or {}).get("chembl_root") == chembl:
        return node
    for c in node.get("children") or []:
        hit = find_drug(c, chembl)
        if hit:
            return hit
    return None


def tree_ncts(node, out=None, path=()):
    """NCT -> list of node paths (regimen > program) under the drug node."""
    out = {} if out is None else out
    here = path + (node.get("name", ""),)
    found = set(NCT_RE.findall(json.dumps((node.get("attrs") or {}).get("trials") or [])))
    if node.get("kind") == "trial":
        found |= set(NCT_RE.findall(node.get("id", "") + " " + node.get("name", "")))
    for n in found:
        out.setdefault(n, []).append(" > ".join(here[1:]))
    for c in node.get("children") or []:
        tree_ncts(c, out, here)
    return out


def main(chembl="CHEMBL941"):
    with open(os.path.join(DRUGTRACK, "data", "tree.json"), encoding="utf8") as f:
        tree = json.load(f)
    scope = tree["meta"]["scope"]
    disease_heading = SCOPE_MESH.get(scope["id"])
    drug_heading = DRUG_MESH.get(chembl)
    if not disease_heading or not drug_heading:
        sys.exit(f"No MeSH heading configured for scope {scope['id']} or drug {chembl}")
    drug = find_drug(tree, chembl)
    if not drug:
        sys.exit(f"{chembl} not found in tree.json")

    ncts = tree_ncts(drug)
    trials, all_pmids = [], set()
    for n in sorted(ncts):
        pmids = search(f"{n}[si]", 200)
        trials.append({"nct": n, "paths": sorted(set(ncts[n])), "pmids": pmids})
        all_pmids |= set(pmids)
        print(f"{n}: {len(pmids)} PMIDs", flush=True)

    tagged = set()
    pmids = sorted(all_pmids)
    for i in range(0, len(pmids), 100):
        ids = " OR ".join(f"{p}[uid]" for p in pmids[i:i + 100])
        tagged |= set(search(f'({ids}) AND "{disease_heading}"[Mesh]', 100))
    meta = summaries(pmids) if pmids else {}
    for t in trials:
        t["articles"] = [{**meta[p], "scope_mesh_tagged": p in tagged} for p in t["pmids"] if p in meta]
        del t["pmids"]

    mesh = article_support(drug_heading, {"scope": disease_heading})
    out = {
        "available": True,
        "scope": scope, "drug": {"chembl": chembl, "name": drug["name"], "node_id": drug["id"], "mesh": drug_heading},
        "disease_mesh": disease_heading,
        "summary": {
            "trials": len(trials),
            "trials_with_articles": sum(1 for t in trials if t["articles"]),
            "trial_linked_pmids": len(pmids),
            "trial_linked_scope_tagged": len(tagged),
            "mesh_tagged_total": mesh["total"],
            "drug_si_any_scope": count(f'"{drug_heading}"[Mesh] AND "{disease_heading}"[Mesh] AND clinicaltrials.gov[si]'),
        },
        "trial_linked": trials,
        "mesh_tagged": mesh,
    }
    path = os.path.join(DRUGTRACK, "data", f"scope_lit_{chembl}.json")
    with open(path, "w", encoding="utf8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    print(json.dumps(out["summary"]))
    print("wrote", path)


if __name__ == "__main__":
    main(*sys.argv[1:])
