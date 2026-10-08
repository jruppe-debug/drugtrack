import { drugTrackGraph } from "./drugTrackGraph.js";

const query = process.argv[2] ?? "imatinib";

const result = await drugTrackGraph.invoke({ query });

console.log("\n=== Subagent results ===");
for (const r of result.results) {
  console.log(`- ${r.source}: ok=${r.ok}${r.error ? ` error=${r.error}` : ""}`);
}
console.log("\n=== Backtest / QA findings ===\n", result.backtestFindings);
console.log("\n=== Auto-debug diagnosis ===\n", result.debugDiagnosis);
console.log("\n=== Matched entities ===\n", result.matchedEntityIds);

if (result.validation) {
  console.log("\n=== Validation layer ===");
  console.table(result.validation.rows);
  console.log(
    `${result.validation.sourcesWithIdentifier}/${result.validation.sourcesTotal} sources produced a real identifier`,
  );
  console.log(`Name agreement: ${result.validation.nameAgreement}`);
  if (result.validation.notes.length) console.log("Notes:", result.validation.notes.join("; "));
}

if (result.manifestError) {
  console.log("\n=== UI manifest: FAILED ===\n", result.manifestError);
} else {
  console.log("\n=== UI manifest written to ===\n", result.manifestPath);
}

if (result.reviewError) {
  console.log("\n=== Qwen final decision: FAILED ===\n", result.reviewError);
} else {
  console.log("\n=== Qwen final decision ===\n", result.finalDecision);
}

console.log("\n=== Process critique ===\n", result.critique);
