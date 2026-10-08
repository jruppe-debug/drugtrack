import { StateGraph, Annotation, START, END } from "@langchain/langgraph";
import { ctgovSubagent } from "./subagents/ctgov.js";
import { fdaSubagent } from "./subagents/fda.js";
import { chemblSubagent } from "./subagents/chembl.js";
import { discoverySubagent } from "./subagents/discovery.js";
import { rxnormSubagent } from "./subagents/rxnorm.js";
import { backtestSubagent } from "./subagents/backtest.js";
import type { SubagentResult } from "./subagents/common.js";
import { diagnoseFailures } from "./autodebug/autoDebugNode.js";
import { matchAndVerify } from "./graph/matchVerify.js";
import type { ValidationSummary } from "./graph/validate.js";
import { uiVisualizationNode } from "./review/uiVisualization.js";
import { qwenReviewNode } from "./review/qwenReview.js";
import { critiqueRun } from "./review/critique.js";
import { config } from "./config.js";

const DrugTrackState = Annotation.Root({
  query: Annotation<string>,
  results: Annotation<SubagentResult[]>({ default: () => [], reducer: (_a, b) => b }),
  retryCounts: Annotation<Record<string, number>>({ default: () => ({}), reducer: (_a, b) => b }),
  backtestFindings: Annotation<string>,
  debugDiagnosis: Annotation<string>,
  matchedEntityIds: Annotation<string[]>,
  validation: Annotation<ValidationSummary | undefined>,
  manifestPath: Annotation<string>,
  manifestError: Annotation<string | undefined>,
  finalDecision: Annotation<string>,
  reviewError: Annotation<string | undefined>,
  critique: Annotation<string | undefined>,
});

type State = typeof DrugTrackState.State;

/**
 * Wraps a node with start/end timestamps and elapsed time, logged
 * unconditionally - the total-time-vs-per-step-timeout mismatch in an
 * earlier run (5m38s total despite every individual timeout capping at
 * 20s) could not be explained without this; guessing at where time went
 * instead of measuring it directly was the wrong approach.
 */
function timed<S>(name: string, fn: (state: S) => Promise<Partial<State>>) {
  return async (state: S) => {
    const t0 = Date.now();
    try {
      return await fn(state);
    } finally {
      console.error(`[timing] ${name} took ${((Date.now() - t0) / 1000).toFixed(1)}s`);
    }
  };
}

/**
 * Runs promise-producing functions with at most `limit` in flight at once.
 * A single isolated Qwen call measured ~25s on this machine under session
 * load - firing all 5 subagents' Qwen calls at once meant 5 inference
 * requests fighting over the same CPU cores, which is why every single one
 * was timing out. This is the actual fix, not a bigger timeout number.
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

// --- fan-out: the 5 real data-source subagents, at most 2 concurrent Qwen
// calls at a time (see runLimited above) instead of all 5 fighting for CPU.
// rxnorm is the verification layer: an authoritative NIH identity check
// against what ctgov/fda/chembl/discovery independently found. ---
async function fetchAllNode(state: State) {
  console.error(`[fetchAll] starting 5 subagents (max 2 concurrent) for "${state.query}"...`);
  const fetched = await runLimited(
    [
      () => ctgovSubagent(state.query),
      () => fdaSubagent(state.query),
      () => chemblSubagent(state.query),
      () => discoverySubagent(state.query),
      () => rxnormSubagent(state.query),
    ],
    2,
  );
  // Safe: exactly 5 tasks were passed above, so runLimited fills exactly 5 slots.
  const [ctgov, fda, chembl, discovery, rxnorm] = fetched as [
    SubagentResult, SubagentResult, SubagentResult, SubagentResult, SubagentResult,
  ];
  console.error(
    `[fetchAll] done: ` +
    [ctgov, fda, chembl, discovery, rxnorm]
      .map((r) => `${r.source}=${r.ok ? "ok" : `FAIL(${r.error})`}`)
      .join(" "),
  );
  return { results: [ctgov, fda, chembl, discovery, rxnorm] };
}

// --- QA layer: the "Backtest Qwen subagent" from the diagram ---
async function backtestNode(state: State) {
  console.error(`[backtest] starting QA review...`);
  const outcome = await backtestSubagent(state.results);
  console.error(`[backtest] done: ok=${outcome.ok}`);
  return { backtestFindings: outcome.ok ? String(outcome.data) : `backtest itself failed: ${outcome.error}` };
}

// --- auto-debugging: diagnose + retry failed subagents, bounded by MAX_AUTO_DEBUG_RETRIES ---
const rerunBySource: Record<string, (query: string) => Promise<SubagentResult>> = {
  ctgov: ctgovSubagent,
  fda: fdaSubagent,
  chembl: chemblSubagent,
  discovery: discoverySubagent,
  rxnorm: rxnormSubagent,
};

async function autoDebugNode(state: State) {
  console.error(`[autoDebug] checking for failures...`);
  let results = state.results;
  let retryCounts = state.retryCounts;
  let diagnosis = "No failures.";

  for (let round = 0; round < config.maxAutoDebugRetries; round++) {
    let outcome;
    try {
      outcome = await diagnoseFailures(results, retryCounts);
    } catch (err) {
      diagnosis = `diagnose call itself failed: ${err instanceof Error ? err.message : String(err)}`;
      console.error(`[autoDebug] diagnose call FAILED, skipping retry this round: ${diagnosis}`);
      break;
    }
    diagnosis = outcome.diagnosis;
    retryCounts = outcome.retryCounts;
    if (outcome.toRetry.length === 0) break;

    const validRetries = outcome.toRetry.filter((source) => rerunBySource[source]);
    console.error(`[autoDebug] retrying (max 2 concurrent): ${validRetries.join(", ")}`);
    const retried = await runLimited(
      validRetries.map((source) => () => rerunBySource[source]!(state.query)),
      2,
    );
    for (const r of retried) {
      results = results.map((existing) => (existing.source === r.source ? r : existing));
    }
    console.error(
      `[autoDebug] retry round done: ` + retried.filter((r): r is SubagentResult => !!r).map((r) => `${r.source}=${r.ok ? "ok" : "FAIL"}`).join(" "),
    );
  }

  console.error(`[autoDebug] done`);
  return { results, retryCounts, debugDiagnosis: diagnosis };
}

// --- deterministic merge into the shared local graph (data/drugtrack.json) ---
async function matchVerifyNode(state: State) {
  console.error(`[matchVerify] merging into local graph...`);
  const { entityIds, validation } = matchAndVerify(state.query, state.results);
  console.error(
    `[matchVerify] done: ${entityIds.length} entities, ` +
    `${validation.sourcesWithIdentifier}/${validation.sourcesTotal} sources with a real identifier, ` +
    `name agreement: ${validation.nameAgreement}`,
  );
  return { matchedEntityIds: entityIds, validation };
}

// --- fan-out leaf 1: UI manifest for a future Next.js frontend.
// Caught locally so a failure here can NEVER take down the qwenReview
// branch running in parallel - that's what actually crashed the last run. ---
async function uiVisNode(state: State) {
  console.error(`[uiVis] generating manifest...`);
  try {
    const { manifestPath } = await uiVisualizationNode(state.query, state.validation);
    console.error(`[uiVis] done: ${manifestPath}`);
    return { manifestPath };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error(`[uiVis] FAILED: ${message}`);
    return { manifestError: message };
  }
}

// --- fan-out leaf 2: Qwen's final call. Same isolation as uiVisNode. ---
async function qwenReviewNodeFn(state: State) {
  console.error(`[qwenReview] requesting final decision...`);
  try {
    const decision = await qwenReviewNode(
      state.query,
      state.results,
      state.backtestFindings,
      state.debugDiagnosis,
      state.validation,
    );
    console.error(`[qwenReview] done`);
    return { finalDecision: decision };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error(`[qwenReview] FAILED: ${message}`);
    return { reviewError: message };
  }
}

// --- final step: critiques the WHOLE run, after both fan-out leaves finish.
// Distinct from backtest (technical QA of subagent output) - this looks at
// whether the process itself behaved well, step by step. Caught the same
// way as uiVis/qwenReview so it can never crash the run either. ---
async function critiqueNode(state: State) {
  console.error(`[critique] reviewing the full run...`);
  try {
    const critique = await critiqueRun({
      query: state.query,
      results: state.results,
      backtestFindings: state.backtestFindings,
      debugDiagnosis: state.debugDiagnosis,
      validation: state.validation,
      finalDecision: state.finalDecision,
      reviewError: state.reviewError,
      manifestError: state.manifestError,
    });
    console.error(`[critique] done`);
    return { critique };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error(`[critique] FAILED: ${message}`);
    return { critique: `Critique step itself failed: ${message}` };
  }
}

const graph = new StateGraph(DrugTrackState)
  .addNode("fetchAll", timed("fetchAll", fetchAllNode))
  .addNode("backtest", timed("backtest", backtestNode))
  .addNode("autoDebug", timed("autoDebug", autoDebugNode))
  .addNode("matchVerify", timed("matchVerify", matchVerifyNode))
  .addNode("uiVis", timed("uiVis", uiVisNode))
  .addNode("qwenReview", timed("qwenReview", qwenReviewNodeFn))
  .addNode("critiqueStep", timed("critiqueStep", critiqueNode))
  .addEdge(START, "fetchAll")
  .addEdge("fetchAll", "backtest")
  .addEdge("backtest", "autoDebug")
  .addEdge("autoDebug", "matchVerify")
  .addEdge("matchVerify", "uiVis")
  .addEdge("matchVerify", "qwenReview")
  .addEdge("uiVis", "critiqueStep")
  .addEdge("qwenReview", "critiqueStep")
  .addEdge("critiqueStep", END);

export const drugTrackGraph = graph.compile();
