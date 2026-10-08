-- Debug views comparing the Clinical Tracker snapshot (schema tracker)
-- against the local AACT restore (schema ctgov). Safe to rerun.
CREATE SCHEMA IF NOT EXISTS tracker_debug;

-- 1. Study-level drift: trials the tracker knows vs what AACT says today.
CREATE OR REPLACE VIEW tracker_debug.study_drift AS
SELECT coalesce(t.nct_id, s.nct_id)              AS nct_id,
       t.nct_id IS NOT NULL                      AS in_tracker,
       s.nct_id IS NOT NULL                      AS in_aact,
       nullif(t.phase, '')                       AS phase_tracker,
       s.phase                                   AS phase_aact,
       t.overall_status                          AS status_tracker,
       s.overall_status                          AS status_aact,
       s.brief_title,
       s.last_update_posted_date,
       CASE
         WHEN s.nct_id IS NULL THEN 'missing_in_aact'
         WHEN t.nct_id IS NULL THEN 'new_in_aact'
         WHEN nullif(t.phase, '') IS DISTINCT FROM s.phase
          AND t.overall_status IS DISTINCT FROM s.overall_status THEN 'phase_and_status_changed'
         WHEN nullif(t.phase, '') IS DISTINCT FROM s.phase THEN 'phase_changed'
         WHEN t.overall_status IS DISTINCT FROM s.overall_status THEN 'status_changed'
         ELSE 'match'
       END                                       AS issue
FROM tracker.aact_studies t
FULL JOIN ctgov.studies s USING (nct_id);

-- 2. Does confirmed_in_aact agree with the real AACT?
CREATE OR REPLACE VIEW tracker_debug.report_aact_check AS
SELECT r.report_id, r.source, r.type, r.nct_id, r.confirmed_in_aact,
       s.nct_id IS NOT NULL AS exists_in_aact,
       CASE
         WHEN r.nct_id IS NULL THEN 'no_nct_id'
         WHEN r.confirmed_in_aact = 1 AND s.nct_id IS NULL THEN 'flag_true_but_missing'
         WHEN r.confirmed_in_aact = 0 AND s.nct_id IS NOT NULL THEN 'flag_false_but_present'
         WHEN r.confirmed_in_aact IS NULL THEN 'flag_null'
         ELSE 'ok'
       END AS issue
FROM tracker.clinical_reports r
LEFT JOIN ctgov.studies s ON s.nct_id = r.nct_id;

-- 3. MeSH condition drift: tracker JSON array vs AACT browse_conditions.
-- Tracker stores mesh-list plus mesh-ancestor terms, so compare both.
CREATE INDEX IF NOT EXISTS browse_conditions_nct_term
  ON ctgov.browse_conditions (nct_id, mesh_term);
DROP VIEW IF EXISTS tracker_debug.mesh_drift;
CREATE VIEW tracker_debug.mesh_drift AS
WITH t AS (
  SELECT nct_id, jsonb_array_elements_text(condition_mesh_terms::jsonb) AS term
  FROM tracker.aact_studies
  WHERE condition_mesh_terms LIKE '[%'
), a AS (
  SELECT DISTINCT nct_id, mesh_term AS term
  FROM ctgov.browse_conditions
)
SELECT coalesce(t.nct_id, a.nct_id) AS nct_id,
       coalesce(t.term, a.term)     AS mesh_term,
       CASE WHEN a.term IS NULL THEN 'only_in_tracker' ELSE 'only_in_aact' END AS issue
FROM t
FULL JOIN a ON a.nct_id = t.nct_id AND a.term = t.term
WHERE (t.term IS NULL OR a.term IS NULL)
  AND coalesce(t.nct_id, a.nct_id) IN (SELECT nct_id FROM tracker.aact_studies);

-- 4. Referential breaks inside the tracker ontology itself.
CREATE OR REPLACE VIEW tracker_debug.orphans AS
SELECT 'report_drugs.chembl_id not in drugs' AS check_name, rd.chembl_id AS key, count(*) AS n
FROM tracker.report_drugs rd LEFT JOIN tracker.drugs d USING (chembl_id)
WHERE d.chembl_id IS NULL GROUP BY rd.chembl_id
UNION ALL
SELECT 'report_drugs.report_id not in clinical_reports', rd.report_id, count(*)
FROM tracker.report_drugs rd LEFT JOIN tracker.clinical_reports r USING (report_id)
WHERE r.report_id IS NULL GROUP BY rd.report_id
UNION ALL
SELECT 'report_diseases.disease_id not in diseases', x.disease_id, count(*)
FROM tracker.report_diseases x LEFT JOIN tracker.diseases d USING (disease_id)
WHERE d.disease_id IS NULL GROUP BY x.disease_id
UNION ALL
SELECT 'indications.chembl_id not in drugs', i.chembl_id, count(*)
FROM tracker.indications i LEFT JOIN tracker.drugs d USING (chembl_id)
WHERE d.chembl_id IS NULL GROUP BY i.chembl_id
UNION ALL
SELECT 'indications.disease_id not in diseases', i.disease_id, count(*)
FROM tracker.indications i LEFT JOIN tracker.diseases d USING (disease_id)
WHERE d.disease_id IS NULL GROUP BY i.disease_id
UNION ALL
SELECT 'indication_reports.report_id not in clinical_reports', ir.report_id, count(*)
FROM tracker.indication_reports ir LEFT JOIN tracker.clinical_reports r USING (report_id)
WHERE r.report_id IS NULL GROUP BY ir.report_id
UNION ALL
SELECT 'indication_reports.indication_id not in indications', ir.indication_id, count(*)
FROM tracker.indication_reports ir LEFT JOIN tracker.indications i USING (indication_id)
WHERE i.indication_id IS NULL GROUP BY ir.indication_id;

-- 5. Drug-trial link check: tracker drug for a trial vs AACT interventions.
CREATE OR REPLACE VIEW tracker_debug.trial_drug_links AS
SELECT r.nct_id, d.chembl_id, d.name AS tracker_drug, rd.drug_from_source,
       EXISTS (SELECT 1 FROM ctgov.interventions iv
               WHERE iv.nct_id = r.nct_id
                 AND (lower(iv.name) LIKE '%' || lower(d.name) || '%'
                      OR lower(iv.name) LIKE '%' || lower(rd.drug_from_source) || '%')
              ) AS name_found_in_aact_interventions
FROM tracker.report_drugs rd
JOIN tracker.clinical_reports r USING (report_id)
LEFT JOIN tracker.drugs d USING (chembl_id)
WHERE r.type = 'CLINICAL_TRIAL';

-- Read-only login for Metabase.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'metabase_ro') THEN
    CREATE ROLE metabase_ro LOGIN PASSWORD 'metabase_ro';
  END IF;
END $$;
GRANT USAGE ON SCHEMA ctgov, tracker, source_index, tracker_debug TO metabase_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA ctgov, tracker, source_index, tracker_debug TO metabase_ro;
