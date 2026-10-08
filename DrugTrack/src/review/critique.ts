import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { HumanMessage } from "@langchain/core/messages";
import { makeQwenWorker, withTimeout, type SubagentResult } from "../subagents/common.js";
import type { ValidationSummary } from "../graph/validate.js";
import { ROOT, config } from "../config.js";

export interface CritiqueInput {
  query: string;
  results: SubagentResult[];
  backtestFindings: string;
  debugDiagnosis: string;
  validation: ValidationSummary | undefined;
  finalDecision: string;
  reviewError: string | undefined;
  manifestError: string | undefined;
}

/**
 * A dedicated critique step, distinct from backtest (which checks whether
 * subagent OUTPUT looks technically sound) and autoDebug (which just
 * retries failures). This one looks at the entire run holistically, after
 * everything else has finished, and asks: was the process itself sound?
 */
export async function critiqueRun(input: CritiqueInput): Promise<string> {
  const worker = makeQwenWorker("critique");

  const stepSummary = [
    `1. fetchAll: ${input.results.filter((r) => r.ok).length}/${input.results.length} subagents succeeded`,
    `2. backtest: ${input.backtestFindings.startsWith("backtest itself failed") ? "FAILED - " + input.backtestFindings : "ran"}`,
    `3. autoDebug: ${input.debugDiagnosis}`,
    `4. matchVerify/validation: ${input.validation ? `${input.validation.sourcesWithIdentifier}/${input.validation.sourcesTotal} real identifiers, agreement=${input.validation.nameAgreement}` : "did not run"}`,
    `5. uiVis: ${input.manifestError ? "FAILED - " + input.manifestError : "ran"}`,
    `6. qwenReview: ${input.reviewError ? "FAILED - " + input.reviewError : "ran"}`,
  ].join("\n");

  const res = await withTimeout(
    worker.invoke([
      new HumanMessage(
        `You are critiquing a multi-agent drug-search pipeline run, step by step, not just its final answer.\n\n` +
        `Query: ${input.query}\n\nStep-by-step outcome:\n${stepSummary}\n\n` +
        `Final decision produced:\n${input.finalDecision || "(none - review step failed)"}\n\n` +
        `For each step, note specifically: did it do its actual job, or did it silently degrade? ` +
        `Is the final decision actually supported by the validation data, or does it overstate confidence? ` +
        `What single change would most improve the next run? Be specific and critical, not diplomatic.`,
      ),
    ]),
    config.qwenCallTimeoutMs,
    "critique call",
  );
  const critique = String(res.content);

  const critiquePath = join(ROOT, "data", "critique.json");
  writeFileSync(
    critiquePath,
    JSON.stringify({ query: input.query, generatedAt: new Date().toISOString(), stepSummary, critique }, null, 2),
    "utf8",
  );

  return critique;
}
