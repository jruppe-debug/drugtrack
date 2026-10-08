import { randomUUID } from "node:crypto";
import type { SubagentResult } from "../subagents/common.js";
import { upsertEntity, upsertEdge, type Entity } from "./sharedGraph.js";
import { validateAcrossSources, type ValidationSummary } from "./validate.js";

/**
 * Now stores each source's real structured identifier (RxCUI, ChEMBL_ID,
 * NCT, FDA_APPL_NO) when the subagent successfully extracted one, closing
 * the gap this file used to flag as an honest limitation - entities are no
 * longer just opaque prose blobs, they carry a citable identifier and a
 * confidence rating alongside the actual cross-source validation summary.
 */
export function matchAndVerify(
  query: string,
  results: SubagentResult[],
): { entityIds: string[]; validation: ValidationSummary } {
  const entityIds: string[] = [];
  const queryEntityId = `query:${query}`;

  upsertEntity({ id: queryEntityId, type: "drug", name: query, source: "user-query", raw: null });
  entityIds.push(queryEntityId);

  for (const r of results) {
    if (!r.ok) continue;
    // Prefer a real, stable identifier as the entity id when one exists -
    // that's what lets the SAME drug from two different runs collapse into
    // the same node instead of always minting a fresh random one.
    const id = r.finding?.identifier
      ? `${r.source}:${r.finding.identifierType ?? "id"}:${r.finding.identifier}`
      : `${r.source}:${randomUUID()}`;

    const entity: Entity = {
      id,
      type: "trial",
      name: r.finding?.matchedName ?? `${r.source} result for ${query}`,
      source: r.source,
      identifier: r.finding?.identifier ?? null,
      identifierType: r.finding?.identifierType ?? null,
      confidence: r.finding?.confidence ?? null,
      raw: r.data,
    };
    upsertEntity(entity);
    upsertEdge({ from: queryEntityId, to: id, relation: "has_result_from", source: r.source });
    entityIds.push(id);
  }

  const validation = validateAcrossSources(query, results);
  return { entityIds, validation };
}
