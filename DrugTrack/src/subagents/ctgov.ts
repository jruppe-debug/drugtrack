import { runFetchSubagent, type SubagentResult } from "./common.js";

/**
 * ClinicalTrials.gov API v2 - public, no key required.
 * Base URL/shape verified against general knowledge of the v2 API, not a
 * fresh 2026 check - if this 404s, confirm the current endpoint at
 * https://clinicaltrials.gov/data-api/api before assuming the code is wrong.
 */
export async function ctgovSubagent(query: string): Promise<SubagentResult> {
  return runFetchSubagent(
    "ctgov",
    async () => {
      const url = `https://clinicaltrials.gov/api/v2/studies?query.term=${encodeURIComponent(query)}&pageSize=10`;
      const res = await fetch(url);
      if (!res.ok) throw new Error(`ClinicalTrials.gov returned ${res.status}`);
      return res.json();
    },
    (raw) =>
      `This is a ClinicalTrials.gov API response for the query "${query}". Find the first ` +
      `trial record. Set identifier to its NCT number (e.g. "NCT01234567"), identifierType ` +
      `to "NCT", matchedName to the drug/intervention name as this trial names it, and rate ` +
      `your confidence that this trial is actually about "${query}".\n\n${JSON.stringify(raw).slice(0, 4000)}`,
  );
}
