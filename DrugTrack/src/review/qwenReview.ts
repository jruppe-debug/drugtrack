import { HumanMessage } from "@langchain/core/messages";
import { makeQwenWorker, withTimeout, type SubagentResult } from "../subagents/common.js";
import type { ValidationSummary } from "../graph/validate.js";
import { config } from "../config.js";

/**
 * The final review/decision node - now backed by local Qwen instead of
 * DeepSeek or Claude. No API key, no cost, nothing leaves this machine.
 * Uses a slightly larger context budget than the other Qwen workers since
 * this call synthesizes everything the other nodes produced, including the
 * real cross-source validation data (not just raw ok/error status).
 */
export async function qwenReviewNode(
  query: string,
  results: SubagentResult[],
  backtestFindings: string,
  debugDiagnosis: string,
  validation: ValidationSummary | undefined,
): Promise<string> {
  const worker = makeQwenWorker("final-review");

  const validationBlock = validation
    ? `Validation layer (real identifiers, not prose):\n` +
      validation.rows
        .map((r) => `- ${r.source}: identifier=${r.identifier ?? "none"} (${r.identifierType ?? "n/a"}) name="${r.matchedName ?? "none"}" confidence=${r.confidence ?? "n/a"}`)
        .join("\n") +
      `\n${validation.sourcesWithIdentifier}/${validation.sourcesTotal} sources produced a real identifier. ` +
      `Name agreement across sources: ${validation.nameAgreement}. Notes: ${validation.notes.join("; ") || "none"}`
    : "Validation layer did not run.";

  const res = await withTimeout(
    worker.invoke([
      new HumanMessage(
        `Query: ${query}\n\n` +
        `Subagent results:\n${results.map((r) => `- ${r.source}: ok=${r.ok} ${r.error ?? ""}`).join("\n")}\n\n` +
        `${validationBlock}\n\n` +
        `Backtest/QA findings:\n${backtestFindings}\n\n` +
        `Auto-debug diagnosis of any failures:\n${debugDiagnosis}\n\n` +
        `Give a final assessment grounded in the validation layer specifically: do the sources ` +
        `actually corroborate each other via real identifiers, or is agreement just assumed? ` +
        `What's missing or unreliable, and what to do next.`,
      ),
    ]),
    config.qwenCallTimeoutMs,
    "Qwen final review call",
  );
  return String(res.content);
}
