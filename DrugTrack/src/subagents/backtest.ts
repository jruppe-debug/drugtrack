import { HumanMessage } from "@langchain/core/messages";
import { makeQwenWorker, withTimeout, type SubagentResult } from "./common.js";
import { config } from "../config.js";

/**
 * The "Backtest Qwen subagent" from the diagram. This is the QA layer: it
 * looks at what the four data subagents actually returned and flags
 * anything that looks wrong (empty results treated as success, malformed
 * data, a source silently failing). It does not fetch new data itself.
 */
export async function backtestSubagent(
  results: Array<{ source: string; ok: boolean; data?: unknown; error?: string }>,
): Promise<SubagentResult> {
  try {
    const worker = makeQwenWorker("backtest");
    const summary = results
      .map((r) => `${r.source}: ok=${r.ok}${r.error ? ` error=${r.error}` : ` data=${JSON.stringify(r.data).slice(0, 300)}`}`)
      .join("\n");
    const res = await withTimeout(
      worker.invoke([
        new HumanMessage(
          `Review these subagent results for a drug-search pipeline run. Flag anything ` +
          `suspicious: a source returning "ok" but with empty/placeholder data, an error ` +
          `that looks retryable vs. one that looks permanent, or inconsistent data between ` +
          `sources. Be specific.\n\n${summary}`,
        ),
      ]),
      config.qwenCallTimeoutMs,
      "backtest Qwen call",
    );
    return { source: "backtest", ok: true, data: res.content };
  } catch (err) {
    return { source: "backtest", ok: false, error: err instanceof Error ? err.message : String(err) };
  }
}
