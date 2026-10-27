import { join } from "node:path";
import { fileURLToPath } from "node:url";
import * as dotenv from "dotenv";

export const ROOT = join(fileURLToPath(new URL(".", import.meta.url)), "..");

dotenv.config({ path: join(ROOT, ".env") });

export const config = {
  ollamaBaseUrl: process.env.OLLAMA_BASE_URL ?? "http://localhost:11434",
  maxAutoDebugRetries: Number(process.env.MAX_AUTO_DEBUG_RETRIES ?? 2),
  dbPath: join(ROOT, "data", "drugtrack.json"),

  // Two model tiers: a small/fast one for mechanical JSON extraction (the
  // subagent normalize step - just pulling an identifier out of an API
  // response), a larger one for actual reasoning (backtest QA, review,
  // critique). Pull the small one first: `ollama pull qwen2.5:3b`.
  smallModel: process.env.SMALL_MODEL ?? "qwen2.5:3b",
  largeModel: process.env.LARGE_MODEL ?? "qwen2.5:7b",

  // Claude Haiku, used only for the two reasoning-heavy, low-volume roles
  // (contrarian review, lead discovery) where local Qwen is the weak link -
  // not the four bulk mechanical fetch subagents, which stay on smallModel
  // above to keep the high-volume path free and local. Requires
  // ANTHROPIC_API_KEY in .env; add it there, never pass it on a command line.
  anthropicApiKey: process.env.ANTHROPIC_API_KEY,
  haikuModel: process.env.HAIKU_MODEL ?? "claude-haiku-4-5-20251001",

  // "Move on fast" policy, split by role: parsing on the small model should
  // be fast, so it gets a tight budget. Reasoning on the large model gets
  // the same short budget it already had - see PARSE_TIMEOUT_MS to tune
  // independently once the small model's real baseline is measured.
  parseTimeoutMs: Number(process.env.PARSE_TIMEOUT_MS ?? 12_000),
  qwenCallTimeoutMs: Number(process.env.QWEN_CALL_TIMEOUT_MS ?? 20_000),
  fetchTimeoutMs: Number(process.env.FETCH_TIMEOUT_MS ?? 10_000),
};
