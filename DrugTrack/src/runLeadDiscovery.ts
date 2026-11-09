import { pubmedLeadSubagent } from "./subagents/leadDiscovery.js";

const scopeQuery = process.argv[2] ?? "AML FLT3 inhibitor";
const result = await pubmedLeadSubagent(scopeQuery);

console.log(`\n=== Lead discovery: "${scopeQuery}" (PROVISIONAL - not yet verified) ===`);
if (!result.ok) {
  console.log(`FAILED: ${result.error}`);
} else if (result.finding?.matchedName) {
  console.log(`candidate: ${result.finding.matchedName}`);
  console.log(`source: ${result.finding.identifierType ?? "n/a"} ${result.finding.identifier ?? ""}`);
  console.log(`confidence: ${result.finding.confidence}`);
  console.log(
    "\nThis is unverified. Run it through tools/verify_regimens.py's deterministic check and " +
    "src/review/contrarian.ts before trusting it.",
  );
} else {
  console.log("No candidate surfaced in this batch.");
}
