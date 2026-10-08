import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { HumanMessage } from "@langchain/core/messages";
import { makeQwenWorker, withTimeout } from "../subagents/common.js";
import { readGraph } from "../graph/sharedGraph.js";
import type { ValidationSummary } from "../graph/validate.js";
import { config, ROOT } from "../config.js";

/**
 * Produces a UI-ready JSON manifest a future Next.js frontend can fetch and
 * render directly - this node does NOT build the UI itself. Keeping the
 * manifest as plain JSON on disk means the Next.js app is a separate,
 * independently-buildable project that just reads this file (or, later,
 * an API route backed by it) rather than being coupled into this graph.
 */
export async function uiVisualizationNode(
  query: string,
  validation: ValidationSummary | undefined,
): Promise<{ manifestPath: string }> {
  const graph = readGraph();
  const worker = makeQwenWorker("ui-vis");
  const res = await withTimeout(
    worker.invoke([
      new HumanMessage(
        `Given this drug-search graph data, write a one-paragraph plain-English summary ` +
        `suitable as a UI header, plus a short bullet list of the top findings:\n\n` +
        JSON.stringify(graph).slice(0, 4000),
      ),
    ]),
    config.qwenCallTimeoutMs,
    "uiVis Qwen call",
  );

  const manifest = {
    query,
    generatedAt: new Date().toISOString(),
    summary: String(res.content),
    entities: graph.entities,
    edges: graph.edges,
    validation: validation ?? null,
  };

  const manifestPath = join(ROOT, "data", "ui-manifest.json");
  writeFileSync(manifestPath, JSON.stringify(manifest, null, 2), "utf8");
  return { manifestPath };
}
