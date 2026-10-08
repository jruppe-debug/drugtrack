import { z } from "zod";

/**
 * The structured shape every data-source subagent must extract, instead of
 * free-text prose. This is what actually makes cross-source validation
 * possible: two sources can only be "compared" if they both produced a real
 * identifier and a name in the same shape.
 */
export const NormalizedFinding = z.object({
  identifier: z.string().nullable().describe("The source's own official identifier for this drug, e.g. an RxCUI, ChEMBL ID, NCT number, or FDA application number. Null if none was found."),
  identifierType: z.string().nullable().describe('What kind of identifier, e.g. "RxCUI", "ChEMBL_ID", "NCT", "FDA_APPL_NO". Null if identifier is null.'),
  matchedName: z.string().nullable().describe("The drug/substance name as this source itself refers to it. Null if the query did not resolve to anything."),
  confidence: z.enum(["high", "medium", "low"]).describe("How confidently this source's data actually corresponds to the query."),
});

export type NormalizedFinding = z.infer<typeof NormalizedFinding>;

export const FINDING_SCHEMA_INSTRUCTIONS = `Respond with ONLY a single JSON object, no other text, matching exactly this shape:
{"identifier": string|null, "identifierType": string|null, "matchedName": string|null, "confidence": "high"|"medium"|"low"}`;
