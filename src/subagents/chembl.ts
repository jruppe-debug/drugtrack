import { runFetchSubagent, type SubagentResult } from "./common.js";

/**
 * ChEMBL web services - public, no key required.
 * TODO: verify current endpoint shape at https://www.ebi.ac.uk/chembl/api/data/docs
 * before relying on this in production; not freshly checked for 2026.
 */
export async function chemblSubagent(query: string): Promise<SubagentResult> {
  return runFetchSubagent(
    "chembl",
    async () => {
      const url = `https://www.ebi.ac.uk/chembl/api/data/molecule/search?q=${encodeURIComponent(query)}&format=json`;
      const res = await fetch(url);
      if (!res.ok) throw new Error(`ChEMBL returned ${res.status}`);
      return res.json();
    },
    (raw) =>
      `This is a ChEMBL molecule search response for the query "${query}". Find the first result. ` +
      `Set identifier to its ChEMBL ID (e.g. "CHEMBL941"), identifierType to "ChEMBL_ID", ` +
      `matchedName to the preferred/pref_name this record uses, and rate your confidence that ` +
      `this molecule is actually "${query}".\n\n${JSON.stringify(raw).slice(0, 4000)}`,
  );
}
