import { ChatOllama } from "@langchain/ollama";
import { ChatAnthropic } from "@langchain/anthropic";
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

/** Anything a runFetchSubagent/challenge call can invoke, regardless of backend (Ollama or Anthropic). */
export interface ChatWorker {
  invoke(messages: HumanMessage[]): Promise<{ content: unknown }>;
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
 * Reserved for the reasoning-heavy, low-volume roles (contrarian review,
 * lead discovery) where local Qwen is the weak link. Deliberately NOT used
 * for the bulk mechanical fetch subagents (ctgov/fda/chembl/rxnorm): those
 * stay free and local on makeStructuredWorker regardless.
 *
 * Uses Claude Haiku when ANTHROPIC_API_KEY is set; otherwise falls back to
 * local qwen2.5:7b (same model/tier as makeQwenWorker) so these two roles
 * still run with zero key and zero cost - just with weaker adversarial
 * reasoning. No flag to remember: add the key later and the next run
 * upgrades itself automatically.
 */
export function makeReasoningWorker(): ChatWorker {
  if (config.anthropicApiKey) {
    return new ChatAnthropic({
      model: config.haikuModel,
      apiKey: config.anthropicApiKey,
      temperature: 0,
    });
  }
  console.error("[makeReasoningWorker] no ANTHROPIC_API_KEY - falling back to local qwen2.5:7b (weaker reasoning)");
  // format: "json" here (unlike makeQwenWorker, which is free-text prose for
  // backtest/critique/qwenReview/discovery) because contrarian and
  // lead-discovery both JSON.parse the response - this fallback must be as
  // parseable as the Haiku path it's replacing, not just reasoning-capable.
  return new ChatOllama({
    model: config.largeModel,
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
  makeWorker: () => ChatWorker = makeStructuredWorker,
): Promise<SubagentResult> {
  try {
    const raw = await withTimeout(fetchRaw(), config.fetchTimeoutMs, `${source} fetch`);
    // Built inside the try so any construction-time failure becomes a
    // clean { ok: false } result instead of an uncaught throw that would
    // crash the graph.
    const worker = makeWorker();
    const res = await withTimeout(
      worker.invoke([new HumanMessage(`${normalizePrompt(raw)}\n\n${FINDING_SCHEMA_INSTRUCTIONS}`)]),
      config.parseTimeoutMs,
      `${source} normalize`,
    );

    const content = String(res.content);
    let parsed: unknown;
    try {
      parsed = JSON.parse(content);
    } catch {
      return { source, ok: false, error: `model did not return valid JSON: ${content.slice(0, 200)}` };
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
