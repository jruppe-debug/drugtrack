"""
Direct client for AACT (Aggregate Analysis of ClinicalTrials.gov).

Mechanism: AACT is just a read-only PostgreSQL database, refreshed nightly
from ClinicalTrials.gov. There is no REST/GraphQL layer -- you get a
Postgres user/password (free signup at
https://aact.ctti-clinicaltrials.org/users/sign_up) and query the `ctgov`
schema directly with SQL. This module is a thin, safety-railed wrapper
around psycopg2: it opens the connection, forces read-only, and returns
plain dicts (list[dict]) so the results are trivially JSON-serializable.

Key ctgov tables used here (verified live against the real database, not
just the published ERD -- see schema reference: https://aact.ctti-clinicaltrials.org/schema):
  - studies              : one row per NCT study (nct_id, brief_title, phase, overall_status, study_type). 603,073 rows.
  - interventions        : drug/device/procedure names as *registered by the sponsor* (raw strings, not normalized)
  - browse_interventions : sponsor terms mapped to MeSH intervention terms by CT.gov's own indexer (closer to normalized)
  - conditions           : disease/indication names as *registered by the sponsor* (raw strings)
  - browse_conditions    : sponsor terms mapped to MeSH condition terms (closer to normalized)

We deliberately pull both the raw and the MeSH-mapped columns, because the
normalization layer (next phase) needs the raw string to resolve against
RxNorm/UMLS, and having CT.gov's own MeSH mapping alongside it is a useful
cross-check, not a replacement for it.

Structural note, confirmed by querying information_schema and pg_constraint
directly: AACT declares **zero foreign-key or primary-key constraints** --
the ERD's arrows are documentation only, not enforced relationships.
Every table-to-table link is an implicit match on the `nct_id` string
column. We confirmed this implicit structure actually holds (not just
documented) by checking for orphans: 0 of ~600k interventions/conditions/
browse_interventions/browse_conditions rows reference a nct_id missing
from `studies`. So the relationship is real, just not DB-enforced -- an
agent or script querying this schema needs to preserve that join
discipline (always match on nct_id) itself, since Postgres won't.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg2
import psycopg2.extras


class AACTConfigError(RuntimeError):
    pass


class AACTClient:
    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        dbname: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        self.host = host or os.environ.get("AACT_HOST", "aact-db.ctti-clinicaltrials.org")
        self.port = port or int(os.environ.get("AACT_PORT", "5432"))
        self.dbname = dbname or os.environ.get("AACT_DBNAME", "aact")
        self.user = user or os.environ.get("AACT_USER")
        self.password = password or os.environ.get("AACT_PASSWORD")
        if not self.user or not self.password:
            raise AACTConfigError(
                "AACT_USER / AACT_PASSWORD are not set. Register a free account at "
                "https://aact.ctti-clinicaltrials.org/users/sign_up and put the "
                "credentials in .env (see .env.example)."
            )

    @contextmanager
    def _connection(self) -> Iterator[psycopg2.extensions.connection]:
        conn = psycopg2.connect(
            host=self.host,
            port=self.port,
            dbname=self.dbname,
            user=self.user,
            password=self.password,
            # AACT's public endpoint is read-only at the account level, but we
            # also enforce it client-side (below) so a bad query fails fast
            # and locally instead of round-tripping to their server.
            options="-c default_transaction_read_only=on",
        )
        try:
            yield conn
        finally:
            conn.close()

    def run_query(self, sql: str, params: tuple[Any, ...] | None = None, limit: int = 500) -> list[dict]:
        """Run a raw SELECT against ctgov. This is the one method an MCP tool
        or an agent should call directly -- everything else in this file is
        a documented example of what a *good* query against this schema
        looks like."""
        stripped = sql.strip().rstrip(";")
        if not stripped.lower().startswith(("select", "with")):
            raise ValueError("Only SELECT / WITH (read) queries are allowed against AACT.")

        with self._connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"{stripped} LIMIT {int(limit)}", params)
                rows = cur.fetchall()
                return [dict(r) for r in rows]

    # -- Convenience wrappers over the raw query mechanism ------------------

    def studies_by_condition(self, condition_term: str, limit: int = 50) -> list[dict]:
        """Studies whose CT.gov-assigned MeSH condition term matches `condition_term`
        (case-insensitive, word-order-independent). Uses browse_conditions, not the raw
        `conditions` table, because sponsor-entered condition text is
        extremely inconsistent (e.g. "NSCLC", "non small cell lung cancer",
        "Non-Small-Cell Lung Carcinoma" all refer to the same disease).

        Matches per-word rather than as one literal substring because MeSH
        uses inverted heading style -- "acute myeloid leukemia" is stored
        as "Leukemia, Myeloid, Acute" -- so a plain `ILIKE '%acute myeloid
        leukemia%'` silently matches nothing. Found live while testing an
        AML query: this bug shipped for a while before anyone noticed,
        because every prior demo in this repo reached AACT via the
        Open Targets NCT-ID join, never through this method."""
        words = [w for w in condition_term.strip().split() if w]
        if not words:
            return []
        where_clause = " AND ".join(["bc.mesh_term ILIKE %s"] * len(words))
        params = tuple(f"%{w}%" for w in words)
        sql = f"""
            SELECT s.nct_id, s.brief_title, s.phase, s.overall_status, s.study_type,
                   bc.mesh_term AS condition_mesh_term
            FROM ctgov.studies s
            JOIN ctgov.browse_conditions bc ON bc.nct_id = s.nct_id
            WHERE {where_clause}
        """
        return self.run_query(sql, params, limit=limit)

    def interventions_for_study(self, nct_id: str) -> list[dict]:
        """Raw sponsor-entered intervention rows plus CT.gov's own MeSH
        mapping, for one study. This is the pair of fields the RxNorm
        normalization step will consume: `raw_name` -> RxNav lookup,
        `mesh_term` as a cross-check."""
        sql = """
            SELECT i.nct_id, i.intervention_type, i.name AS raw_name, bi.mesh_term
            FROM ctgov.interventions i
            LEFT JOIN ctgov.browse_interventions bi ON bi.nct_id = i.nct_id
            WHERE i.nct_id = %s
        """
        return self.run_query(sql, (nct_id,), limit=200)

    def studies_by_intervention(self, drug_term: str, limit: int = 50) -> list[dict]:
        """Studies whose CT.gov-assigned MeSH intervention term matches
        `drug_term` (word-order-independent, see studies_by_condition for
        why), joined out to that study's condition MeSH terms too -- this
        is AACT's answer to Open Targets' drug->indication list, kept in
        AACT's own vocabulary (MeSH) rather than pre-joined to anything."""
        words = [w for w in drug_term.strip().split() if w]
        if not words:
            return []
        where_clause = " AND ".join(["bi.mesh_term ILIKE %s"] * len(words))
        params = tuple(f"%{w}%" for w in words)
        sql = f"""
            SELECT s.nct_id, s.brief_title, s.phase, s.overall_status,
                   bi.mesh_term AS intervention_mesh_term,
                   bc.mesh_term AS condition_mesh_term
            FROM ctgov.studies s
            JOIN ctgov.browse_interventions bi ON bi.nct_id = s.nct_id
            LEFT JOIN ctgov.browse_conditions bc ON bc.nct_id = s.nct_id
            WHERE {where_clause}
        """
        return self.run_query(sql, params, limit=limit)

    def studies_by_nct_ids(self, nct_ids: list[str]) -> list[dict]:
        """Exact-match lookup for a known set of NCT IDs, with condition MeSH
        terms aggregated per study. This is the AACT side of the strongest
        cross-source join available: Open Targets embeds real NCT IDs
        (source == "AACT") in its drug-indication clinicalReports, so this
        method lets us confirm those referenced trials actually exist here
        and pull AACT's own view of them for a side-by-side comparison --
        no name matching involved."""
        if not nct_ids:
            return []
        sql = """
            SELECT s.nct_id, s.brief_title, s.phase, s.overall_status, s.study_type,
                   array_agg(DISTINCT bc.mesh_term) FILTER (WHERE bc.mesh_term IS NOT NULL) AS condition_mesh_terms
            FROM ctgov.studies s
            LEFT JOIN ctgov.browse_conditions bc ON bc.nct_id = s.nct_id
            WHERE s.nct_id = ANY(%s)
            GROUP BY s.nct_id, s.brief_title, s.phase, s.overall_status, s.study_type
        """
        return self.run_query(sql, (nct_ids,), limit=len(nct_ids))

    def conditions_for_study(self, nct_id: str) -> list[dict]:
        sql = """
            SELECT c.nct_id, c.name AS raw_name, bc.mesh_term
            FROM ctgov.conditions c
            LEFT JOIN ctgov.browse_conditions bc ON bc.nct_id = c.nct_id
            WHERE c.nct_id = %s
        """
        return self.run_query(sql, (nct_id,), limit=200)
