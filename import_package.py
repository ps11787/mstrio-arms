"""Import a prebuilt MicroStrategy migration package.

See [docs/import_package.md](docs/import_package.md) for the full workflow and
error-handling documentation.
"""

from __future__ import annotations

import os
from datetime import datetime
from time import sleep

from mstrio.connection import Connection
from mstrio.datasources.datasource_instance import DatasourceInstance
from mstrio.object_management.migration import Migration
from mstrio.object_management.migration.package import ImportStatus, PackageStatus
from mstrio.project_objects.report import Report
from mstrio.server.job_monitor import list_jobs


BASE_URL = os.getenv("MSTR_BASE_URL", "https://env-371825.ma.cloud.microstrategy.com/MicroStrategyLibrary")
SOURCE_ENVIRONMENT = os.getenv("MSTR_SOURCE_ENVIRONMENT", "dev")
ENVIRONMENT_URLS = {
    "dev": BASE_URL,
    # Example mapping for future environments. Keep this configurable so the
    # target environment URL can be changed without editing the logic itself.
    # "test": "https://env-xxxx.ma.cloud.microstrategy.com/MicroStrategyLibrary",
    # "qa": "https://env-yyyy.ma.cloud.microstrategy.com/MicroStrategyLibrary",
}
ENVIRONMENT_URLS.update({
    key.strip().lower(): value.strip()
    for key, value in {
        "test": os.getenv("MSTR_TEST_URL", ""),
        "qa": os.getenv("MSTR_QA_URL", ""),
        "prod": os.getenv("MSTR_PROD_URL", ""),
    }.items()
    if value and value.strip()
})
MSTR_USERNAME = os.getenv("MSTR_USERNAME", "mstr")
MSTR_PASSWORD = os.getenv("MSTR_PASSWORD", "EW85AcqE1SoT")
PROJECT_ID = os.getenv("MSTR_PROJECT_ID", "4B0544627D49F8DA03C26D817D208263")
REQUEST_REPORT_ID = os.getenv("MSTR_REQUEST_REPORT_ID", "A62CDD7147492E20EE83C5841B7C9183")
DATASOURCE_INSTANCE_ID = os.getenv("MSTR_DATASOURCE_INSTANCE_ID", "A23BBC514D336D5B4FCE919FE19661A3")
TARGET_PROJECT_TABLE = os.getenv("MSTR_TARGET_PROJECT_TABLE", "arms.t_mig_lu_tgt_project")
STATUS_TABLE = os.getenv("MSTR_STATUS_TABLE", "arms.t_mig_lu_status")
TARGET_ENV_TABLE = os.getenv("MSTR_TARGET_ENV_TABLE", "arms.t_mig_lu_tgt_env")
REQUEST_TABLE = os.getenv("MSTR_REQUEST_TABLE", "arms.t_mig_dim_request")
PROJECT_LOOKUP_TABLE = os.getenv("MSTR_PROJECT_LOOKUP_TABLE", "arms.t_mig_lu_project")
MIGRATION_OBJECT_TABLE = os.getenv("MSTR_MIGRATION_OBJECT_TABLE", "arms.t_mig_lu_migration_obj")
REQUEST_STATUS_FILTER = tuple(
    int(item.strip())
    for item in os.getenv("MSTR_REQUEST_STATUS_FILTER", "5, 7, 9").split(",")
    if item.strip()
)
FORCE_UNLOCK_LOCKED_PACKAGE = os.getenv("MSTR_FORCE_UNLOCK_LOCKED_PACKAGE", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "y",
}


def _normalize_lookup_key(value: object) -> str:
    return str(value).strip().lower().replace(" ", "_").replace("-", "_")


def _get_first_value_from_result_data(data: dict[str, object], *candidate_keys: str) -> str | None:
    """Return the first non-empty value from a result-data dict for any candidate key.

    Some datasource responses return a scalar value instead of a list, especially
    when a single-row query is emitted. Handle both shapes without raising.
    """
    normalized_candidates = {_normalize_lookup_key(key): key for key in candidate_keys}
    for column_name, raw_value in data.items():
        normalized_name = _normalize_lookup_key(column_name)
        if normalized_name not in normalized_candidates:
            continue

        if isinstance(raw_value, (list, tuple)):
            values = raw_value
            if not values:
                continue
            value = values[0]
        else:
            value = raw_value

        if value is None:
            continue
        text_value = str(value).strip()
        if text_value:
            return text_value
    return None


def _rows_from_result(result: dict) -> list[dict[str, object]]:
    """Convert a datasource query result into a list of dictionary rows."""
    data = result.get("results", {}).get("data", {})
    if not data:
        return []

    normalized_data: dict[str, list[object]] = {}
    for column_name, raw_values in data.items():
        if isinstance(raw_values, (list, tuple)):
            normalized_data[column_name] = list(raw_values)
        else:
            normalized_data[column_name] = [raw_values]

    columns = list(normalized_data.keys())
    row_count = max((len(values) for values in normalized_data.values()), default=0)
    rows: list[dict[str, object]] = []
    for row_index in range(row_count):
        row: dict[str, object] = {}
        for column_name in columns:
            values = normalized_data.get(column_name, [])
            row[column_name] = values[row_index] if row_index < len(values) else None
        rows.append(row)
    return rows


def _coerce_int(value: object) -> int:
    """Normalize integer-like values returned by data-source queries, including float strings such as '13.0'."""
    if value is None:
        raise ValueError("Cannot coerce None to int.")
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        raise ValueError("Cannot coerce empty string to int.")
    return int(float(text))


def _fetch_requests_for_statuses(
    datasource: DatasourceInstance,
    project_id: str,
    status_ids: tuple[int, ...] = REQUEST_STATUS_FILTER,
) -> list[dict[str, object]]:
    """Select all request rows whose status is one of the configured migration statuses."""
    if not status_ids:
        return []

    status_in_list = ", ".join(str(status_id) for status_id in status_ids)
    query = (
        f"SELECT * FROM {REQUEST_TABLE} "
        f"WHERE status_id IN ({status_in_list}) "
        "ORDER BY request_id DESC;"
    )
    result = datasource.execute_query(project_id=project_id, query=query)
    return _rows_from_result(result)


def _resolve_migration_object_guid(
    datasource: DatasourceInstance,
    project_id: str,
    request_row: dict[str, object],
) -> str | None:
    """Resolve the migration object GUID from the request's migration_key_id in the lookup table."""
    migration_key_id = _get_first_value_from_result_data(
        request_row,
        "migration_key_id",
        "migration_key",
        "migration_keyid",
        "key_id",
    )
    if not migration_key_id:
        return None

    query = (
        f"SELECT * FROM {MIGRATION_OBJECT_TABLE} "
        f"WHERE migration_key_id = {migration_key_id};"
    )
    result = datasource.execute_query(project_id=project_id, query=query)
    for row in _rows_from_result(result):
        migration_obj_guid = _get_first_value_from_result_data(
            row,
            "migration_obj_guid",
            "migration_guid",
            "migration_object_guid",
            "migration_obj_id",
            "migration_id",
            "guid",
            "obj_guid",
        )
        if migration_obj_guid:
            return str(migration_obj_guid).strip()
    return None


def _get_status_target_env_id(
    datasource: DatasourceInstance,
    status_id: int | str,
    project_id: str,
) -> str | None:
    """Resolve the target environment ID from the request status row.

    In the live ARMS_WH schema, the status table currently stores only the status
    metadata (status_id, status_name, status_desc, ...). It does not include a
    target-environment foreign key. In that case, return None so the caller can
    transparently fall back to the default environment instead of crashing.
    """
    query = f"SELECT * FROM {STATUS_TABLE} WHERE status_id = {status_id};"
    result = datasource.execute_query(project_id=project_id, query=query)
    for row in _rows_from_result(result):
        env_id = _get_first_value_from_result_data(
            row,
            "tgt_env_id",
            "target_env_id",
            "env_id",
            "environment_id",
            "tgt_environment_id",
            "target_environment_id",
        )
        if env_id:
            return str(env_id).strip()
    return None


def _get_target_environment_details(
    datasource: DatasourceInstance,
    target_env_id: str,
    project_id: str,
) -> dict[str, str]:
    """Resolve the target environment metadata from the target environment lookup table."""
    query = f"SELECT * FROM {TARGET_ENV_TABLE};"
    result = datasource.execute_query(project_id=project_id, query=query)
    for row in _rows_from_result(result):
        current_env_id = _get_first_value_from_result_data(
            row,
            "tgt_env_id",
            "target_env_id",
            "env_id",
            "environment_id",
            "tgt_environment_id",
            "target_environment_id",
        )
        if current_env_id and str(current_env_id).strip() == str(target_env_id).strip():
            return {
                "env_id": str(current_env_id).strip(),
                "env_name": _get_first_value_from_result_data(
                    row,
                    "tgt_env_name",
                    "target_env_name",
                    "env_name",
                    "environment_name",
                    "target_environment",
                    "environment",
                    "env",
                )
                or str(target_env_id).strip(),
                "prefix": _get_first_value_from_result_data(
                    row,
                    "project_prefix",
                    "tgt_project_prefix",
                    "target_project_prefix",
                    "tgt_env_prefix",
                    "env_prefix",
                    "project_name_prefix",
                    "prefix",
                )
                or "",
                "url": _get_first_value_from_result_data(
                    row,
                    "tgt_env_url",
                    "target_env_url",
                    "env_url",
                    "environment_url",
                    "target_url",
                    "base_url",
                    "url",
                )
                or "",
            }
    raise ValueError(
        f"No target environment metadata found in {TARGET_ENV_TABLE} for target environment '{target_env_id}'."
    )


def _resolve_target_environment_url(
    base_url: str,
    source_environment: str,
    target_environment: str,
    environment_urls: dict[str, str] | None = None,
) -> str:
    """Resolve the target MicroStrategy URL from a configurable environment map."""
    url_map = {
        str(key).strip().lower(): str(value).strip()
        for key, value in (environment_urls or ENVIRONMENT_URLS).items()
    }

    target_key = str(target_environment).strip().lower()
    if target_key in url_map:
        return url_map[target_key]

    source_key = str(source_environment).strip().lower() or SOURCE_ENVIRONMENT
    if target_key == source_key:
        return str(base_url).strip()

    cleaned_base_url = str(base_url).strip()
    if not cleaned_base_url:
        raise ValueError("Base URL is empty; target environment URL cannot be resolved.")

    # Safe default: keep the source base URL until the environment-specific target URL is
    # configured centrally. This keeps the script operational while allowing future mapping.
    return cleaned_base_url


def _strip_prefix_from_project_name(project_name: str, prefix: str) -> str:
    """Remove a leading project prefix from a project name while preserving the rest."""
    name = str(project_name).strip()
    clean_prefix = str(prefix).strip()
    if not name or not clean_prefix:
        return name

    prefix_variants = {clean_prefix, clean_prefix.lower(), clean_prefix.upper()}
    for variant in prefix_variants:
        if name.lower().startswith(variant.lower()):
            remainder = name[len(variant):].lstrip(" -_/")
            return remainder.strip()

    return name


def _derive_target_project_name(source_project_name: str, target_prefix: str, fallback_name: str | None = None) -> str:
    """Build the target project name as: {target_prefix} + {source project without prefix}.

    Example: prefix='TGT', source project='DEV - Finance' -> 'TGT - Finance'
    If the source project name is blank, use the request target project name or the prefix alone.
    """
    source_name = str(source_project_name).strip()
    prefix = str(target_prefix).strip()
    if not source_name:
        source_name = str(fallback_name or "").strip()
    if not source_name:
        if prefix:
            return prefix
        raise ValueError("Source project name is empty and no fallback target project name or prefix is available.")
    if not prefix:
        return source_name

    name_without_prefix = _strip_prefix_from_project_name(source_name, prefix)
    if not name_without_prefix:
        name_without_prefix = source_name

    if " - " in name_without_prefix:
        _, suffix = name_without_prefix.split(" - ", 1)
        if suffix.strip():
            return f"{prefix} - {suffix.strip()}"
        return prefix

    if " " in name_without_prefix:
        _, suffix = name_without_prefix.split(" ", 1)
        if suffix.strip():
            return f"{prefix} - {suffix.strip()}"

    return f"{prefix} - {name_without_prefix}"


def _resolve_target_project_name_from_lookup(
    datasource: DatasourceInstance,
    project_id: str,
    source_project_name: str,
    target_prefix: str,
    fallback_name: str | None = None,
) -> str:
    """Look up the actual target project using the request source project plus the configured prefix.

    This avoids constructing names like 'T01 -' when the source project name is empty or the source
    value is only a project prefix fragment. We resolve against the real rows in
    arms.t_mig_lu_tgt_project and fall back to the derived name only when no row matches.
    """
    source_name = str(source_project_name or fallback_name or "").strip()
    prefix = str(target_prefix).strip()

    candidate_names: list[str] = []
    if prefix:
        candidate_names.append(prefix)
        if source_name:
            name_without_prefix = _strip_prefix_from_project_name(source_name, prefix)
            if not name_without_prefix:
                name_without_prefix = source_name

            if " - " in name_without_prefix:
                _, suffix = name_without_prefix.split(" - ", 1)
                if suffix.strip():
                    candidate_names.append(f"{prefix} - {suffix.strip()}")
            if name_without_prefix:
                candidate_names.append(f"{prefix} - {name_without_prefix}")
                candidate_names.append(f"{prefix}-{name_without_prefix}")
                candidate_names.append(name_without_prefix)
            candidate_names.append(source_name)
    elif source_name:
        candidate_names.append(source_name)

    cleaned_names = []
    for name in candidate_names:
        cleaned = str(name).strip()
        if cleaned:
            cleaned = cleaned.rstrip("- ")
            if cleaned:
                cleaned_names.append(cleaned)
    unique_names = list(dict.fromkeys(cleaned_names))

    if unique_names:
        escaped_values = [value.replace("'", "''") for value in unique_names]
        value_sql = ", ".join(f"'{value}'" for value in escaped_values)
        query = (
            f"SELECT tgt_project_name FROM {TARGET_PROJECT_TABLE} "
            f"WHERE LOWER(tgt_project_name) IN ({value_sql}) "
            f"OR LOWER(tgt_project_name) LIKE LOWER('{prefix}%') "
            f"OR LOWER(tgt_project_name) LIKE LOWER('%{source_name.lower().replace("'", "''")}%') "
            "ORDER BY tgt_project_name ASC;"
        )
        result = datasource.execute_query(project_id=project_id, query=query)
        rows = _rows_from_result(result)
        for row in rows:
            project_name = _get_first_value_from_result_data(
                row,
                "tgt_project_name",
                "target_project_name",
                "project_name",
            )
            if project_name:
                cleaned = str(project_name).strip().rstrip("-").strip()
                if cleaned:
                    return cleaned

    derived_name = _derive_target_project_name(source_name, prefix, fallback_name)
    cleaned_derived = str(derived_name).strip().rstrip("-").strip()
    if cleaned_derived:
        return cleaned_derived
    return source_name or str(fallback_name or prefix).strip()


def _resolve_target_project_name_from_sql(
    datasource: DatasourceInstance,
    project_id: str,
    target_env_id: str | None,
    request_status_id: str | int | None,
    source_project_name: str,
    target_prefix: str,
    fallback_name: str | None = None,
    target_project_id: str | int | None = None,
) -> str:
    """Resolve the target project using the request status, target/source environment, and prefix.

    The target-project lookup should be narrowed to the relevant status/environment mapping before
    falling back to a name-derived guess. This avoids returning unrelated rows like the source
    project's project name when the proper target project is known by prefix and environment.
    """
    env_id = str(target_env_id).strip() if target_env_id is not None else ""
    status_id = str(request_status_id).strip() if request_status_id is not None else ""
    source_name = str(source_project_name or fallback_name or "").strip()
    prefix = str(target_prefix).strip()
    target_project_id_text = str(target_project_id).strip() if target_project_id is not None else ""
    escaped_env_id = env_id.replace("'", "''")
    escaped_source_name = source_name.replace("'", "''")
    escaped_prefix = prefix.replace("'", "''")

    if target_project_id_text:
        query = (
            f"SELECT * FROM {TARGET_PROJECT_TABLE} "
            f"WHERE CAST(tgt_project_id AS VARCHAR) = '{target_project_id_text}' "
            "ORDER BY status_id DESC, tgt_project_name ASC;"
        )
        result = datasource.execute_query(project_id=project_id, query=query)
        for row in _rows_from_result(result):
            project_name = _get_first_value_from_result_data(
                row,
                "tgt_project_name",
                "target_project_name",
                "project_name",
            )
            if project_name:
                cleaned = str(project_name).strip().rstrip("-").strip()
                if cleaned:
                    return cleaned

    conditions: list[str] = []
    if status_id:
        conditions.append(f"status_id = {status_id}")
    if env_id:
        conditions.append(f"CAST(tgt_env_id AS VARCHAR) = '{escaped_env_id}'")

    query = f"SELECT * FROM {TARGET_PROJECT_TABLE}"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY tgt_project_name ASC;"

    result = datasource.execute_query(project_id=project_id, query=query)
    exact_matches: list[dict[str, object]] = []
    fallback_matches: list[dict[str, object]] = []
    for row in _rows_from_result(result):
        project_name = _get_first_value_from_result_data(
            row,
            "tgt_project_name",
            "target_project_name",
            "project_name",
        )
        if not project_name:
            continue

        cleaned = str(project_name).strip().rstrip("-").strip()
        if not cleaned:
            continue

        row_target_env = _get_first_value_from_result_data(
            row,
            "tgt_env_name",
            "target_env_name",
            "env_name",
            "environment_name",
            "tgt_environment_name",
            "target_environment",
            "environment",
        ) or ""
        row_source_env = _get_first_value_from_result_data(
            row,
            "source_env_name",
            "source_environment",
            "src_env_name",
            "src_environment",
            "source_env",
            "project_source_env",
        ) or ""
        row_prefix = _get_first_value_from_result_data(
            row,
            "project_prefix",
            "tgt_project_prefix",
            "target_project_prefix",
            "prefix",
        ) or ""

        env_matches = True
        if env_id:
            row_env_id = _get_first_value_from_result_data(
                row,
                "tgt_env_id",
                "target_env_id",
                "env_id",
                "environment_id",
                "tgt_environment_id",
                "target_environment_id",
            ) or ""
            env_matches = env_matches and (
                str(row_env_id).strip() == env_id
                or str(row_target_env).strip().lower() == str(target_env_id).strip().lower()
            )

        source_match = True
        if source_project_name:
            row_source_name = _get_first_value_from_result_data(
                row,
                "source_project_name",
                "source_project",
                "project_name",
                "src_project_name",
            ) or ""
            source_match = (
                str(row_source_name).strip().lower() == source_name.lower()
                or source_name.lower() in str(cleaned).lower()
                or str(cleaned).lower() in source_name.lower()
            )

        prefix_match = True
        if prefix:
            prefix_match = (
                str(row_prefix).strip().lower() == prefix.lower()
                or str(cleaned).lower().startswith(prefix.lower())
                or str(cleaned).lower().startswith(f"{prefix.lower()} -")
            )

        source_env_match = True
        source_env_name = str(SOURCE_ENVIRONMENT).strip().lower()
        if source_env_name:
            source_env_match = (
                not row_source_env
                or str(row_source_env).strip().lower() == source_env_name
                or str(row_source_env).strip().lower() in {source_env_name, source_name.lower()}
            )

        if env_matches and prefix_match and source_env_match:
            exact_matches.append(row)
            continue

        if env_matches and source_match and source_env_match:
            fallback_matches.append(row)

    for row in exact_matches:
        project_name = _get_first_value_from_result_data(
            row,
            "tgt_project_name",
            "target_project_name",
            "project_name",
        )
        if project_name:
            cleaned = str(project_name).strip().rstrip("-").strip()
            if cleaned:
                return cleaned

    for row in fallback_matches:
        project_name = _get_first_value_from_result_data(
            row,
            "tgt_project_name",
            "target_project_name",
            "project_name",
        )
        if project_name:
            cleaned = str(project_name).strip().rstrip("-").strip()
            if cleaned:
                return cleaned

    return _resolve_target_project_name_from_lookup(
        datasource,
        project_id,
        source_name,
        prefix,
        fallback_name,
    )


def _get_target_project_status_id(
    datasource: DatasourceInstance,
    target_project_name: str,
    project_id: str,
) -> int:
    """Return the status_id for the target project from the ARMS lookup table."""
    escaped_name = target_project_name.replace("'", "''")
    query = (
        f"SELECT status_id FROM {TARGET_PROJECT_TABLE} "
        f"WHERE tgt_project_name = '{escaped_name}' "
        "ORDER BY status_id DESC"
    )
    result = datasource.execute_query(project_id=project_id, query=query)
    data = result.get("results", {}).get("data", {})
    values = data.get("status_id", [])
    if not values:
        raise ValueError(
            f"No status_id found in {TARGET_PROJECT_TABLE} for target project '{target_project_name}'."
        )
    return _coerce_int(values[0])


def _get_failed_status_id_for_environment(
    datasource: DatasourceInstance,
    project_id: str,
    target_env_id: str,
    target_env_name: str,
) -> int:
    """Select the failed status row matching the target environment from the status lookup table."""
    query = f"SELECT * FROM {STATUS_TABLE};"
    result = datasource.execute_query(project_id=project_id, query=query)
    env_key = str(target_env_id).strip().lower()
    env_name_key = str(target_env_name).strip().lower()

    for row in _rows_from_result(result):
        status_id = _get_first_value_from_result_data(row, "status_id")
        status_name = _get_first_value_from_result_data(row, "status_name", "status", "name") or ""
        status_desc = _get_first_value_from_result_data(row, "status_desc", "description") or ""
        row_env_id = _get_first_value_from_result_data(
            row,
            "tgt_env_id",
            "target_env_id",
            "env_id",
            "environment_id",
            "tgt_environment_id",
            "target_environment_id",
        )
        combined = f"{status_name} {status_desc} {row_env_id or ''} {target_env_name or ''}".lower()
        if "failed" not in combined:
            continue
        if row_env_id and str(row_env_id).strip().lower() == env_key:
            return _coerce_int(status_id)
        if env_name_key and env_name_key in combined:
            return _coerce_int(status_id)

    generic_query = (
        f"SELECT status_id FROM {STATUS_TABLE} "
        "WHERE LOWER(status_name) LIKE '%failed%' OR LOWER(status_desc) LIKE '%failed%' "
        "ORDER BY status_id ASC"
    )
    generic_result = datasource.execute_query(project_id=project_id, query=generic_query)
    values = generic_result.get("results", {}).get("data", {}).get("status_id", [])
    if not values:
        raise ValueError(
            "No failed status row found in the status lookup table for the target environment."
        )
    return _coerce_int(values[0])


def _sql_literal(value: object) -> str:
    """Escape SQL string values for safe insertion into ARMS tables."""
    if value is None:
        return "NULL"
    text = str(value).replace("'", "''")
    return f"'{text}'"


def _resolve_arms_project_id(
    datasource: DatasourceInstance,
    query_project_id: str,
    project_guid: str | None = None,
    project_name: str | None = None,
) -> int | None:
    """Resolve the numeric ARMS project key for a MicroStrategy project GUID or name."""
    if not project_guid and not project_name:
        return None

    clauses = []
    if project_guid:
        escaped_guid = str(project_guid).replace("'", "''")
        clauses.append(f"project_guid = '{escaped_guid}'")
    if project_name:
        escaped_name = str(project_name).strip().replace("'", "''")
        clauses.append(f"LOWER(project_name) = '{escaped_name.lower()}'")

    query = (
        f"SELECT project_id FROM {PROJECT_LOOKUP_TABLE} "
        f"WHERE {' OR '.join(clauses)} ORDER BY project_id ASC LIMIT 1;"
    )
    result = datasource.execute_query(project_id=query_project_id, query=query)
    rows = _rows_from_result(result)
    for row in rows:
        value = _get_first_value_from_result_data(row, "project_id", "id")
        if value is None:
            continue
        return _coerce_int(value)
    return None


def _insert_migration_dim_record(
    datasource: DatasourceInstance,
    project_id: str,
    request_id: object,
    source_project_name: str,
    target_project_name: str,
    target_env_name: str,
    migration_obj_guid: str,
    migration_id: str,
    migration_name: str,
    status_id: int | None = None,
    arms_project_id: int | str | None = None,
) -> None:
    """Insert a migration record using the actual ARMS migration table schema.

    The live schema exposes columns such as migration_key_id, request_id,
    migration_time, project_id, and status_id. The value written into the
    ARMS table must be the numeric project key from arms.t_mig_lu_project,
    not the MicroStrategy project GUID used for datasource queries.
    """
    columns = [
        "migration_id",
        "migration_key_id",
        "request_id",
        "migration_time",
        "project_id",
    ]

    project_key_value = "NULL"
    if arms_project_id is not None:
        try:
            project_key_value = str(_coerce_int(arms_project_id))
        except (TypeError, ValueError):
            project_key_value = "NULL"

    values = [
        "(SELECT COALESCE(MAX(migration_id), 0) + 1 FROM arms.t_mig_dim_migration)",
        f"(SELECT migration_key_id FROM {REQUEST_TABLE} WHERE request_id = {request_id})",
        str(request_id),
        "CURRENT_TIMESTAMP",
        project_key_value,
    ]

    if status_id is not None:
        columns.append("status_id")
        values.append(str(status_id))

    query = (
        "INSERT INTO arms.t_mig_dim_migration ("
        + ", ".join(columns)
        + ") VALUES ("
        + ", ".join(values)
        + ");"
    )
    datasource.execute_query(project_id=project_id, query=query)


def _mark_failure_status(
    datasource: DatasourceInstance,
    project_id: str,
    request_id: object,
    migration_obj_guid: str,
    target_env_id: str,
    target_env_name: str,
) -> int:
    """Set the failed status on both the migration record and request row for this environment."""
    failed_status_id = _get_failed_status_id_for_environment(
        datasource,
        project_id,
        target_env_id,
        target_env_name,
    )

    datasource.execute_query(
        project_id=project_id,
        query=(
            f"UPDATE arms.t_mig_dim_request SET status_id={failed_status_id} "
            f"WHERE request_id={request_id};"
        ),
    )
    datasource.execute_query(
        project_id=project_id,
        query=(
            f"UPDATE arms.t_mig_dim_migration SET status_id={failed_status_id} "
            f"WHERE request_id={request_id};"
        ),
    )
    return failed_status_id


def _insert_comment(
    datasource: DatasourceInstance,
    project_id: str,
    request_id: object,
    comment_text: str,
    author: str = "system",
) -> None:
    """Insert a new comment for the request using the ARMS comments table."""
    escaped_comment = comment_text.replace("'", "''")
    query = (
        "INSERT INTO arms.t_mig_comments(comment_id,request_id,comment_text,comment_time,comment_author,human_flag) "
        "VALUES ((SELECT COALESCE(MAX(comment_id), 0) + 1 FROM arms.t_mig_comments), "
        f"{request_id}, '{escaped_comment}', CURRENT_TIMESTAMP, '{author}', 0);"
    )
    datasource.execute_query(project_id=project_id, query=query)


def _wait_for_migration_completion(migration: Migration, poll_seconds: int = 5) -> ImportStatus | str | None:
    """Poll the migration import status until it reaches a terminal state."""
    start_time = datetime.now()
    last_status = None

    while True:
        try:
            migration.fetch()
        except Exception:
            pass

        import_status = getattr(getattr(migration, "import_info", None), "status", None)
        status_name = getattr(import_status, "name", str(import_status))

        if last_status != status_name:
            print(
                f"Migration status: {status_name} at "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            last_status = status_name

        if import_status in (
            ImportStatus.IMPORTED,
            ImportStatus.IMPORT_FAILED,
            ImportStatus.UNDO_SUCCESS,
            ImportStatus.UNDO_FAILED,
        ):
            break

        sleep(poll_seconds)

    end_time = datetime.now()
    duration_seconds = (end_time - start_time).total_seconds()
    print(
        f"Migration completed at {end_time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"after {duration_seconds:.1f}s with status '{status_name}'."
    )
    return import_status


def _is_package_locked_error(exc: BaseException) -> bool:
    """Return True when Strategy indicates the migration package is currently locked."""
    message = str(exc).lower()
    return "packageinfo.status" in message and "locked" in message


def _wait_for_package_unlock(
    migration: Migration,
    poll_seconds: int = 5,
    timeout_seconds: int = 180,
) -> PackageStatus | str | None:
    """Wait until the migration object is no longer locked or the timeout is reached."""
    deadline_epoch = datetime.now().timestamp() + timeout_seconds
    last_status = None

    while True:
        try:
            migration.fetch()
        except Exception:
            pass

        package_status = getattr(getattr(migration, "package_info", None), "status", None)
        status_name = getattr(package_status, "name", str(package_status or "unknown"))

        if last_status != status_name:
            print(
                f"Package status: {status_name} at "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            last_status = status_name

        if package_status is None or str(status_name).lower() != "locked":
            return package_status

        if datetime.now().timestamp() >= deadline_epoch:
            raise TimeoutError(
                "Migration package remained locked for more than "
                f"{timeout_seconds} seconds and could not be imported. "
                "Check for a concurrent migration job or unlock the package before retrying."
            )

        sleep(poll_seconds)


def _list_active_migration_jobs(
    connection: Connection,
    migration_obj_guid: str,
    *,
    limit: int = 20,
) -> list[dict[str, object]]:
    """List any current or recent jobs associated with the migration object.

    This is a read-only inspection step before the retry/fail workflow continues. It
    does not attempt to manipulate or unlock the package; it only reports the job state
    so operators can decide whether to wait, cancel, or continue with the failure path.
    """
    migration_guid = str(migration_obj_guid or "").strip()
    if not migration_guid:
        return []

    try:
        jobs = list_jobs(connection=connection, object_id=migration_guid, limit=limit)
    except Exception as exc:  # pragma: no cover - depends on server response
        print(f"Unable to list migration jobs for object '{migration_guid}': {exc}")
        return []

    rows: list[dict[str, object]] = []
    for job in jobs or []:
        if job is None:
            continue
        status_value = getattr(job, "status", None)
        status_name = getattr(status_value, "name", str(status_value)) if status_value is not None else "unknown"
        rows.append(
            {
                "job_id": getattr(job, "id", None),
                "status": str(status_name),
                "description": getattr(job, "description", None),
                "user": getattr(job, "user", None),
                "object_id": getattr(job, "object_id", None),
                "error_message": getattr(job, "error_message", None),
            }
        )

    if not rows:
        print(f"No current migration jobs found for object '{migration_guid}'.")
        return rows

    print(f"Current migration jobs for object '{migration_guid}':")
    for row in rows:
        print(
            "  - "
            f"Job {row.get('job_id')}: status={row.get('status')}, "
            f"description={row.get('description') or '-'}, user={row.get('user') or '-'}"
        )
    return rows


def _migrate_with_lock_retry(
    migration: Migration,
    *,
    target_env,
    target_project_name: str,
    generate_undo: bool = True,
    poll_seconds: int = 5,
    max_attempts: int = 1,
):
    """Attempt the migration without deleting the migration object before import.

    The project policy is intentionally conservative: if a migration object is already locked,
    the script does not delete it before a migration attempt. If FORCE_UNLOCK_LOCKED_PACKAGE is
    set, we log the condition but still do not destructively clear the object because the object
    ID may be unavailable or the migration may not yet exist in the target environment.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return migration.migrate(
                target_env=target_env,
                target_project_name=target_project_name,
                generate_undo=generate_undo,
            )
        except Exception as exc:  # pragma: no cover - lock path is exercised in tests
            if not _is_package_locked_error(exc):
                raise

            if FORCE_UNLOCK_LOCKED_PACKAGE:
                print(
                    f"Package '{getattr(migration, 'name', getattr(migration, 'id', 'unknown'))}' is locked; "
                    "FORCE_UNLOCK_LOCKED_PACKAGE is enabled, but this script will not delete the migration object "
                    "before import. The migration is left untouched and the workflow stops safely."
                )

            print(
                f"Package '{getattr(migration, 'name', getattr(migration, 'id', 'unknown'))}' is locked; "
                "going directly to the failed scenario without waiting for unlock."
            )
            raise

    raise RuntimeError("Migration retry loop exited without executing the package import.")


def main() -> int:
    print("Connecting to environment...")

    my_conn = Connection(
        BASE_URL,
        MSTR_USERNAME,
        MSTR_PASSWORD,
        project_id=PROJECT_ID,
    )
    db_instance = DatasourceInstance(connection=my_conn, id=DATASOURCE_INSTANCE_ID)

    request_rows = _fetch_requests_for_statuses(db_instance, PROJECT_ID, REQUEST_STATUS_FILTER)
    if not request_rows:
        raise ValueError(
            f"No requests found in {REQUEST_TABLE} with status_id in {REQUEST_STATUS_FILTER}."
        )

    request_row = request_rows[0]
    request_id = _get_first_value_from_result_data(
        request_row,
        "request_id",
        "Request@ID",
        "request",
    )
    print(f"Retrieved request number: {request_id}")
    migration_obj_guid = _resolve_migration_object_guid(db_instance, PROJECT_ID, request_row) or ""

    request_report = None
    report_source_project_name = ""
    report_target_project_name = ""
    try:
        request_report = Report(connection=my_conn, id=REQUEST_REPORT_ID)
        df = request_report.to_dataframe()
        if not df.empty:
            report_row = df.iloc[0].to_dict()
            report_source_project_name = _get_first_value_from_result_data(
                report_row,
                "source_project_name",
                "source_project",
                "project_name",
                "Project@Name",
                "Project@NAME",
                "project",
            ) or ""
            report_target_project_name = _get_first_value_from_result_data(
                report_row,
                "target_project_name",
                "Target Project@NAME",
                "Target Project@NAME",
                "target_project",
            ) or ""
    except Exception:
        report_source_project_name = ""
        report_target_project_name = ""

    source_project_name = (
        report_source_project_name
        or _get_first_value_from_result_data(
            request_row,
            "source_project_name",
            "project_name",
            "Project@Name",
            "Project@NAME",
            "source_project",
            "project",
        )
        or ""
    )
    request_status_id = _get_first_value_from_result_data(
        request_row,
        "status_id",
        "Status@ID",
        "status",
    )
    target_project_name_from_request = (
        report_target_project_name
        or _get_first_value_from_result_data(
            request_row,
            "target_project_name",
            "Target Project@NAME",
            "target_project",
        )
        or ""
    )
    if not source_project_name and target_project_name_from_request:
        source_project_name = target_project_name_from_request

    if not request_id:
        raise ValueError(f"First matching request row does not contain a request_id.")
    if not migration_obj_guid:
        raise ValueError(
            f"Request {request_id} has no migration object GUID in {MIGRATION_OBJECT_TABLE}."
        )

    target_env_name = SOURCE_ENVIRONMENT
    target_env_url = BASE_URL
    target_env_id = None

    if request_status_id is None or str(request_status_id).strip() == "":
        target_project_name = _get_first_value_from_result_data(
            request_row,
            "target_project_name",
            "Target Project@NAME",
            "target_project",
            "target_project_name",
        )
        if not target_project_name:
            raise ValueError(
                f"Request {request_id} does not contain a status mapping or a target project name."
            )
    else:
        target_env_id = _get_status_target_env_id(db_instance, request_status_id, PROJECT_ID)
        if target_env_id:
            target_env_details = _get_target_environment_details(db_instance, target_env_id, PROJECT_ID)
            target_env_name = target_env_details.get("env_name", str(target_env_id).strip())
            target_env_url = target_env_details.get("url") or _resolve_target_environment_url(
                BASE_URL,
                SOURCE_ENVIRONMENT,
                target_env_name,
                ENVIRONMENT_URLS,
            )
            target_project_prefix = target_env_details.get("prefix", "")
            target_project_name = _resolve_target_project_name_from_sql(
                db_instance,
                PROJECT_ID,
                target_env_id,
                request_status_id,
                source_project_name,
                target_project_prefix,
                target_project_name_from_request,
                request_row.get("tgt_project_id") or request_row.get("target_project_id"),
            )
        else:
            target_project_name = _get_first_value_from_result_data(
                request_row,
                "target_project_name",
                "Target Project@NAME",
                "target_project",
                "target_project_name",
            ) or source_project_name
            target_env_name = SOURCE_ENVIRONMENT
            target_env_url = _resolve_target_environment_url(
                BASE_URL,
                SOURCE_ENVIRONMENT,
                target_env_name,
                ENVIRONMENT_URLS,
            )

    print(f"Resolved target project name: {target_project_name}")
    print(
        "Retrieved migration parameters:\n"
        f"  Request ID: {request_id}\n"
        f"  Source project: {source_project_name}\n"
        f"  Target project: {target_project_name}\n"
        f"  Target environment: {target_env_name}\n"
        f"  Target URL: {target_env_url}\n"
        f"  Migration object GUID: {migration_obj_guid}\n"
        f"  Request status ID: {request_status_id}\n"
        f"  Base URL: {BASE_URL}\n"
        f"  Source environment: {SOURCE_ENVIRONMENT}\n"
        f"  Project ID: {PROJECT_ID}\n"
        f"  Datasource instance ID: {DATASOURCE_INSTANCE_ID}"
    )

    target_env = None
    try:
        target_env = Connection(
            target_env_url,
            MSTR_USERNAME,
            MSTR_PASSWORD,
            project_name=target_project_name,
        )

        current_migration = Migration(my_conn, id=migration_obj_guid)
        _list_active_migration_jobs(my_conn, migration_obj_guid, limit=20)
        print("Starting package import...")

        try:
            migration_start_time = datetime.now()
            print(
                f"Starting migration at {migration_start_time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"with undo generation enabled."
            )
            import_result = _migrate_with_lock_retry(
                current_migration,
                target_env=target_env,
                target_project_name=target_project_name,
                generate_undo=True,
                poll_seconds=5,
                max_attempts=1,
            )
            import_status = _wait_for_migration_completion(current_migration)
            print(f"Migration import status final value: {getattr(import_status, 'name', str(import_status))}")
        except Exception as exc:
            failed_status_id = _mark_failure_status(
                datasource=db_instance,
                project_id=PROJECT_ID,
                request_id=request_id,
                migration_obj_guid=migration_obj_guid,
                target_env_id=str(target_env_id).strip() if target_env_id is not None else str(target_env_name).strip(),
                target_env_name=target_env_name,
            )
            error_message = f"Migration failed for request {request_id} on target environment '{target_env_name}': {exc}"
            _insert_comment(
                datasource=db_instance,
                project_id=PROJECT_ID,
                request_id=request_id,
                comment_text=error_message,
            )
            print(
                f"Migration failed for request {request_id}; set request and migration tables to "
                f"status_id={failed_status_id} for environment '{target_env_name}'."
            )
            print(error_message)
            raise

        print("Import result:")
        print(import_result)

        migration_name = getattr(current_migration, "name", None) or str(migration_obj_guid)
        target_status_id = _get_target_project_status_id(db_instance, target_project_name, PROJECT_ID)
        arms_project_id = _resolve_arms_project_id(
            db_instance,
            PROJECT_ID,
            project_guid=PROJECT_ID,
            project_name=source_project_name or target_project_name,
        )
        _insert_migration_dim_record(
            datasource=db_instance,
            project_id=PROJECT_ID,
            request_id=request_id,
            source_project_name=source_project_name,
            target_project_name=target_project_name,
            target_env_name=target_env_name,
            migration_obj_guid=migration_obj_guid,
            migration_id=str(current_migration.id),
            migration_name=str(migration_name),
            status_id=target_status_id,
            arms_project_id=arms_project_id,
        )
        print(
            f"Inserted migration record for request {request_id} into arms.t_mig_dim_migration."
        )
        _insert_comment(
            datasource=db_instance,
            project_id=PROJECT_ID,
            request_id=request_id,
            comment_text=(
                f"Migration completed successfully for request {request_id}. "
                f"Imported into target project '{target_project_name}' on environment '{target_env_name}'."
            ),
        )

        print(
            f"Updating request {request_id} status to target status_id {target_status_id} "
            f"for target project '{target_project_name}'."
        )
        db_instance.execute_query(
            query=f"UPDATE arms.t_mig_dim_request SET status_id={target_status_id} WHERE request_id={request_id};",
            project_id=PROJECT_ID,
        )
        return 0
    finally:
        if target_env is not None:
            target_env.close()
        my_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
