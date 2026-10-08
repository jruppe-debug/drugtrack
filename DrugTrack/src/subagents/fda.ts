import { runFetchSubagent, type SubagentResult } from "./common.js";

/**
 * openFDA drug endpoints - public, no key required for low volume.
 * TODO: verify current endpoint shape at https://open.fda.gov/apis/drug/
 * before relying on this in production; not freshly checked for 2026.
 */
export async function fdaSubagent(query: string): Promise<SubagentResult> {
  return runFetchSubagent(
    "fda",
    async () => {
      const url = `https://api.fda.gov/drug/label.json?search=${encodeURIComponent(query)}&limit=5`;
      const res = await fetch(url);
      if (!res.ok) throw new Error(`openFDA returned ${res.status}`);
      return res.json();
    },
    (raw) =>
      `This is an openFDA drug label response for the query "${query}". Find the first result. ` +
      `Set identifier to its application number if present (e.g. "NDA123456" or "ANDA123456"), ` +
      `identifierType to "FDA_APPL_NO", matchedName to the brand or generic name this label uses, ` +
      `and rate your confidence that this label is actually about "${query}".\n\n${JSON.stringify(raw).slice(0, 4000)}`,
  );
}
