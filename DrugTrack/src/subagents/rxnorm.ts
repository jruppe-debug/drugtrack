import { runFetchSubagent, type SubagentResult } from "./common.js";

/**
 * RxNorm/RxNav (NIH/NLM) - free, no API key, official government drug
 * identity data. This is the "verification layer" node: it doesn't replace
 * ChEMBL/FDA/CT.gov, it cross-checks them against an authoritative,
 * citable source. Note: NIH discontinued the drug-drug interaction API in
 * Jan 2024 - this only gives identity/normalization (RxCUI) + label links
 * via DailyMed, not interaction checking.
 * Verify current shape at https://lhncbc.nlm.nih.gov/RxNav/APIs/ before relying on it.
 */
export async function rxnormSubagent(query: string): Promise<SubagentResult> {
  return runFetchSubagent(
    "rxnorm",
    async () => {
      const url = `https://rxnav.nlm.nih.gov/REST/rxcui.json?name=${encodeURIComponent(query)}`;
      const res = await fetch(url);
      if (!res.ok) throw new Error(`RxNav returned ${res.status}`);
      return res.json();
    },
    (raw) =>
      `This is an NIH RxNorm identity lookup for "${query}". Set identifier to the RxCUI ` +
      `if present, identifierType to "RxCUI", matchedName to "${query}" if an RxCUI was found ` +
      `(RxNorm confirms the name, it doesn't return an alternate one), and rate your confidence ` +
      `based on whether exactly one RxCUI was returned (high) versus zero or multiple (low).\n\n${JSON.stringify(raw).slice(0, 2000)}`,
  );
}
