import type { SubagentResult } from "../subagents/common.js";

export interface ValidationRow {
  source: string;
  identifier: string | null;
  identifierType: string | null;
  matchedName: string | null;
  confidence: string | null;
}

export interface ValidationSummary {
  query: string;
  rows: ValidationRow[];
  sourcesWithIdentifier: number;
  sourcesTotal: number;
  distinctNames: string[];
  nameAgreement: "unanimous" | "partial" | "none" | "insufficient-data";
  notes: string[];
}

/**
 * The actual validation layer: takes every subagent's structured
 * NormalizedFinding and cross-checks them against each other. This is what
 * "traceable/verifiable" means concretely - not "an AI said so," but "N
 * independent sources, each with a real citable identifier, agree or
 * disagree on this name," visible as data, not prose.
 */
export function validateAcrossSources(query: string, results: SubagentResult[]): ValidationSummary {
  const rows: ValidationRow[] = results.map((r) => ({
    source: r.source,
    identifier: r.finding?.identifier ?? null,
    identifierType: r.finding?.identifierType ?? null,
    matchedName: r.finding?.matchedName ?? null,
    confidence: r.finding?.confidence ?? null,
  }));

  const withIdentifier = rows.filter((r) => r.identifier !== null);
  const names = rows
    .map((r) => r.matchedName?.trim().toLowerCase())
    .filter((n): n is string => !!n);
  const distinctNames = [...new Set(names)];

  const notes: string[] = [];
  let nameAgreement: ValidationSummary["nameAgreement"];
  if (names.length === 0) {
    nameAgreement = "insufficient-data";
    notes.push("No source returned a matched name to compare.");
  } else if (distinctNames.length === 1) {
    nameAgreement = "unanimous";
  } else if (distinctNames.length < names.length) {
    nameAgreement = "partial";
    notes.push(`Sources disagree on naming: ${distinctNames.join(" vs. ")}`);
  } else {
    nameAgreement = distinctNames.length > 1 ? "partial" : "none";
  }

  for (const r of rows) {
    if (r.confidence === "low") notes.push(`${r.source} reported low confidence in its own match.`);
    if (r.identifier === null) notes.push(`${r.source} produced no citable identifier.`);
  }

  return {
    query,
    rows,
    sourcesWithIdentifier: withIdentifier.length,
    sourcesTotal: rows.length,
    distinctNames,
    nameAgreement,
    notes,
  };
}
