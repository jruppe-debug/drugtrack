import { HumanMessage } from "@langchain/core/messages";
import { makeQwenWorker, withTimeout, type SubagentResult } from "./common.js";
import { config } from "../config.js";

/**
 * Unlike ctgov/fda/chembl, this node doesn't call a fixed API - it's a
 * reasoning step that proposes additional sources/cross-references worth
 * checking for a given query. Deliberately not wired to a real search API
 * yet (would need a real provider + key); it returns candidate leads for a
 * human (or a later automated step) to follow up on.
 */
export async function discoverySubagent(query: string): Promise<SubagentResult> {
  try {
    const worker = makeQwenWorker("discovery");
    const res = await withTimeout(
      worker.invoke([
        new HumanMessage(
          `For the drug/target/disease query "${query}", list other public databases or ` +
          `cross-reference identifiers (e.g. DrugBank, UniProt, PubChem, OMIM) worth checking ` +
          `that aren't ClinicalTrials.gov, openFDA, or ChEMBL. Be concise and concrete.`,
        ),
      ]),
      config.qwenCallTimeoutMs,
      "discovery Qwen call",
    );
    return { source: "discovery", ok: true, data: res.content };
  } catch (err) {
    return { source: "discovery", ok: false, error: err instanceof Error ? err.message : String(err) };
  }
}
