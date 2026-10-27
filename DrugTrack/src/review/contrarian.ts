import { readFileSync } from "node:fs";
import { join } from "node:path";
import { HumanMessage } from "@langchain/core/messages";
import { makeReasoningWorker, withTimeout } from "../subagents/common.js";
import { ContrarianVerdict, CONTRARIAN_SCHEMA_INSTRUCTIONS } from "../graph/schema.js";
import { config, ROOT } from "../config.js";

interface RegimenRecord {
  record_id: string;
  status: string;
  excluded_reason: string | null;
  basis_ncts: string[];
  [key: string]: unknown;
}

interface RegimenVerification {
  disease: string;
  universe_generated: string;
  results: RegimenRecord[];
}

type ExcludedRoles = Record<string, string>;

function drugNameOf(recordId: string): string {
  return recordId.split(":")[0] ?? recordId;
}

/**
 * Reads the Python pipeline's own deterministic output
 * (tools/verify_regimens.py) for one disease, from the generalized
 * per-disease layout (data/diseases/<disease>/regimen_verification.json) -
 * NOT the stale top-level data/regimen_verification.json (17 records,
 * generated 2026-10-14, superseded once the pipeline was generalized across
 * diseases on 2026-10-26). "aml_gen" is the current AML universe (42
 * records); "cll" is also available.
 */
function loadUniverse(disease: string): RegimenVerification {
  return JSON.parse(
    readFileSync(join(ROOT, "data", "diseases", disease, "regimen_verification.json"), "utf8"),
  ) as RegimenVerification;
}

/** The disease config's own documented reasons for each excluded_* role - the source of truth for "was this exclusion authorized". */
function loadExcludedRoles(disease: string): ExcludedRoles {
  const cfg = JSON.parse(readFileSync(join(ROOT, "config", "diseases", `${disease}.json`), "utf8")) as {
    excluded_roles?: ExcludedRoles;
  };
  return cfg.excluded_roles ?? {};
}

export interface ContrarianRecordResult {
  recordId: string;
  drug: string;
  deterministicStatus: string;
  verdict: ContrarianVerdict["verdict"];
  reason: string;
  exclusionJustified: boolean | null;
}

export interface ContrarianReport {
  disease: string;
  universeGenerated: string;
  reviewedAt: string;
  total: number;
  survives: number;
  disproven: number;
  needsReview: number;
  deterministicCorrectPct: number;
  records: ContrarianRecordResult[];
}

/**
 * One adversarial pass per record: given the verdict this repo's own
 * rule-based pipeline already reached (tools/verify_regimens.py,
 * tools/central_anchor.py), argue against it instead of restating it. For
 * an excluded_* record the question flips: does the exclusion actually
 * match the disease config's documented reason, or does it look like a
 * real gap dressed up as an intentional exclusion?
 */
async function challengeRecord(
  record: RegimenRecord,
  excludedRoles: ExcludedRoles,
): Promise<ContrarianRecordResult> {
  const drug = drugNameOf(record.record_id);
  const fallback = (reason: string): ContrarianRecordResult => ({
    recordId: record.record_id,
    drug,
    deterministicStatus: record.status,
    verdict: "needs_review",
    reason,
    exclusionJustified: null,
  });

  const isExcluded = record.status.startsWith("excluded_");
  const isVerified = record.status.startsWith("verified") || record.status === "pre_registry_backbone";
  const role = isExcluded ? record.status.slice("excluded_".length) : null;
  const configuredReason = role ? excludedRoles[role] : undefined;

  // Record detail includes gates/waterfall/design/conflicts (the
  // deterministic pipeline's own evidence trail) - enough context to argue
  // against without a separate FDA-anchor file.
  const detail = `Record detail (gates/waterfall/design/conflicts are the pipeline's own evidence): ${JSON.stringify(record).slice(0, 1800)}`;

  const prompt = isExcluded
    ? `Record ${record.record_id} (drug ${drug}) was EXCLUDED from the deterministic pipeline with ` +
      `reason "${record.excluded_reason ?? role}". The disease config's documented reason for the ` +
      `"${role}" exclusion role is: "${configuredReason ?? "NOT FOUND IN CONFIG - this is a red flag"}". ` +
      `Decide: does this specific exclusion actually match that documented reason, or does it look like a ` +
      `real gap (a legitimate indication being dropped) mislabeled as an intentional exclusion? ${detail}`
    : isVerified
      ? `Record ${record.record_id} (drug ${drug}) was marked "${record.status}" by a deterministic rule-based ` +
        `pipeline, basis NCT(s) ${JSON.stringify(record.basis_ncts)}. Try to DISPROVE this link: is there a ` +
        `plausible reason the chosen NCT is the wrong basis trial, the regimen key (see "gates"/"design") is too ` +
        `loose or too tight, or a signature-only match (no printed Section 14 NCT) is being over-trusted? ${detail}`
      : `Record ${record.record_id} (drug ${drug}) got status "${record.status}" from the deterministic pipeline - ` +
        `neither a confirmed verification nor a config-documented exclusion. Evaluate whether that status itself ` +
        `is actually the right call given the evidence, or whether it's masking either a real link or a real gap. ${detail}`;

  // Everything below is caught, so a network/timeout failure on either
  // backend (Haiku or the local-Qwen fallback in makeReasoningWorker)
  // becomes a needs_review record instead of crashing the whole universe run.
  let content: string;
  try {
    const worker = makeReasoningWorker();
    const res = await withTimeout(
      worker.invoke([new HumanMessage(`${prompt}\n\n${CONTRARIAN_SCHEMA_INSTRUCTIONS}`)]),
      config.qwenCallTimeoutMs,
      `contrarian challenge ${record.record_id}`,
    );
    content = String(res.content);
  } catch (err) {
    return fallback(`Contrarian call failed: ${err instanceof Error ? err.message : String(err)}`);
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch {
    return fallback(`Contrarian model did not return valid JSON: ${content.slice(0, 200)}`);
  }
  const validated = ContrarianVerdict.safeParse(parsed);
  if (!validated.success) {
    return fallback(`JSON did not match expected shape: ${validated.error.message}`);
  }
  return {
    recordId: record.record_id,
    drug,
    deterministicStatus: record.status,
    verdict: validated.data.verdict,
    reason: validated.data.reason,
    exclusionJustified: validated.data.exclusionJustified,
  };
}

/**
 * At most `limit` challenges in flight - same reasoning as runLimited in
 * drugTrackGraph.ts: the local Ollama server chokes on concurrent 7b calls.
 */
async function runLimited<T>(tasks: Array<() => Promise<T>>, limit: number): Promise<T[]> {
  const results: T[] = new Array(tasks.length);
  let next = 0;
  async function worker() {
    while (next < tasks.length) {
      const i = next++;
      const task = tasks[i];
      if (task) results[i] = await task();
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, tasks.length) }, worker));
  return results;
}

export async function runContrarianReview(disease = "aml_gen", concurrency = 2): Promise<ContrarianReport> {
  const universe = loadUniverse(disease);
  const excludedRoles = loadExcludedRoles(universe.disease);

  const tasks = universe.results.map((record) => () => challengeRecord(record, excludedRoles));

  const records = await runLimited(tasks, concurrency);

  const survives = records.filter((r) => r.verdict === "survives").length;
  const disproven = records.filter((r) => r.verdict === "disproven").length;
  const needsReview = records.filter((r) => r.verdict === "needs_review").length;

  return {
    disease: universe.disease,
    universeGenerated: universe.universe_generated,
    reviewedAt: new Date().toISOString(),
    total: records.length,
    survives,
    disproven,
    needsReview,
    deterministicCorrectPct: records.length ? Math.round((survives / records.length) * 1000) / 10 : 0,
    records,
  };
}
