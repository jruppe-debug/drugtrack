import { existsSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { config } from "../config.js";

/**
 * The "SHARED GRAPH" box from the architecture diagram, stored as plain JSON
 * instead of a real graph database. Deliberate choice: no native dependency
 * to install (we already hit real pain from koffi/node-pty native installs
 * on this machine), and you can open data/drugtrack.json directly in an
 * editor to see exactly what's in it - no query language required to inspect it.
 */

export interface Entity {
  id: string;
  type: "drug" | "disease" | "target" | "trial";
  name: string;
  source: string;
  identifier?: string | null;
  identifierType?: string | null;
  confidence?: string | null;
  raw: unknown;
}

export interface Edge {
  from: string;
  to: string;
  relation: string;
  source: string;
}

interface GraphFile {
  entities: Entity[];
  edges: Edge[];
}

function loadGraph(): GraphFile {
  if (!existsSync(config.dbPath)) return { entities: [], edges: [] };
  return JSON.parse(readFileSync(config.dbPath, "utf8")) as GraphFile;
}

function saveGraph(graph: GraphFile): void {
  mkdirSync(dirname(config.dbPath), { recursive: true });
  writeFileSync(config.dbPath, JSON.stringify(graph, null, 2), "utf8");
}

/** Insert or overwrite an entity by id. Returns the updated graph size. */
export function upsertEntity(entity: Entity): { entities: number; edges: number } {
  const graph = loadGraph();
  const idx = graph.entities.findIndex((e) => e.id === entity.id);
  if (idx >= 0) graph.entities[idx] = entity;
  else graph.entities.push(entity);
  saveGraph(graph);
  return { entities: graph.entities.length, edges: graph.edges.length };
}

/** Insert an edge if an identical one doesn't already exist (dedupe by from/to/relation). */
export function upsertEdge(edge: Edge): { entities: number; edges: number } {
  const graph = loadGraph();
  const exists = graph.edges.some(
    (e) => e.from === edge.from && e.to === edge.to && e.relation === edge.relation,
  );
  if (!exists) graph.edges.push(edge);
  saveGraph(graph);
  return { entities: graph.entities.length, edges: graph.edges.length };
}

export function readGraph(): GraphFile {
  return loadGraph();
}
