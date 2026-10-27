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

/**
 * The contrarian reviewer's verdict shape. It never restates the
 * deterministic pipeline's own conclusion - it must either find a concrete
 * flaw ("disproven"), fail to find one ("survives"), or admit it can't tell
 * ("needs_review"). exclusionJustified only applies to records the
 * deterministic pipeline already excluded; it's null for everything else.
 */
export const ContrarianVerdict = z.object({
  verdict: z.enum(["survives", "disproven", "needs_review"]).describe("survives: the adversarial challenge found no real flaw in the deterministic link. disproven: a genuine problem was found. needs_review: inconclusive, a human must look."),
  reason: z.string().describe("The specific adversarial argument made, or, for an excluded record, why the exclusion is or isn't justified."),
  exclusionJustified: z.boolean().nullable().describe("For excluded_* records only: true if the exclusion matches the disease config's documented reason, false if it looks like a real gap mislabeled as intentional. Null if this record was not excluded."),
});
export type ContrarianVerdict = z.infer<typeof ContrarianVerdict>;

export const CONTRARIAN_SCHEMA_INSTRUCTIONS = `Respond with ONLY a single JSON object, no other text, matching exactly this shape:
{"verdict": "survives"|"disproven"|"needs_review", "reason": string, "exclusionJustified": boolean|null}`;
