import { runFetchSubagent, makeReasoningWorker, type SubagentResult } from "./common.js";

interface PubmedRaw {
  ids: string[];
  titles: string[];
}

/**
 * Real PubMed search (NCBI E-utilities, no API key) - the "research
 * articles" source for drug-lead discovery. Fetches real recent article
 * titles first, then asks the reasoning worker (Haiku if ANTHROPIC_API_KEY
 * is set, else local qwen2.5:7b - see makeReasoningWorker) to extract a
 * named drug/agent candidate from that real text - it never invents one.
 * The stronger model matters more here than for the four bulk mechanical
 * fetch subagents (this is low-volume, judgment-heavy: is this actually a
 * new candidate worth flagging, not just a drug class or the disease name),
 * but either backend keeps this runnable with zero API key. Same
 * fetch-then-extract shape and NormalizedFinding schema as ctgov/fda/chembl;
 * identifierType becomes "PMID" here since a lead has no registry
 * identifier yet.
 *
 * The result is PROVISIONAL. A "pubmed-lead" finding must clear the
 * deterministic pipeline (tools/verify_regimens.py) and the contrarian
 * adversarial pass (src/review/contrarian.ts) before it is trusted - never
 * feed it into matchAndVerify directly.
 *
 * AACT-tag leads and company-pipeline-page leads are the obvious next
 * sources (this repo already has working real fetchers for both, in the
 * AACT_bridge and tools/sponsor_links.py) but are not wired here yet.
 * "KOL commentary" has no free, no-key, systematically-fetchable source
 * found so far - name specific sources (a conference abstracts site, a
 * journal RSS feed) to wire that one up rather than scraping arbitrary
 * pages.
 */
export async function pubmedLeadSubagent(scopeQuery: string, retmax = 8): Promise<SubagentResult> {
  return runFetchSubagent(
    "pubmed-lead",
    async (): Promise<PubmedRaw> => {
      const esearchUrl = `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&retmode=json&retmax=${retmax}&sort=date&term=${encodeURIComponent(scopeQuery)}`;
      const esearchRes = await fetch(esearchUrl);
      if (!esearchRes.ok) throw new Error(`PubMed esearch returned ${esearchRes.status}`);
      const esearchJson = (await esearchRes.json()) as { esearchresult?: { idlist?: string[] } };
      const ids = esearchJson.esearchresult?.idlist ?? [];
      if (ids.length === 0) return { ids: [], titles: [] };

      const esummaryUrl = `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&retmode=json&id=${ids.join(",")}`;
      const esummaryRes = await fetch(esummaryUrl);
      if (!esummaryRes.ok) throw new Error(`PubMed esummary returned ${esummaryRes.status}`);
      const esummaryJson = (await esummaryRes.json()) as { result?: Record<string, { title?: string }> };
      const titles = ids.map((id) => `PMID ${id}: ${esummaryJson.result?.[id]?.title ?? ""}`);
      return { ids, titles };
    },
    (raw) => {
      const r = raw as PubmedRaw;
      if (r.ids.length === 0) {
        return `No recent PubMed articles matched "${scopeQuery}". Set identifier, identifierType, and matchedName to null, confidence "low".`;
      }
      return (
        `These are real recent PubMed titles for the search "${scopeQuery}":\n${r.titles.join("\n")}\n\n` +
        `If any title names a SPECIFIC drug, agent, or code name (not a drug class, not the disease itself) ` +
        `that looks like a new treatment candidate worth checking, set matchedName to it, identifierType to ` +
        `"PMID", identifier to the matching PMID, and rate your confidence. If none do, set all three to ` +
        `null and confidence "low" - do not invent a candidate.`
      );
    },
    makeReasoningWorker,
  );
}
