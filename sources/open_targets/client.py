"""
Client for the Open Targets Platform GraphQL API.

Mechanism: a single public HTTPS endpoint
(https://api.platform.opentargets.org/api/v4/graphql) that takes a POST
with {"query": "<GraphQL>", "variables": {...}} and returns
{"data": {...}} or {"errors": [...]}. No API key, no auth header. This
module sends exactly that -- there is no SDK, just `requests` and four
query strings that were verified against the live schema (see below).

Entity IDs, verified live on 2026-09-17 against api version 26.6.3:
  - drug   -> ChEMBL ID, e.g. "CHEMBL941" (imatinib)
  - target -> Ensembl gene ID, e.g. "ENSG00000097007" (ABL1)
  - disease -> the GraphQL argument is still named `efoId` for historical
    reasons, but the platform migrated its disease ontology from EFO to
    MONDO; live values returned by `search` are MONDO_* IDs (e.g.
    "MONDO_0011996" for CML), not EFO_* IDs. Passing an old EFO_* ID will
    return null. This is exactly the kind of "the field name lies about
    what's actually in it" trap the normalization layer needs to know
    about up front.

The four methods below are 1:1 wrappers around four queries, not a
generic ORM -- keeping the GraphQL visible and unabstracted is the point
of this prototype.

Cross-source join key, found by introspecting the live schema: each
indication row's `clinicalReports` field lists the underlying evidence
records, and when one has `source == "AACT"`, its `id` is a literal
ClinicalTrials.gov NCT ID (lowercase, e.g. "nct00080665"). Open Targets
sources that evidence FROM AACT, so this is a real foreign key back to
AACT's `studies.nct_id`, not a name to fuzzy-match -- confirmed live for
imatinib, 86/89 indications carried at least one AACT-sourced NCT ID.
`drug_indications()` pulls this field for exactly that reason.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

DEFAULT_URL = "https://api.platform.opentargets.org/api/v4/graphql"

_SEARCH = """
query Search($q: String!, $entities: [String!], $size: Int!) {
  search(queryString: $q, entityNames: $entities, page: {index: 0, size: $size}) {
    total
    hits { id entity name description }
  }
}
"""

_DRUG_INDICATIONS = """
query DrugIndications($chemblId: String!) {
  drug(chemblId: $chemblId) {
    id
    name
    drugType
    maximumClinicalStage
    indications {
      count
      rows {
        maxClinicalStage
        disease { id name }
        clinicalReports { id source type url }
      }
    }
  }
}
"""

_DISEASE_ASSOCIATED_TARGETS = """
query DiseaseTargets($id: String!, $size: Int!) {
  disease(efoId: $id) {
    id
    name
    associatedTargets(page: {index: 0, size: $size}) {
      count
      rows { score target { id approvedSymbol } }
    }
  }
}
"""

_TARGET_ASSOCIATED_DISEASES = """
query TargetDiseases($id: String!, $size: Int!) {
  target(ensemblId: $id) {
    id
    approvedSymbol
    associatedDiseases(page: {index: 0, size: $size}) {
      count
      rows { score disease { id name } }
    }
  }
}
"""


class OpenTargetsClient:
    def __init__(self, url: str | None = None) -> None:
        self.url = url or os.environ.get("OPEN_TARGETS_GRAPHQL_URL", DEFAULT_URL)
        self.session = requests.Session()

    def _query(self, query: str, variables: dict[str, Any], retries: int = 3) -> dict:
        # The public endpoint occasionally resets the connection under load
        # (observed directly while building this) -- a short retry is
        # cheap insurance, not defensive overengineering.
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                resp = self.session.post(self.url, json={"query": query, "variables": variables}, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                if "errors" in data:
                    raise RuntimeError(f"Open Targets GraphQL error: {data['errors']}")
                return data["data"]
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
        raise last_exc  # type: ignore[misc]

    def search(self, query_string: str, entity_names: list[str] | None = None, size: int = 10) -> list[dict]:
        """Free-text search across drug/target/disease entities. Returns the
        raw hits (id, entity type, name, description) -- this is how you
        turn a plain-English drug or disease name into the ID the other
        three methods need."""
        data = self._query(_SEARCH, {"q": query_string, "entities": entity_names, "size": size})
        return data["search"]["hits"]

    def drug_indications(self, chembl_id: str) -> dict:
        """All indications curated for a drug, each with the highest
        clinical phase reached for that specific indication."""
        return self._query(_DRUG_INDICATIONS, {"chemblId": chembl_id})["drug"]

    def disease_associated_targets(self, disease_id: str, size: int = 25) -> dict:
        """Targets (genes/proteins) associated with a disease, ranked by
        Open Targets' aggregate evidence score. `disease_id` is a MONDO_*
        id despite the underlying GraphQL argument being named efoId."""
        return self._query(_DISEASE_ASSOCIATED_TARGETS, {"id": disease_id, "size": size})["disease"]

    def target_associated_diseases(self, ensembl_id: str, size: int = 25) -> dict:
        """Diseases associated with a target (gene), ranked by score. Inverse of disease_associated_targets."""
        return self._query(_TARGET_ASSOCIATED_DISEASES, {"id": ensembl_id, "size": size})["target"]
