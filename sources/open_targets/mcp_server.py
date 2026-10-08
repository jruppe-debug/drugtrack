"""
MCP server exposing OpenTargetsClient as tools an agent can call.

Same mechanism as sources/aact/mcp_server.py: MCPServer (the `mcp` SDK's
high-level server class -- called FastMCP in SDK v1, renamed in v2, same
decorator-based API) turns each function below into a schema'd MCP tool
and speaks the protocol over stdio. Because
Open Targets needs no credentials, this server is immediately runnable --
compare that to the AACT server, which will error at call time until
AACT_USER/AACT_PASSWORD are set.

Run standalone for a sanity check:
    python -m sources.open_targets.mcp_server
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from sources.open_targets.client import OpenTargetsClient

mcp = MCPServer("open_targets")
_client = OpenTargetsClient()  # no credentials needed, safe to build once


@mcp.tool()
def ot_search(query_string: str, entity_names: list[str] | None = None, size: int = 10) -> list[dict]:
    """Free-text search across Open Targets drug/target/disease entities.
    entity_names filters to a subset, e.g. ["drug"], ["disease"], ["target"].
    Use this first to resolve a plain-English name to the ChEMBL/Ensembl/MONDO
    ID the other tools require."""
    return _client.search(query_string, entity_names=entity_names, size=size)


@mcp.tool()
def ot_drug_indications(chembl_id: str) -> dict:
    """All curated indications for a drug (by ChEMBL ID), each tagged with
    the highest clinical phase reached for that specific indication."""
    return _client.drug_indications(chembl_id)


@mcp.tool()
def ot_disease_associated_targets(disease_id: str, size: int = 25) -> dict:
    """Targets associated with a disease (by MONDO ID), ranked by Open
    Targets' evidence score."""
    return _client.disease_associated_targets(disease_id, size=size)


@mcp.tool()
def ot_target_associated_diseases(ensembl_id: str, size: int = 25) -> dict:
    """Diseases associated with a target (by Ensembl gene ID), ranked by score."""
    return _client.target_associated_diseases(ensembl_id, size=size)


if __name__ == "__main__":
    mcp.run()
