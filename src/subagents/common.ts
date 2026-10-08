import { ChatOllama } from "@langchain/ollama";
import { HumanMessage } from "@langchain/core/messages";
import { config } from "../config.js";
import { NormalizedFinding, FINDING_SCHEMA_INSTRUCTIONS, type NormalizedFinding as Finding } from "../graph/schema.js";

export interface SubagentResult {
  source: string;
  ok: boolean;
  data?: unknown;
  finding?: Finding;
  error?: string;
}

/**
 * One Qwen instance per subagent role, matching the diagram's "N separate
 * Qwen subagents" - they're all the same underlying model served by Ollama,
 * just called with different prompts/roles. This is cheaper and simpler
 * than running N separate model processes.
 */
export function makeQwenWorker(role: string) {
  return new ChatOllama({
    model: config.largeModel,
    baseUrl: config.ollamaBaseUrl,
    temperature: 0,
  });
}

/**
 * A worker forced into Ollama's native JSON mode - this is what actually
 * makes structured extraction reliable instead of hoping the model happens
 * to produce parseable JSON. `format: "json"` is a real Ollama API option,
 * not a LangChain abstraction. Deliberately on the SMALL model: extracting
 * one identifier out of an API response is mechanical, not reasoning-heavy,
 * and doesn't need the larger model's capacity - it needs to be fast.
 */
function makeStructuredWorker() {
  return new ChatOllama({
    model: config.smallModel,
    baseUrl: config.ollamaBaseUrl,
    temperature: 0,
    format: "json",
  });
}

/**
 * Races a promise against a timeout so a hung network call or an
 * unresponsive local Ollama server can never block the whole graph
 * indefinitely - this was a real bug (no timeout anywhere) that made a
 * DrugTrack run hang silently for 10+ minutes with zero output.
 */
export function withTimeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  return Promise.race([
    promise,
    new Promise<T>((_, reject) =>
      setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms),
    ),
  ]);
}

/**
 * Shared shape for a data-source subagent: fetch raw data from a public API,
 * then ask Qwen to extract a NormalizedFinding (a real identifier + name +
 * confidence) instead of free prose - that's what makes the validation
 * layer's cross-source comparison possible downstream. Any thrown error
 * (network failure, bad JSON, API downtime, timeout, schema mismatch) is
 * caught here and turned into a { ok: false, error } result instead of
 * crashing or hanging the graph.
 */
export async function runFetchSubagent(
  source: string,
  fetchRaw: () => Promise<unknown>,
  normalizePrompt: (raw: unknown) => string,
): Promise<SubagentResult> {
  try {
    const raw = await withTimeout(fetchRaw(), config.fetchTimeoutMs, `${source} fetch`);
    const worker = makeStructuredWorker();
    const res = await withTimeout(
      worker.invoke([new HumanMessage(`${normalizePrompt(raw)}\n\n${FINDING_SCHEMA_INSTRUCTIONS}`)]),
      config.parseTimeoutMs,
      `${source} Qwen normalize`,
    );

    const content = String(res.content);
    let parsed: unknown;
    try {
      parsed = JSON.parse(content);
    } catch {
      return { source, ok: false, error: `Qwen did not return valid JSON: ${content.slice(0, 200)}` };
    }

    const validated = NormalizedFinding.safeParse(parsed);
    if (!validated.success) {
      return { source, ok: false, error: `JSON did not match expected shape: ${validated.error.message}` };
    }

    return { source, ok: true, data: content, finding: validated.data };
  } catch (err) {
    return { source, ok: false, error: err instanceof Error ? err.message : String(err) };
  }
}
