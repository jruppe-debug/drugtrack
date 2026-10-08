import { HumanMessage } from "@langchain/core/messages";
import { makeQwenWorker, withTimeout, type SubagentResult } from "../subagents/common.js";
import { config } from "../config.js";

export interface DebugOutcome {
  retryCounts: Record<string, number>;
  toRetry: string[];
  diagnosis: string;
}

/**
 * The generic "auto debugging" step: given whatever subagent results just
 * came back, find the failed ones, ask Qwen to diagnose each error, and
 * decide (by retry count) which ones the graph should loop back and retry
 * versus which have failed too many times and should be escalated to
 * Claude's review step instead of retried forever.
 */
export async function diagnoseFailures(
  results: SubagentResult[],
  retryCounts: Record<string, number>,
): Promise<DebugOutcome> {
  const failed = results.filter((r) => !r.ok);
  if (failed.length === 0) {
    return { retryCounts, toRetry: [], diagnosis: "No failures." };
  }

  const worker = makeQwenWorker("autodebug");
  const res = await withTimeout(
    worker.invoke([
      new HumanMessage(
        `These subagents failed in a drug-search pipeline run. For each, say whether it ` +
        `looks retryable (network blip, rate limit, timeout) or not (bad query, auth failure, ` +
        `malformed request):\n\n` +
        failed.map((f) => `${f.source}: ${f.error}`).join("\n"),
      ),
    ]),
    config.qwenCallTimeoutMs,
    "autodebug diagnose call",
  );

  const updatedCounts = { ...retryCounts };
  const toRetry: string[] = [];
  for (const f of failed) {
    const current = updatedCounts[f.source] ?? 0;
    if (current < config.maxAutoDebugRetries) {
      updatedCounts[f.source] = current + 1;
      toRetry.push(f.source);
    }
  }

  return { retryCounts: updatedCounts, toRetry, diagnosis: String(res.content) };
}
