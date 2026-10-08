"""
MCP server exposing AACTClient as tools an agent can call.

Mechanism: this uses MCPServer (the official `mcp` Python SDK's high-level
server class -- named FastMCP in SDK v1, renamed to MCPServer in v2; same
decorator API) to wrap the plain functions in client.py. It does three things for us: (1)
turns each Python function into a tool with a JSON schema derived from its
type hints, (2) speaks the MCP protocol over stdio so any MCP-aware agent
(Claude, a Qwen agent via a generic MCP client, etc.) can list and call
these tools, (3) handles the request/response framing -- we never touch
JSON-RPC directly.

Run standalone for a sanity check:
    python -m sources.aact.mcp_server

Run under an MCP client/agent by pointing it at this file with the stdio
transport (command: `python`, args: ["-m", "sources.aact.mcp_server"]).
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from sources.aact.client import AACTClient

mcp = MCPServer("aact")


def _client() -> AACTClient:
    # Constructed per-call, not at import time, so the server can start
    # (and list its tools) even before AACT credentials are configured --
    # it only fails when a tool is actually invoked.
    return AACTClient()


@mcp.tool()
def aact_run_sql(sql: str, limit: int = 500) -> list[dict]:
    """Run a read-only SQL SELECT against the AACT `ctgov` schema and return rows as dicts.
    Use this for anything the convenience tools below don't cover -- see
    https://aact.ctti-clinicaltrials.org/schema for the full table list."""
    return _client().run_query(sql, limit=limit)


@mcp.tool()
def aact_studies_by_condition(condition_term: str, limit: int = 50) -> list[dict]:
    """Find studies whose CT.gov MeSH-mapped condition matches condition_term (substring, case-insensitive)."""
    return _client().studies_by_condition(condition_term, limit=limit)


@mcp.tool()
def aact_studies_by_intervention(drug_term: str, limit: int = 50) -> list[dict]:
    """Find studies whose CT.gov MeSH-mapped intervention matches drug_term (substring, case-insensitive), with each study's condition MeSH term(s)."""
    return _client().studies_by_intervention(drug_term, limit=limit)


@mcp.tool()
def aact_studies_by_nct_ids(nct_ids: list[str]) -> list[dict]:
    """Exact-match lookup for a known list of NCT IDs, with condition MeSH terms aggregated per study."""
    return _client().studies_by_nct_ids(nct_ids)


@mcp.tool()
def aact_interventions_for_study(nct_id: str) -> list[dict]:
    """Raw + MeSH-mapped intervention (drug/device/procedure) rows for one NCT study."""
    return _client().interventions_for_study(nct_id)


@mcp.tool()
def aact_conditions_for_study(nct_id: str) -> list[dict]:
    """Raw + MeSH-mapped condition (indication) rows for one NCT study."""
    return _client().conditions_for_study(nct_id)


if __name__ == "__main__":
    mcp.run()
