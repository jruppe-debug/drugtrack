import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { createReadStream, existsSync } from "node:fs";
import { join } from "node:path";
import { ROOT, config } from "./config.js";

/**
 * Zero-dependency local dashboard: built-in node:http server, no Express,
 * no build step. Deliberate choice after this session's repeated pain from
 * native-module and version-mismatch issues (koffi, node-pty, langgraph-cli)
 * - this has nothing that can conflict, ever.
 */
const PORT = Number(process.env.VIZ_PORT ?? 4300);
const manifestPath = join(ROOT, "data", "ui-manifest.json");
const critiquePath = join(ROOT, "data", "critique.json");

const server = createServer(async (req, res) => {
  try {
    if (req.url === "/") {
      res.writeHead(302, { Location: "/disease" });
      res.end();
      return;
    }

    if (req.url === "/index.html") {
      const html = await readFile(join(ROOT, "public", "index.html"), "utf8");
      res.writeHead(200, { "Content-Type": "text/html", "Cache-Control": "no-store" });
      res.end(html);
      return;
    }

    if (req.url === "/api/graph") {
      const raw = existsSync(config.dbPath)
        ? await readFile(config.dbPath, "utf8")
        : JSON.stringify({ entities: [], edges: [] });
      res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
      res.end(raw);
      return;
    }

    if (req.url === "/api/manifest") {
      // Always 200, never a real HTTP error status - a missing manifest is
      // an expected, normal state (uiVis hasn't succeeded yet), not a fault.
      // Using 404 for this made the browser console log a wall of "failed
      // to load resource" errors every 4s from the auto-refresh, which is
      // exactly the kind of noise that makes a working page look broken.
      if (!existsSync(manifestPath)) {
        res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
        res.end(JSON.stringify({ available: false }));
        return;
      }
      const raw = await readFile(manifestPath, "utf8");
      const parsed = JSON.parse(raw);
      res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
      res.end(JSON.stringify({ available: true, ...parsed }));
      return;
    }

    if (req.url === "/disease") {
      const html = await readFile(join(ROOT, "public", "disease.html"), "utf8");
      res.writeHead(200, { "Content-Type": "text/html", "Cache-Control": "no-store" });
      res.end(html);
      return;
    }

    // Deterministic disease tree (tools/export_disease_tree.py). Served
    // byte-for-byte: this server never parses, filters, or rewrites it.
    if (req.url === "/api/tree") {
      const treePath = join(ROOT, "data", "tree.json");
      if (!existsSync(treePath)) {
        res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
        res.end(JSON.stringify({ available: false }));
        return;
      }
      // streamed from disk: the 50 MB file is never held in server memory as one string
      res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
      createReadStream(treePath).pipe(res);
      return;
    }

    // Generic pipeline view per disease (tools/export_paradigm.py): data/diseases/<disease>/paradigm.json, ~100 KB.
    if (req.url === "/paradigm" || req.url?.startsWith("/paradigm?")) {
      const html = await readFile(join(ROOT, "public", "paradigm.html"), "utf8");
      res.writeHead(200, { "Content-Type": "text/html", "Cache-Control": "no-store" });
      res.end(html);
      return;
    }
    // Contrarian adversarial review (src/review/contrarian.ts): per-disease
    // deterministic_correct_% and per-record verdicts, same serving shape as
    // paradigm/finder below.
    if (req.url?.startsWith("/api/paradigm") || req.url?.startsWith("/api/finder") || req.url?.startsWith("/api/contrarian")) {
      const d = new URL(req.url, "http://x").searchParams.get("disease") ?? "cll";
      const file = req.url.startsWith("/api/finder")
        ? "finder.json"
        : req.url.startsWith("/api/contrarian")
          ? "contrarian_review.json"
          : "paradigm.json";
      const p = join(ROOT, "data", "diseases", d.replace(/[^a-z0-9_]/gi, ""), file);
      if (!existsSync(p)) {
        res.writeHead(404, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ available: false, disease: d }));
        return;
      }
      res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
      createReadStream(p).pipe(res);
      return;
    }

    // Drug + indication branches: NCT list and MeSH-tagged PubMed article support
    // (tools/drug_lit.py), shown on the main viewer. Served byte-for-byte like /api/tree.
    if (req.url === "/api/drug-lit") {
      const drugLitPath = join(ROOT, "data", "drug_lit_imatinib.json");
      if (!existsSync(drugLitPath)) {
        res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
        res.end(JSON.stringify({ available: false }));
        return;
      }
      res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
      res.end(await readFile(drugLitPath, "utf8"));
      return;
    }

    // PubMed support for one drug node of the disease tree, scoped to the tree's disease
    // (tools/scope_drug_lit.py). Served byte-for-byte like /api/tree.
    if (req.url === "/api/scope-lit") {
      const scopeLitPath = join(ROOT, "data", "scope_lit_CHEMBL941.json");
      if (!existsSync(scopeLitPath)) {
        res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
        res.end(JSON.stringify({ available: false }));
        return;
      }
      res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
      res.end(await readFile(scopeLitPath, "utf8"));
      return;
    }

    // DailyMed SPL figure images cached by tools/dailymed_label.py ({setid}_v{version}/{file}).
    // Both path parts are whitelisted so the URL cannot leave data/dailymed/versions.
    const spl = req.url?.match(/^\/spl-img\/([0-9a-f-]{36}_v\d{1,4})\/([A-Za-z0-9._-]{1,120}\.(?:jpe?g|png|gif))$/i);
    if (spl) {
      const [, splDir, splFile] = spl;
      const imgPath = join(ROOT, "data", "dailymed", "versions", splDir!, splFile!);
      if (!existsSync(imgPath)) {
        res.writeHead(404);
        res.end();
        return;
      }
      const ext = splFile!.split(".").pop()!.toLowerCase();
      res.writeHead(200, { "Content-Type": ext === "png" ? "image/png" : ext === "gif" ? "image/gif" : "image/jpeg", "Cache-Control": "max-age=86400" });
      res.end(await readFile(imgPath));
      return;
    }

    // Playwright link-check results (tools/validate_lit_urls.py), keyed by PMID.
    // Served byte-for-byte like /api/tree.
    if (req.url === "/api/lit") {
      const litPath = join(ROOT, "data", "lit_validated.json");
      if (!existsSync(litPath)) {
        res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
        res.end(JSON.stringify({ available: false }));
        return;
      }
      res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
      res.end(await readFile(litPath, "utf8"));
      return;
    }

    if (req.url === "/api/critique") {
      if (!existsSync(critiquePath)) {
        res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
        res.end(JSON.stringify({ available: false }));
        return;
      }
      const raw = await readFile(critiquePath, "utf8");
      const parsed = JSON.parse(raw);
      res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
      res.end(JSON.stringify({ available: true, ...parsed }));
      return;
    }

    res.writeHead(404);
    res.end("Not found");
  } catch (err) {
    res.writeHead(500, { "Content-Type": "text/plain" });
    res.end(err instanceof Error ? err.message : String(err));
  }
});

server.listen(PORT, () => {
  console.log(`DrugTrack viewer running at http://localhost:${PORT}`);
});
