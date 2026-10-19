-- PostgreSQL. Deploy separately; import_package.py is not changed by this file.
-- Removing columns from an existing view requires DROP VIEW followed by CREATE;
-- CREATE OR REPLACE alone cannot deploy this reduced output over the older view.
-- Includes import requests (5, 6, 7) and reverse requests (18), including invalid mappings.
-- Consumer: SELECT * FROM arms.v_mig_import_input
--           ORDER BY request_id DESC;
-- Check is_ready and resolution_error before importing. Do not hide invalid rows.
-- Credentials and environment URL fallbacks remain application configuration.
-- MAX(id)+1 allocation for INSERTs is intentionally NOT provided by this view:
-- cached next IDs are unsafe under concurrent writes.

CREATE OR REPLACE VIEW arms.v_mig_import_input AS
WITH input AS (
    SELECT
        r.request_id,
        r.status_id AS request_status_id,
        r.migration_key_id,
        p.source_project_count,
        p.source_project_name,
        s.status_mapping_count,
        s.tgt_env_id,
        e.environment_mapping_count,
        e.target_env_url,
        BTRIM(e.target_project_prefix) AS target_project_prefix,
        e.status_id_success,
        e.status_id_failed,
        m.migration_object_count,
        m.migration_obj_id,
        h.latest_migration_id
    FROM arms.t_mig_dim_request r
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS source_project_count,
               MIN(BTRIM(project_name)) AS source_project_name
        FROM arms.t_mig_lu_project
        WHERE project_id = r.project_id
    ) p ON TRUE
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS status_mapping_count,
               MIN(BTRIM(tgt_env_id)) AS tgt_env_id
        FROM arms.t_mig_lu_status
        WHERE status_id = r.status_id
    ) s ON TRUE
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS environment_mapping_count,
               MIN(env_url) AS target_env_url,
               MIN(env_prefix) AS target_project_prefix,
               MIN(status_id_success) AS status_id_success,
               MIN(status_id_failed) AS status_id_failed
        FROM arms.t_mig_lu_tgt_env
        WHERE BTRIM(tgt_env_id) = s.tgt_env_id
    ) e ON TRUE
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS migration_object_count,
               MIN(NULLIF(BTRIM(migration_obj_id), '')) AS migration_obj_id
        FROM arms.t_mig_lu_migration_obj
        WHERE migration_key_id = r.migration_key_id
    ) m ON TRUE
    LEFT JOIN LATERAL (
        SELECT MAX(migration_id) AS latest_migration_id
        FROM arms.t_mig_dim_migration
        WHERE request_id = r.request_id
    ) h ON TRUE
    WHERE r.status_id IN (5, 6, 7)
),
source_names AS (
    SELECT input.*,
           BTRIM(CASE
               WHEN STRPOS(source_project_name, ' - ') > 0
               THEN SUBSTRING(source_project_name FROM STRPOS(source_project_name, ' - ') + 3)
               ELSE source_project_name
           END) AS source_project_suffix
    FROM input
),
resolved AS (
    SELECT n.*,
           t.target_project_name_count,
           CASE WHEN t.target_project_name_count = 1
                THEN t.target_project_name END AS target_project_name,
           k.target_project_id_count,
           k.invalid_target_project_id_count,
           CASE WHEN t.target_project_name_count = 1
                     AND k.target_project_id_count = 1
                     AND k.invalid_target_project_id_count = 0
                THEN k.tgt_project_id END AS tgt_project_id,
           COALESCE(n.status_id_failed, 11) AS resolution_failed_status_id
    FROM source_names n
    LEFT JOIN LATERAL (
        SELECT COUNT(DISTINCT names.cleaned_name) AS target_project_name_count,
               MIN(names.cleaned_name) AS target_project_name
        FROM (
            SELECT BTRIM(RTRIM(BTRIM(tp.tgt_project_name), '-')) AS cleaned_name
            FROM arms.t_mig_lu_tgt_project tp
            WHERE tp.status_id = n.request_status_id
              AND BTRIM(tp.tgt_env_id) = n.tgt_env_id
        ) names
        WHERE NULLIF(n.target_project_prefix, '') IS NOT NULL
          AND NULLIF(n.source_project_suffix, '') IS NOT NULL
          AND LEFT(LOWER(names.cleaned_name), LENGTH(n.target_project_prefix))
              = LOWER(n.target_project_prefix)
          AND (
              LENGTH(names.cleaned_name) = LENGTH(n.target_project_prefix)
              OR RIGHT(n.target_project_prefix, 1) IN ('-', '_', '/')
              OR SUBSTRING(names.cleaned_name FROM LENGTH(n.target_project_prefix) + 1 FOR 1)
                  ~ '^[[:space:]_/-]$'
          )
          AND LOWER(BTRIM(LTRIM(
              SUBSTRING(names.cleaned_name FROM LENGTH(n.target_project_prefix) + 1), ' -_/'
          ))) = LOWER(n.source_project_suffix)
    ) t ON TRUE
    -- Match the script's second lookup by the selected name across the target table.
    -- Do not silently pick one numeric key if the same name maps to multiple keys.
    LEFT JOIN LATERAL (
        SELECT
            COUNT(DISTINCT tp.tgt_project_id) AS target_project_id_count,
            COUNT(*) FILTER (
                WHERE tp.tgt_project_id IS NULL
                   OR tp.tgt_project_id::text IN ('NaN', 'Infinity', '-Infinity')
                   OR tp.tgt_project_id <> TRUNC(tp.tgt_project_id)
            ) AS invalid_target_project_id_count,
            MIN(CASE
                WHEN tp.tgt_project_id::text NOT IN ('NaN', 'Infinity', '-Infinity')
                     AND tp.tgt_project_id = TRUNC(tp.tgt_project_id)
                THEN tp.tgt_project_id::numeric
            END) AS tgt_project_id
        FROM arms.t_mig_lu_tgt_project tp
        WHERE t.target_project_name_count = 1
          AND LOWER(BTRIM(tp.tgt_project_name)) = LOWER(t.target_project_name)
    ) k ON TRUE
),
diagnostics AS (
    SELECT resolved.*,
           NULLIF(CONCAT_WS('; ',
               CASE WHEN request_id IS NULL THEN 'Missing request ID' END,
               CASE WHEN source_project_count <> 1 THEN 'Missing or ambiguous source project mapping' END,
               CASE WHEN NULLIF(source_project_suffix, '') IS NULL THEN 'Missing source project name/suffix' END,
               CASE WHEN status_mapping_count <> 1 OR NULLIF(tgt_env_id, '') IS NULL
                    THEN 'Missing or ambiguous status/environment mapping' END,
               CASE WHEN environment_mapping_count <> 1 THEN 'Missing or ambiguous target environment' END,
               CASE WHEN NULLIF(target_project_prefix, '') IS NULL THEN 'Missing target prefix' END,
               CASE WHEN target_project_name_count <> 1 THEN 'Missing or ambiguous matching target project' END,
               CASE WHEN target_project_id_count <> 1 OR invalid_target_project_id_count <> 0
                    THEN 'Missing, invalid or ambiguous numeric target project key' END,
               CASE WHEN migration_object_count <> 1 OR migration_obj_id IS NULL
                    THEN 'Missing or ambiguous migration object' END,
               CASE WHEN status_id_success IS NULL THEN 'Missing success status' END,
               CASE WHEN status_id_failed IS NULL THEN 'Missing failure status' END
           ), '') AS resolution_error
    FROM resolved
),
rollback_input AS (
    SELECT r.request_id, r.status_id AS request_status_id,
           NULL::text AS source_project_name,
           h.migration_key_id, m.migration_obj_id,
           p.tgt_env_id, e.target_env_url, p.target_project_name,
           h.project_id::numeric AS tgt_project_id,
           19 AS status_id_success, 11 AS status_id_failed,
           11 AS resolution_failed_status_id, latest.latest_migration_id,
           NULLIF(CONCAT_WS('; ',
               CASE WHEN latest.latest_migration_id IS NULL OR h.history_count <> 1
                    THEN 'Missing or ambiguous latest migration history' END,
               CASE WHEN h.migration_key_id IS NULL OR m.object_count <> 1 OR m.migration_obj_id IS NULL
                    THEN 'Missing or ambiguous rollback migration object' END,
               CASE WHEN p.mapping_count <> 1 OR p.target_project_name IS NULL OR p.tgt_env_id IS NULL
                    THEN 'Missing or ambiguous historical target project' END,
               CASE WHEN e.environment_count <> 1 THEN 'Missing or ambiguous rollback environment' END
           ), '') AS resolution_error
    FROM arms.t_mig_dim_request r
    LEFT JOIN LATERAL (
        SELECT MAX(migration_id) AS latest_migration_id
        FROM arms.t_mig_dim_migration WHERE request_id = r.request_id
    ) latest ON TRUE
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS history_count, MIN(migration_key_id) AS migration_key_id,
               MIN(project_id) AS project_id
        FROM arms.t_mig_dim_migration
        WHERE request_id = r.request_id AND migration_id = latest.latest_migration_id
    ) h ON TRUE
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS object_count,
               MIN(NULLIF(BTRIM(migration_obj_id), '')) AS migration_obj_id
        FROM arms.t_mig_lu_migration_obj WHERE migration_key_id = h.migration_key_id
    ) m ON TRUE
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS mapping_count,
               MIN(NULLIF(BTRIM(tgt_project_name), '')) AS target_project_name,
               MIN(NULLIF(BTRIM(tgt_env_id), '')) AS tgt_env_id
        FROM arms.t_mig_lu_tgt_project WHERE tgt_project_id = h.project_id
    ) p ON TRUE
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS environment_count, MIN(env_url) AS target_env_url
        FROM arms.t_mig_lu_tgt_env WHERE BTRIM(tgt_env_id) = p.tgt_env_id
    ) e ON TRUE
    WHERE r.status_id = 18
)
SELECT request_id,
       request_status_id,
       source_project_name,
       migration_key_id,
       migration_obj_id,
       tgt_env_id,
       target_env_url,
       target_project_name,
       tgt_project_id,
       status_id_success,
       status_id_failed,
       resolution_failed_status_id,
       latest_migration_id,
       resolution_error,
       resolution_error IS NULL AS is_ready
FROM diagnostics
UNION ALL
SELECT request_id, request_status_id, source_project_name, migration_key_id,
       migration_obj_id, tgt_env_id, target_env_url, target_project_name,
       tgt_project_id, status_id_success, status_id_failed,
       resolution_failed_status_id, latest_migration_id, resolution_error,
       resolution_error IS NULL AS is_ready
FROM rollback_input;

COMMENT ON VIEW arms.v_mig_import_input IS
'Import/reverse input snapshot for request status_id IN (5, 6, 7, 18). Imports use strict status-environment-prefix-source-name resolution; reverse uses the latest request history target and linked migration object. Check is_ready and resolution_error.';
