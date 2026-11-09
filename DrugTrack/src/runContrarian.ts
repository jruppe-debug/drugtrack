import { writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { runContrarianReview } from "./review/contrarian.js";
import { ROOT } from "./config.js";

const disease = process.argv[2] ?? "aml_gen";
const report = await runContrarianReview(disease);
const outDir = join(ROOT, "data", "diseases", disease);
mkdirSync(outDir, { recursive: true });
const outPath = join(outDir, "contrarian_review.json");
writeFileSync(outPath, JSON.stringify(report, null, 2), "utf8");

console.log(`\n=== Contrarian review: ${report.disease} ===`);
console.log(`Universe generated: ${report.universeGenerated}`);
console.log(
  `deterministic_correct_%: ${report.deterministicCorrectPct}% ` +
  `(${report.survives}/${report.total} survive adversarial challenge)`,
);
console.log(`disproven=${report.disproven} needs_review=${report.needsReview}`);
console.log(`Full report written to ${outPath}`);

const flagged = report.records.filter((r) => r.verdict !== "survives");
if (flagged.length) {
  console.log("\n=== Flagged records ===");
  for (const r of flagged) {
    console.log(`- ${r.recordId} [${r.deterministicStatus} -> ${r.verdict}]: ${r.reason}`);
  }
}
