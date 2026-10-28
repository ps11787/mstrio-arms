"""Import a prebuilt MicroStrategy migration package.

See [docs/import_package.md](docs/import_package.md) for the full workflow and
error-handling documentation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from time import sleep

import mstrio.api.migration as migration_api
from mstrio.connection import Connection
from mstrio.datasources.datasource_instance import DatasourceInstance
from mstrio.object_management.migration import Migration
from mstrio.object_management.migration.package import ImportStatus, PackageStatus, ValidationStatus
from mstrio.server.job_monitor import list_jobs

try:
    import config_local as _local_config
except ImportError:
    _local_config = None


BASE_URL = "https://env-371825.ma.cloud.microstrategy.com/MicroStrategyLibrary"
SOURCE_ENVIRONMENT = "dev"
# Target environment URLs keyed by tgt_env_id; empty values are ignored.
TEST_URL = ""
QA_URL = ""
PROD_URL = ""
ENVIRONMENT_URLS = {
    key: value.strip()
    for key, value in {"dev": BASE_URL, "test": TEST_URL, "qa": QA_URL, "prod": PROD_URL}.items()
    if value and value.strip()
}
MSTR_USERNAME = getattr(_local_config, "MSTR_USERNAME", "mstr")
MSTR_PASSWORD = getattr(_local_config, "MSTR_PASSWORD", "")
PROJECT_ID = "4B0544627D49F8DA03C26D817D208263"
DATASOURCE_INSTANCE_ID = "A23BBC514D336D5B4FCE919FE19661A3"
TARGET_PROJECT_TABLE = "arms.t_mig_lu_tgt_project"
STATUS_TABLE = "arms.t_mig_lu_status"
TARGET_ENV_TABLE = "arms.t_mig_lu_tgt_env"
REQUEST_TABLE = "arms.t_mig_dim_request"
PROJECT_LOOKUP_TABLE = "arms.t_mig_lu_project"
MIGRATION_OBJECT_TABLE = "arms.t_mig_lu_migration_obj"
TARGET_RESOLUTION_FAILED_STATUS_ID = 11
# Require an unambiguous status/environment mapping; disable to allow legacy fallbacks.
FORCE_STATUS_TARGET_PROJECT_RESOLUTION = True
REQUEST_STATUS_FILTER = (5, 6, 7)
FORCE_UNLOCK_LOCKED_PACKAGE = True
POST_IMPORT_POLL_SECONDS = 5
POST_IMPORT_TIMEOUT_SECONDS = 600
REQUIRE_UNLOCKED_PACKAGE_AFTER_IMPORT = False
REQUIRE_UNDO_REQUESTED_AFTER_IMPORT = True


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
    text = str(value).strip()
    if not text:
        raise ValueError("Cannot coerce empty string to int.")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid integer value: {value!r}.") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError(f"Expected a finite whole number; got {value!r}.")
    return int(number)


def _execute_query(datasource: DatasourceInstance, *, project_id: str, query: str) -> dict:
    print(f"Executing SQL:\n{query}", flush=True)
    return datasource.execute_query(project_id=project_id, query=query)


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
        f"SELECT r.*, p.project_name AS source_project_name FROM {REQUEST_TABLE} r "
        f"LEFT JOIN {PROJECT_LOOKUP_TABLE} p ON r.project_id = p.project_id "
        f"WHERE r.status_id IN ({status_in_list}) "
        "ORDER BY r.request_id DESC;"
    )
    result = _execute_query(datasource, project_id=project_id, query=query)
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
    result = _execute_query(datasource, project_id=project_id, query=query)
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


def _select_request_with_migration(
    request_rows: list[dict[str, object]],
    datasource: DatasourceInstance,
    project_id: str,
) -> tuple[dict[str, object], str] | None:
    for request_row in request_rows:
        request_id = _get_first_value_from_result_data(
            request_row, "request_id", "Request@ID", "request"
        )
        if not request_id:
            print("Skipping request row without a request_id.")
            continue
        migration_guid = _resolve_migration_object_guid(datasource, project_id, request_row)
        if migration_guid:
            return request_row, migration_guid
        print(
            f"Skipping request {request_id}: no migration object GUID in "
            f"{MIGRATION_OBJECT_TABLE}. Create its package before importing."
        )
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
    result = _execute_query(datasource, project_id=project_id, query=query)
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
    result = _execute_query(datasource, project_id=project_id, query=query)
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
                "status_id_success": _get_first_value_from_result_data(row, "status_id_success") or "",
                "status_id_failed": _get_first_value_from_result_data(row, "status_id_failed") or "",
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
        result = _execute_query(datasource, project_id=project_id, query=query)
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
    *,
    strict_status: bool = False,
) -> str:
    """Resolve the target project using the request status, target/source environment, and prefix.

    The target-project lookup should be narrowed to the relevant status/environment mapping before
    falling back to a name-derived guess. This avoids returning unrelated rows like the source
    project's project name when the proper target project is known by prefix and environment.
    With strict_status enabled, require one distinct name matching the status,
    environment, and environment prefix; ignore request IDs and name-derived fallbacks.
    """
    env_id = str(target_env_id).strip() if target_env_id is not None else ""
    status_id = str(request_status_id).strip() if request_status_id is not None else ""
    source_name = str(source_project_name or fallback_name or "").strip()
    prefix = str(target_prefix).strip()
    target_project_id_text = str(target_project_id).strip() if target_project_id is not None else ""
    escaped_env_id = env_id.replace("'", "''")
    escaped_source_name = source_name.replace("'", "''")
    escaped_prefix = prefix.replace("'", "''")

    if strict_status and (not status_id or not env_id):
        raise ValueError("Strict status resolution requires a request status and target environment mapping.")
    if strict_status and not prefix:
        raise ValueError(
            f"Strict status resolution requires a target project prefix in {TARGET_ENV_TABLE} "
            f"for tgt_env_id={env_id}."
        )
    source_suffix = str(source_project_name or "").strip()
    if strict_status:
        if not source_suffix:
            raise ValueError("Strict status resolution requires the source project name.")
        if " - " in source_suffix:
            _, source_suffix = source_suffix.split(" - ", 1)
            source_suffix = source_suffix.strip()
        if not source_suffix:
            raise ValueError("Strict status resolution requires a non-empty source project suffix.")

    if target_project_id_text and not strict_status:
        query = (
            f"SELECT * FROM {TARGET_PROJECT_TABLE} "
            f"WHERE CAST(tgt_project_id AS VARCHAR) = '{target_project_id_text}' "
            "ORDER BY status_id DESC, tgt_project_name ASC;"
        )
        result = _execute_query(datasource, project_id=project_id, query=query)
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

    result = _execute_query(datasource, project_id=project_id, query=query)
    if strict_status:
        project_names: set[str] = set()
        for row in _rows_from_result(result):
            project_name = _get_first_value_from_result_data(
                row, "tgt_project_name", "target_project_name", "project_name"
            )
            if project_name:
                cleaned = project_name.strip().rstrip("-").strip()
                prefix_matches = cleaned.casefold().startswith(prefix.casefold())
                remainder = cleaned[len(prefix):] if prefix_matches else ""
                boundary_matches = (
                    not remainder
                    or prefix.endswith(("-", "_", "/"))
                    or remainder[0].isspace()
                    or remainder[0] in "-_/"
                )
                target_suffix = remainder.lstrip(" -_/").strip()
                if (
                    cleaned and prefix_matches and boundary_matches
                    and target_suffix.casefold() == source_suffix.casefold()
                ):
                    project_names.add(cleaned)
        if len(project_names) != 1:
            raise ValueError(
                f"Strict status resolution requires exactly one target project in {TARGET_PROJECT_TABLE} "
                f"for status_id={status_id}, tgt_env_id={env_id}, prefix={prefix!r}, "
                f"source suffix={source_suffix!r}; "
                f"found {len(project_names)}: {sorted(project_names)}."
            )
        return next(iter(project_names))

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


def _get_result_status_id_for_environment(
    datasource: DatasourceInstance,
    project_id: str,
    target_env_id: str,
    *,
    succeeded: bool,
) -> int:
    """Read the explicit result status configured for the selected target environment."""
    column = "status_id_success" if succeeded else "status_id_failed"
    details = _get_target_environment_details(datasource, target_env_id, project_id)
    value = details.get(column, "")
    try:
        return _coerce_int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Missing or invalid {column} in {TARGET_ENV_TABLE} "
            f"for tgt_env_id={target_env_id}: {value!r}."
        ) from exc


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
    result = _execute_query(datasource, project_id=query_project_id, query=query)
    rows = _rows_from_result(result)
    for row in rows:
        value = _get_first_value_from_result_data(row, "project_id", "id")
        if value is None:
            continue
        return _coerce_int(value)
    return None


def _resolve_target_arms_project_id(
    datasource: DatasourceInstance,
    query_project_id: str,
    target_project_name: str,
    *,
    target_project_table: str | None = None,
) -> int:
    """Resolve the target lookup's numeric key for the actual import destination."""
    table = TARGET_PROJECT_TABLE if target_project_table is None else target_project_table
    name = target_project_name.strip()
    if not name:
        raise ValueError("Target project name is required to resolve tgt_project_id.")
    escaped_name = name.replace("'", "''")
    query = (
        f"SELECT tgt_project_id FROM {table} "
        f"WHERE LOWER(TRIM(tgt_project_name)) = LOWER('{escaped_name}');"
    )
    result = _execute_query(datasource, project_id=query_project_id, query=query)
    project_ids: set[int] = set()
    for row in _rows_from_result(result):
        value = _get_first_value_from_result_data(row, "tgt_project_id")
        try:
            project_ids.add(_coerce_int(value))
        except ValueError as exc:
            raise ValueError(
                f"Invalid tgt_project_id {value!r} in {table} "
                f"for target project '{name}'."
            ) from exc
    if len(project_ids) != 1:
        raise ValueError(
            f"Expected exactly one tgt_project_id in {table} "
            f"for target project '{name}'; found {len(project_ids)}: {sorted(project_ids)}."
        )
    return next(iter(project_ids))


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
    migration_message: str | None = None,
) -> None:
    """Insert a migration record using the actual ARMS migration table schema.

    The live schema exposes columns such as migration_key_id, request_id,
    migration_time, project_id, and status_id. The value written into the
    ARMS table must be tgt_project_id from the configured target-project lookup,
    not the MicroStrategy project GUID used for datasource queries.
    """
    columns = [
        "migration_id",
        "migration_key_id",
        "request_id",
        "migration_time",
        "project_id",
    ]

    try:
        project_key_value = str(_coerce_int(arms_project_id))
    except ValueError as exc:
        raise ValueError(
            f"A numeric tgt_project_id is required for migration history; got {arms_project_id!r}."
        ) from exc

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

    if migration_message is not None:
        columns.append("migration_message")
        values.append(_sql_literal(migration_message))

    query = (
        "INSERT INTO arms.t_mig_dim_migration ("
        + ", ".join(columns)
        + ") VALUES ("
        + ", ".join(values)
        + ");"
    )
    _execute_query(datasource, project_id=project_id, query=query)


def _insert_comment(
    datasource: DatasourceInstance,
    project_id: str,
    request_id: object,
    comment_text: str,
    author: str = "system",
    *,
    comment_table: str = "arms.t_mig_comments",
) -> None:
    """Insert a new comment for the request using the ARMS comments table."""
    escaped_comment = comment_text.replace("'", "''")
    query = (
        f"INSERT INTO {comment_table}(comment_id,request_id,comment_text,comment_time,comment_author,human_flag) "
        f"VALUES ((SELECT COALESCE(MAX(comment_id), 0) + 1 FROM {comment_table}), "
        f"{request_id}, '{escaped_comment}', CURRENT_TIMESTAMP, '{author}', 0);"
    )
    _execute_query(datasource, project_id=project_id, query=query)


def _validate_before_import(
    migration: Migration,
    *,
    target_env,
    target_project_name: str,
    poll_seconds: int = 5,
    timeout_seconds: int = POST_IMPORT_TIMEOUT_SECONDS,
) -> None:
    """Validate the package against the target; raise with the server message on failure."""
    print(f"Validating package against target project '{target_project_name}'...")
    try:
        migration.trigger_validation(target_env=target_env, target_project_name=target_project_name)
    except Exception as exc:
        if not _is_package_locked_error(exc):
            raise
        # A locked package rejects updates; the import reuses it via _migrate_with_lock_retry.
        print(f"Validation skipped: package is locked ({exc}). Continuing with locked-package import.")
        return
    deadline_epoch = datetime.now().timestamp() + timeout_seconds
    last_status = None

    while True:
        validation = migration.validation
        status = getattr(validation, "status", None)
        status_name = getattr(status, "name", str(status))
        if status_name != last_status:
            print(f"Validation status: {status_name} at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            last_status = status_name

        if status == ValidationStatus.VALIDATED:
            print("Validation succeeded.")
            return
        if status == ValidationStatus.VALIDATION_FAILED:
            server_message = str(getattr(validation, "message", "") or "").strip() or "no message returned"
            print(f"Validation failed: {server_message}")
            raise RuntimeError(f"Package validation failed: {server_message}")

        if datetime.now().timestamp() >= deadline_epoch:
            raise TimeoutError(
                f"Package validation timed out after {timeout_seconds}s; last status: {status_name}."
            )
        sleep(poll_seconds)


def _wait_for_migration_completion(
    migration: Migration,
    poll_seconds: int = 5,
    *,
    timeout_seconds: int = POST_IMPORT_TIMEOUT_SECONDS,
) -> ImportStatus | str | None:
    """Poll the migration import status until it reaches a terminal state."""
    start_time = datetime.now()
    deadline_epoch = start_time.timestamp() + timeout_seconds
    last_status = None

    while True:
        migration.fetch()

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

        if datetime.now().timestamp() >= deadline_epoch:
            raise TimeoutError(
                f"Migration import timed out after {timeout_seconds}s; last status: {status_name}."
            )
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


def _is_undo_package_ready(migration: Migration) -> bool:
    import_info = getattr(migration, "import_info", None)
    undo_storage = getattr(import_info, "undo_storage", None)
    undo_path = getattr(undo_storage, "path", None)
    return bool(str(undo_path or "").strip())


def _wait_for_post_import_readiness(
    migration: Migration,
    poll_seconds: int = POST_IMPORT_POLL_SECONDS,
    timeout_seconds: int = POST_IMPORT_TIMEOUT_SECONDS,
    require_unlocked_package: bool = REQUIRE_UNLOCKED_PACKAGE_AFTER_IMPORT,
) -> None:
    """Wait until import is complete and undo package exists.

    Some environments keep the migration package in LOCKED status even after
    successful import and undo package creation. In strict mode
    (`require_unlocked_package=True`), the workflow still waits for unlock.
    """
    deadline_epoch = datetime.now().timestamp() + timeout_seconds
    last_snapshot = None

    while True:
        migration.fetch()

        import_info = getattr(migration, "import_info", None)
        package_info = getattr(migration, "package_info", None)

        import_status = getattr(import_info, "status", None)
        import_status_name = getattr(import_status, "name", str(import_status))
        package_status = getattr(package_info, "status", None)
        package_status_name = getattr(package_status, "name", str(package_status or "unknown"))
        undo_request_status = getattr(import_info, "undo_request_status", None)
        undo_request_name = getattr(undo_request_status, "name", str(undo_request_status or "unknown"))
        undo_ready = _is_undo_package_ready(migration)

        snapshot = (
            str(import_status_name),
            str(package_status_name),
            str(undo_request_name),
            bool(undo_ready),
        )
        if snapshot != last_snapshot:
            print(
                "Post-import status: "
                f"import={import_status_name}, package={package_status_name}, "
                f"undo_request={undo_request_name}, undo_package_ready={undo_ready}"
            )
            last_snapshot = snapshot

        if import_status in (ImportStatus.IMPORT_FAILED, ImportStatus.UNDO_FAILED):
            raise RuntimeError(
                "Migration ended in a failed terminal state during post-import checks: "
                f"{import_status_name}."
            )

        import_completed = import_status in (ImportStatus.IMPORTED, ImportStatus.UNDO_SUCCESS)
        package_unlocked = package_status is None or str(package_status_name).lower() != "locked"

        if import_completed and undo_ready:
            if package_unlocked:
                print("Post-import checks passed: undo package is available and migration package is unlocked.")
                return
            if not require_unlocked_package:
                print(
                    "Post-import checks passed: import completed and undo package is available. "
                    "Package remains locked due to server lifecycle behavior."
                )
                return

        if datetime.now().timestamp() >= deadline_epoch:
            if require_unlocked_package:
                raise TimeoutError(
                    "Post-import checks timed out before migration became reusable. "
                    f"Last state: import={import_status_name}, package={package_status_name}, "
                    f"undo_request={undo_request_name}, undo_package_ready={undo_ready}."
                )
            raise TimeoutError(
                "Post-import checks timed out before import reached ready state. "
                f"Last state: import={import_status_name}, package={package_status_name}, "
                f"undo_request={undo_request_name}, undo_package_ready={undo_ready}."
            )

        sleep(poll_seconds)


def _ensure_undo_request_not_pending(
    migration: Migration,
    target_connection: Connection,
    require_requested: bool = REQUIRE_UNDO_REQUESTED_AFTER_IMPORT,
) -> None:
    """Move undoRequestStatus from PENDING to REQUESTED after successful import.

    This does not execute undo. It only advances the request lifecycle so the
    migration does not remain in a permanently pending undo-request state.
    """
    import_info = getattr(migration, "import_info", None)
    undo_request_status = getattr(import_info, "undo_request_status", None)
    undo_request_name = str(getattr(undo_request_status, "name", undo_request_status or "unknown")).upper()

    if undo_request_name != "PENDING":
        print(f"Undo request status already '{undo_request_name}', no normalization needed.")
        return

    migration_id = str(getattr(migration, "id", "")).strip()
    if not migration_id:
        message = "Cannot normalize undo request status: migration ID is missing."
        if require_requested:
            raise ValueError(message)
        print(message)
        return

    try:
        migration_api.update_migration(
            connection=target_connection,
            migration_id=migration_id,
            body={"importInfo": {"undoRequestStatus": "requested"}},
            prefer="respond-async",
            project_id=getattr(target_connection, "project_id", None),
        )
        migration.fetch()
    except Exception as exc:
        message = f"Unable to normalize undo request status for migration '{migration_id}': {exc}"
        if require_requested:
            raise RuntimeError(message) from exc
        print(message)
        return

    refreshed_status = getattr(getattr(migration, "import_info", None), "undo_request_status", None)
    refreshed_name = str(getattr(refreshed_status, "name", refreshed_status or "unknown")).upper()
    print(f"Undo request status after normalization: {refreshed_name}")

    if require_requested and refreshed_name == "PENDING":
        raise RuntimeError(
            "Undo request status is still PENDING after normalization attempt. "
            f"Migration ID: {migration_id}."
        )


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
    if ":" in migration_guid:
        print(
            f"Skipping job lookup: migration ID '{migration_guid}' is composite, "
            "not a single object GUID accepted by the job monitor. Job state is unknown."
        )
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
    on_reuse: Callable[[Migration], None] | None = None,
):
    """Import normally, or reuse a locked package in a new target-bound migration.

    The original instance is never deleted. on_reuse persists the replacement reference
    before the new import is started.
    """

    def _start_reused_migration(existing_migration: Migration):
        """Start the newly created import instance without rebinding the locked package."""
        migration_id = str(getattr(existing_migration, "id", "")).strip()
        if not migration_id:
            raise ValueError("Migration object has no valid ID.")

        source_conn = existing_migration.connection

        def _validate_target_binding(body: dict) -> None:
            import_info = body.get("importInfo") or {}
            bound_project_id = str((import_info.get("project") or {}).get("id") or "").strip()
            expected_project_id = str(getattr(target_env, "project_id", "") or "").strip()
            bound_environment = str((import_info.get("environment") or {}).get("id") or "").rstrip("/")
            expected_environment = str(getattr(target_env, "base_url", "") or "").rstrip("/")
            if (
                not expected_project_id
                or bound_project_id.lower() != expected_project_id.lower()
                or not expected_environment
                or bound_environment.lower() != expected_environment.lower()
            ):
                raise RuntimeError(
                    f"Locked migration '{migration_id}' cannot be rebound: saved import project "
                    f"'{bound_project_id}' on '{bound_environment}' does not match selected target "
                    f"'{expected_project_id}' on '{expected_environment}'. "
                    "Create and validate a migration bound to the intended target before importing. "
                    "The script will not change the project header to import into a different project."
                )

        existing_body = migration_api.get_migration(
            connection=source_conn,
            migration_id=migration_id,
            show_content="all",
        ).json()
        _validate_target_binding(existing_body)

        fetched_body = existing_body
        fetched_body["packageInfo"]["replicated"] = True

        return migration_api.start_migration(
            connection=target_env,
            migration_id=migration_id,
            prefer="respond-async",
            generate_undo=generate_undo,
            body=fetched_body,
            project_id=getattr(target_env, "project_id", None),
        )

    def _clone_migration_for_reimport(existing_migration: Migration) -> Migration:
        """Create a new migration object from the same package for re-import."""
        source_conn = existing_migration.connection
        package_id = str(getattr(existing_migration.package_info, "id", "")).strip()
        if not package_id:
            raise ValueError("Migration package ID is missing; cannot clone migration.")
        if not getattr(target_env, "project_id", None) or not getattr(target_env, "base_url", None):
            raise ValueError("Package reuse requires a target project ID and environment URL.")

        base_url = str(getattr(target_env, "base_url", "") or "").strip()
        target_env_id = base_url if base_url.endswith("/") else f"{base_url}/"

        create_body = {
            "importInfo": {
                "environment": {"id": target_env_id, "name": target_env_id},
                "project": {
                    "id": getattr(target_env, "project_id", None),
                    "name": target_project_name,
                },
            }
        }

        response = migration_api.create_new_migration(
            connection=source_conn,
            package_id=package_id,
            body=create_body,
            unlock_on_start=True,
        )
        cloned_id = (response.json() or {}).get("id")
        if not cloned_id:
            raise RuntimeError("Failed to clone migration from existing package.")

        print(f"Created replacement migration for re-import: {cloned_id}")
        return Migration(source_conn, id=cloned_id)

    for attempt in range(1, max_attempts + 1):
        try:
            result = migration.migrate(
                target_env=target_env,
                target_project_name=target_project_name,
                generate_undo=generate_undo,
            )
            return result, migration
        except Exception as exc:  # pragma: no cover - lock path is exercised in tests
            if not _is_package_locked_error(exc):
                raise

            if FORCE_UNLOCK_LOCKED_PACKAGE:
                print(
                    f"Package '{getattr(migration, 'name', getattr(migration, 'id', 'unknown'))}' is locked; "
                    "FORCE_UNLOCK_LOCKED_PACKAGE is enabled, but this script will not delete the migration object "
                    "before import."
                )

            print(
                f"Package '{getattr(migration, 'name', getattr(migration, 'id', 'unknown'))}' is locked; "
                "reusing the package in a new migration bound to the selected target."
            )

            has_runtime_metadata = bool(str(getattr(migration, "id", "")).strip()) and hasattr(
                migration, "connection"
            )
            if not has_runtime_metadata:
                raise

            cloned_migration = _clone_migration_for_reimport(migration)
            if on_reuse is not None:
                on_reuse(cloned_migration)
            start_response = _start_reused_migration(cloned_migration)
            print(f"Reused-package import started for migration '{cloned_migration.id}'.")
            return start_response, cloned_migration

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

    selected_request = _select_request_with_migration(request_rows, db_instance, PROJECT_ID)
    if selected_request is None:
        print("No matching requests have a migration package available to import.")
        my_conn.close()
        return 0
    request_row, migration_obj_guid = selected_request
    request_id = _get_first_value_from_result_data(
        request_row,
        "request_id",
        "Request@ID",
        "request",
    )
    print(f"Retrieved request number: {request_id}")

    source_project_name = (
        _get_first_value_from_result_data(
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
        _get_first_value_from_result_data(
            request_row,
            "target_project_name",
            "Target Project@NAME",
            "target_project",
        )
        or ""
    )
    if not FORCE_STATUS_TARGET_PROJECT_RESOLUTION and not source_project_name and target_project_name_from_request:
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

    try:
        if request_status_id is None or str(request_status_id).strip() == "":
            if FORCE_STATUS_TARGET_PROJECT_RESOLUTION:
                raise ValueError(f"Request {request_id} has no status_id; strict status resolution cannot continue.")
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
            if FORCE_STATUS_TARGET_PROJECT_RESOLUTION and not target_env_id:
                raise ValueError(
                    f"Request {request_id}: status_id={request_status_id} has no target environment mapping "
                    f"in {STATUS_TABLE}; strict status resolution cannot continue."
                )
            target_project_prefix = ""
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
            else:
                target_env_name = SOURCE_ENVIRONMENT
                target_env_url = _resolve_target_environment_url(
                    BASE_URL,
                    SOURCE_ENVIRONMENT,
                    target_env_name,
                    ENVIRONMENT_URLS,
                )

            target_project_name = _resolve_target_project_name_from_sql(
                db_instance,
                PROJECT_ID,
                target_env_id,
                request_status_id,
                source_project_name,
                target_project_prefix,
                target_project_name_from_request,
                (
                    None
                    if FORCE_STATUS_TARGET_PROJECT_RESOLUTION
                    else request_row.get("tgt_project_id") or request_row.get("target_project_id")
                ),
                strict_status=FORCE_STATUS_TARGET_PROJECT_RESOLUTION,
            )
    except ValueError as exc:
        message = "Unable to resolve matchine Target Project name. Please check Environment/Status/Project mapping."
        print(message)
        print(f"Target project resolution failed for request {request_id}: {exc}")
        try:
            _insert_comment(
                datasource=db_instance,
                project_id=PROJECT_ID,
                request_id=request_id,
                comment_text=message,
            )
            resolution_failed_status_id = TARGET_RESOLUTION_FAILED_STATUS_ID
            if target_env_id:
                try:
                    resolution_failed_status_id = _get_result_status_id_for_environment(
                        db_instance, PROJECT_ID, target_env_id, succeeded=False,
                    )
                except ValueError as status_exc:
                    print(
                        f"Unable to resolve environment failure status: {status_exc}. "
                        f"Using fallback status_id={resolution_failed_status_id}."
                    )
            _execute_query(
                db_instance,
                project_id=PROJECT_ID,
                query=(
                    f"UPDATE {REQUEST_TABLE} SET status_id={resolution_failed_status_id} "
                    f"WHERE request_id={request_id};"
                ),
            )
            _execute_query(
                db_instance,
                project_id=PROJECT_ID,
                query=(
                    f"UPDATE arms.t_mig_dim_migration SET status_id={resolution_failed_status_id} "
                    f"WHERE request_id={request_id} AND migration_id=("
                    "SELECT MAX(migration_id) FROM arms.t_mig_dim_migration "
                    f"WHERE request_id={request_id});"
                ),
            )
            print(
                f"Set request {request_id} and its latest migration record, if present, "
                f"to status_id={resolution_failed_status_id}."
            )
        except Exception as writeback_exc:
            raise RuntimeError(
                f"Target project resolution failed: {exc}. "
                f"Unable to persist resolution-failure comment/statuses: {writeback_exc}"
            ) from writeback_exc
        finally:
            my_conn.close()
        raise

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
    active_migration = None
    try:
        if not target_env_id:
            raise ValueError("A target environment mapping is required to resolve migration result statuses.")
        success_status_id = _get_result_status_id_for_environment(
            db_instance, PROJECT_ID, target_env_id, succeeded=True,
        )
        failed_status_id = _get_result_status_id_for_environment(
            db_instance, PROJECT_ID, target_env_id, succeeded=False,
        )
        arms_project_id = _resolve_target_arms_project_id(
            datasource=db_instance,
            query_project_id=PROJECT_ID,
            target_project_name=target_project_name,
        )
        try:
            target_env = Connection(
                target_env_url,
                MSTR_USERNAME,
                MSTR_PASSWORD,
                project_name=target_project_name,
            )
            current_migration = Migration(my_conn, id=migration_obj_guid)
            _list_active_migration_jobs(my_conn, migration_obj_guid, limit=20)
            _validate_before_import(
                current_migration,
                target_env=target_env,
                target_project_name=target_project_name,
                poll_seconds=POST_IMPORT_POLL_SECONDS,
            )
            print("Starting package import...")
            def record_reused_migration(reused_migration: Migration) -> None:
                nonlocal active_migration
                active_migration = reused_migration
                _execute_query(
                    db_instance,
                    project_id=PROJECT_ID,
                    query=(
                        f"UPDATE {MIGRATION_OBJECT_TABLE} "
                        f"SET migration_obj_id={_sql_literal(reused_migration.id)}, "
                        "migration_obj_cre_time=CURRENT_TIMESTAMP "
                        f"WHERE migration_key_id=(SELECT migration_key_id FROM {REQUEST_TABLE} "
                        f"WHERE request_id={request_id});"
                    ),
                )

            migration_start_time = datetime.now()
            print(
                f"Starting migration at {migration_start_time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"with undo generation enabled."
            )
            import_result, active_migration = _migrate_with_lock_retry(
                current_migration,
                target_env=target_env,
                target_project_name=target_project_name,
                generate_undo=True,
                poll_seconds=5,
                max_attempts=1,
                on_reuse=record_reused_migration,
            )
            import_status = _wait_for_migration_completion(active_migration)
            print(f"Migration import status final value: {getattr(import_status, 'name', str(import_status))}")
            if import_status != ImportStatus.IMPORTED:
                server_detail = str(getattr(getattr(active_migration, "import_info", None), "message", "") or "").strip()
                raise RuntimeError(
                    "Migration did not complete a successful import: "
                    f"{getattr(import_status, 'name', str(import_status))}."
                    + (f" Server message: {server_detail}" if server_detail else "")
                )
            _wait_for_post_import_readiness(
                active_migration,
                poll_seconds=POST_IMPORT_POLL_SECONDS,
                timeout_seconds=POST_IMPORT_TIMEOUT_SECONDS,
                require_unlocked_package=REQUIRE_UNLOCKED_PACKAGE_AFTER_IMPORT,
            )
            _ensure_undo_request_not_pending(
                active_migration,
                target_connection=target_env,
                require_requested=REQUIRE_UNDO_REQUESTED_AFTER_IMPORT,
            )
        except Exception as exc:
            error_message = f"Migration failed for request {request_id} on target environment '{target_env_name}': {exc}"
            print(error_message)
            try:
                _execute_query(
                    db_instance,
                    project_id=PROJECT_ID,
                    query=(
                        f"UPDATE {REQUEST_TABLE} SET status_id={failed_status_id} "
                        f"WHERE request_id={request_id};"
                    ),
                )
                _insert_migration_dim_record(
                    datasource=db_instance,
                    project_id=PROJECT_ID,
                    request_id=request_id,
                    source_project_name=source_project_name,
                    target_project_name=target_project_name,
                    target_env_name=target_env_name,
                    migration_obj_guid=str(getattr(active_migration, "id", migration_obj_guid)),
                    migration_id=str(getattr(active_migration, "id", migration_obj_guid)),
                    migration_name=str(getattr(active_migration, "name", migration_obj_guid)),
                    status_id=failed_status_id,
                    arms_project_id=arms_project_id,
                    migration_message=error_message,
                )
                _insert_comment(
                    datasource=db_instance,
                    project_id=PROJECT_ID,
                    request_id=request_id,
                    comment_text=error_message,
                )
            except Exception as writeback_exc:
                raise RuntimeError(
                    f"{error_message}\nFailed to persist failure status/history/comment: {writeback_exc}"
                ) from writeback_exc
            print(
                f"Migration failed for request {request_id}; updated request and recorded migration attempt with "
                f"status_id={failed_status_id} for environment '{target_env_name}'."
            )
            raise

        print("Import result:")
        print(import_result)

        migration_name = getattr(active_migration, "name", None) or str(migration_obj_guid)
        target_status_id = success_status_id
        final_migration_id = str(getattr(active_migration, "id", migration_obj_guid))
        _insert_migration_dim_record(
            datasource=db_instance,
            project_id=PROJECT_ID,
            request_id=request_id,
            source_project_name=source_project_name,
            target_project_name=target_project_name,
            target_env_name=target_env_name,
            migration_obj_guid=final_migration_id,
            migration_id=final_migration_id,
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
        _execute_query(
            db_instance,
            query=f"UPDATE {REQUEST_TABLE} SET status_id={target_status_id} WHERE request_id={request_id};",
            project_id=PROJECT_ID,
        )
        return 0
    finally:
        if target_env is not None:
            target_env.close()
        my_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
