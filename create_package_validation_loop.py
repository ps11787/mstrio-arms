import os
from mstrio.object_management.object import Object
from mstrio.datasources.datasource_instance import DatasourceInstance
from mstrio.project_objects.report import Report
from mstrio.object_management.folder import Folder, get_folder_id_from_path, list_folders
from mstrio.object_management.migration.package import Action, ValidationStatus
from mstrio.object_management.migration import PackageSettings, Migration
from mstrio.distribution_services.email import send_email
from mstrio.users_and_groups import User
from mstrio.types import ObjectSubTypes, ObjectTypes
from datetime import datetime
from html import escape
import re
from time import sleep
from mstrio.connection import Connection

BASE_URL = os.getenv("MSTR_BASE_URL", "https://env-371825.ma.cloud.microstrategy.com/MicroStrategyLibrary")
MSTR_USERNAME = os.getenv("MSTR_USERNAME", "mstr")
MSTR_PASSWORD = os.getenv("MSTR_PASSWORD", "EW85AcqE1SoT")
PROJECT_ID = os.getenv("MSTR_PROJECT_ID", "4B0544627D49F8DA03C26D817D208263")
DATASOURCE_INSTANCE_ID = os.getenv("MSTR_DATASOURCE_INSTANCE_ID", "A23BBC514D336D5B4FCE919FE19661A3")
REQUEST_REPORT_ID = os.getenv("MSTR_REQUEST_REPORT_ID", "A62CDD7147492E20EE83C5841B7C9183")
ENABLE_VALIDATION = os.getenv("MSTR_ENABLE_VALIDATION", "false").strip().lower() in {"1", "true", "yes", "y"}


def _normalize_mstr_folder_path(project_name, migration_path):
    raw_path = str(migration_path).strip().replace(chr(92), "/")
    if not raw_path:
        raise ValueError("Migration Obj@Content Path is empty.")

    # Use direct absolute path when provided, e.g. /Project/Public Objects/Reports.
    if raw_path.startswith("/"):
        segments = [segment.strip() for segment in raw_path.split("/") if segment.strip()]
        return f"/{'/'.join(segments)}"

    # Treat non-absolute values as project-relative paths.
    segments = [segment.strip() for segment in raw_path.split("/") if segment.strip()]
    return f"/{project_name.strip()}/{'/'.join(segments)}"


def _resolve_folder_id(connection, folder_path, project_name):
    try:
        return get_folder_id_from_path(connection, path=folder_path)
    except ValueError:
        normalized_target = folder_path.lower()
        for folder in list_folders(connection, project_name=project_name, include_subfolders=True):
            if folder.path and folder.path.lower() == normalized_target:
                return folder.id
        raise


def _format_dt_userfriendly(value):
    if value is None:
        return "N/A"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _normalize_object_type_name(value):
    if value is None:
        return ""

    if isinstance(value, str):
        text = value
    elif hasattr(value, "name"):
        text = value.name
    else:
        text = str(value)
        for enum_type in (ObjectTypes, ObjectSubTypes):
            try:
                if enum_type.contains(value):
                    text = enum_type(value).name
                    break
            except Exception:
                pass

    text = text.split(".")[-1] if "." in str(text) else str(text)
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _canonical_object_type_name(value):
    normalized = _normalize_object_type_name(value)
    aliases = {
        "metric_agg": "agg_metric",
    }
    return aliases.get(normalized, normalized)


def _is_schema_object_type(obj_type):
    normalized = _canonical_object_type_name(obj_type)
    return normalized in {"schema", "schema_definition", "project_schema"}


def _required_dependency_types():
    return {
        "metric",
        "agg_metric",
        "metric_agg",
        "metric_dmx",
        "metric_training",
        "dataset",
        "datamart",
        "datamart_report",
        "report",
        "report_definition",
        "report_grid",
        "report_graph",
        "report_text",
        "report_engine",
        "report_base",
        "report_transaction",
        "report_hyper_card",
        "report_non_interactive",
        "incremental_refresh_report",
        "cube",
        "olap_cube",
        "super_cube",
        "super_cube_irr",
        "prompt",
        "prompt_boolean",
        "prompt_long",
        "prompt_string",
        "prompt_double",
        "prompt_date",
        "prompt_objects",
        "prompt_elements",
        "prompt_expression",
        "prompt_expression_draft",
        "prompt_dimty",
        "prompt_big_decimal",
        "filter",
        "global_filter",
        "security_filter",
    }


def _is_required_dependency_type(obj_type):
    normalized = _canonical_object_type_name(obj_type)
    return normalized in _required_dependency_types()


_BUILTIN_METRIC_FUNCTION_NAMES = {
    "aggregation",
    "average",
    "count",
    "geometric mean",
    "maximum",
    "median",
    "minimum",
    "mode",
    "product",
    "standard deviation",
    "sum of wya",
    "total",
    "variance",
    "weighted yearly average",
}


def _is_builtin_system_dependency(dep):
    if dep is None:
        return True

    dep_name = str(dep.get("name") or "").strip()
    dep_type_raw = dep.get("type")
    dep_type = str(dep_type_raw or "").strip()
    dep_id = str(dep.get("id") or "").strip()
    if not dep_id or not dep_type:
        return True

    normalized_dep_name = re.sub(r"[^a-z0-9]+", " ", dep_name.lower()).strip()
    normalized_dep_type = _canonical_object_type_name(dep_type_raw)

    if dep_name in {"*", ">", "DSS Built-in Package"}:
        return True
    if dep_name.lower().startswith("dss built-in"):
        return True
    if dep_name and dep_type.lower() in {"folder", "package", "project"} and set(dep_name) <= {"*", ">"}:
        return True
    if (
        normalized_dep_type in {"metric", "agg_metric", "metric_agg", "metric_dmx", "metric_training", "function"}
        and normalized_dep_name in _BUILTIN_METRIC_FUNCTION_NAMES
    ):
        return True

    return False


def _canonical_object_id(value):
    if value is None:
        return ""
    return str(value).strip().upper()


def _is_allowed_dependency_entry(dep):
    if dep is None or _is_builtin_system_dependency(dep):
        return False

    dep_id = dep.get("id")
    dep_type = dep.get("type")
    if not dep_id or not dep_type:
        return False
    if not _is_required_dependency_type(dep_type):
        return False
    return True


def _is_shortcut_root_entry(dep):
    if dep is None or _is_builtin_system_dependency(dep) or _is_schema_object_type(dep.get("type")):
        return False

    dep_id = dep.get("id")
    dep_type = dep.get("type")
    return bool(dep_id and dep_type)


def _resolve_target_object(obj, connection=None):
    if obj is None:
        return None

    target_info = getattr(obj, "target_info", None)
    if not isinstance(target_info, dict):
        return obj

    target_id = (
        target_info.get("id")
        or target_info.get("object_id")
        or target_info.get("objectId")
    )
    target_type = target_info.get("type")
    if not target_id or not target_type or connection is None:
        return obj

    try:
        return Object(connection=connection, id=target_id, type=target_type)
    except Exception:
        return obj


def _resolve_shortcut_root_object(obj, connection=None):
    target_obj = _resolve_target_object(obj, connection)
    if target_obj is None:
        return None

    if _canonical_object_type_name(getattr(target_obj, "type", None)) != "shortcut_type":
        return target_obj

    if connection is None:
        return None

    try:
        dependencies = obj.list_dependencies() or []
    except Exception:
        return None

    for dependency in dependencies:
        if not _is_allowed_dependency_entry(dependency):
            continue

        dep_id = _canonical_object_id(dependency.get("id"))
        dep_type = dependency.get("type")
        if not dep_id or not dep_type:
            continue

        try:
            return Object(connection=connection, id=dep_id, type=dep_type)
        except Exception:
            continue

    return None


def _resolve_shortcut_target_entry(obj, connection=None):
    if obj is None:
        return None, None

    target_info = getattr(obj, "target_info", None)
    if isinstance(target_info, dict):
        target_id = target_info.get("id") or target_info.get("object_id") or target_info.get("objectId")
        target_type = target_info.get("type")
        if target_id and target_type:
            target_obj = None
            if connection is not None:
                try:
                    target_obj = Object(connection=connection, id=target_id, type=target_type)
                except Exception:
                    target_obj = None
            return target_info, target_obj

    try:
        dependencies = obj.list_dependencies() or []
    except Exception:
        return None, None

    for dependency in dependencies:
        if not _is_allowed_dependency_entry(dependency):
            continue

        dep_id = _canonical_object_id(dependency.get("id"))
        dep_type = dependency.get("type")
        if not dep_id or not dep_type:
            continue

        dep_obj = None
        if connection is not None:
            try:
                dep_obj = Object(connection=connection, id=dep_id, type=dep_type)
            except Exception:
                dep_obj = None
        return dependency, dep_obj

    return None, None


def _collect_package_dependencies(shortcut_objects, connection=None):
    collected = []
    seen_ids = set()

    def add_dependency(dep, *, include_if_required=True):
        if not _is_allowed_dependency_entry(dep):
            return

        dep_id = _canonical_object_id(dep.get("id"))
        dep_type = dep.get("type")
        if dep_id in seen_ids:
            return
        if include_if_required and not _is_required_dependency_type(dep_type):
            return

        dep_name = dep.get("name")
        dep_obj = None
        if connection is not None:
            try:
                dep_obj = Object(connection=connection, id=dep_id, type=dep_type)
            except Exception:
                dep_obj = None

        item = {
            "id": dep_id,
            "type": dep_type,
            "name": dep_name or getattr(dep_obj, "name", ""),
            "date_modified": getattr(dep_obj, "date_modified", dep.get("date_modified")),
        }
        if dep_obj is not None:
            item["location"] = getattr(dep_obj, "location", None)
            item["date_created"] = getattr(dep_obj, "date_created", None)
        else:
            item["location"] = dep.get("location")
            item["date_created"] = dep.get("date_created")

        collected.append(item)
        seen_ids.add(dep_id)

    def walk_object_dependencies(target_obj, visited=None):
        if target_obj is None:
            return

        target_id = _canonical_object_id(getattr(target_obj, "id", None))
        if target_id:
            visited = set() if visited is None else visited
            if target_id in visited:
                return
            visited.add(target_id)

        try:
            dependencies = target_obj.list_dependencies() or []
        except Exception:
            return

        for dependency in dependencies:
            if not _is_allowed_dependency_entry(dependency):
                continue

            dep_id = _canonical_object_id(dependency.get("id"))
            dep_type = dependency.get("type")
            if not dep_id:
                continue

            dep_obj = None
            if connection is not None and dep_type:
                try:
                    dep_obj = Object(connection=connection, id=dep_id, type=dep_type)
                except Exception:
                    dep_obj = None

            add_dependency({**dependency, "id": dep_id}, include_if_required=True)
            if dep_obj is not None:
                walk_object_dependencies(dep_obj, visited)

    for shortcut in shortcut_objects or []:
        target_obj = _resolve_target_object(shortcut, connection)
        if target_obj is not None:
            dep_entry = {
                "id": getattr(target_obj, "id", getattr(shortcut, "id", None)),
                "type": getattr(target_obj, "type", getattr(shortcut, "type", None)),
                "name": getattr(target_obj, "name", getattr(shortcut, "name", None)),
            }
            if _is_allowed_dependency_entry(dep_entry):
                add_dependency(dep_entry, include_if_required=True)

        dependencies = []
        try:
            dependencies = (target_obj or shortcut).list_dependencies() or []
        except Exception:
            dependencies = []

        for dependency in dependencies:
            if not _is_allowed_dependency_entry(dependency):
                continue

            dep_id = _canonical_object_id(dependency.get("id"))
            dep_type = dependency.get("type")
            if not dep_id:
                continue

            dep_obj = None
            if connection is not None and dep_type:
                try:
                    dep_obj = Object(connection=connection, id=dep_id, type=dep_type)
                except Exception:
                    dep_obj = None

            add_dependency({**dependency, "id": dep_id}, include_if_required=True)
            if dep_obj is not None:
                walk_object_dependencies(dep_obj, set())

    return collected


def _parse_missing_object(validation_message):
    if not validation_message:
        return None

    object_match = re.search(
        r"Object:\s*<([^>]+?)\s+id=([0-9A-Fa-f]{32})>\s+is\s+not\s+found",
        validation_message,
        re.IGNORECASE,
    )
    if not object_match:
        return None

    object_type_name = re.sub(r"[^A-Za-z0-9]+", "_", object_match.group(1)).strip("_")
    object_type = getattr(ObjectTypes, object_type_name.upper(), None)
    if object_type is None:
        return None

    return {
        "id": object_match.group(2),
        "type": object_type,
    }


def _sql_literal(value):
    if value is None:
        return "''"
    return str(value).replace("'", "''")


def _db_object_type_name(value):
    return _canonical_object_type_name(value)


def _build_object_insert_queries(object_properties, request_id):
    return [
        f"INSERT INTO arms.t_mig_dim_objects(id,type,name,location,date_created,date_modified,keyid,migration_key_id) VALUES ('{_sql_literal(item['id'])}','{_sql_literal(_db_object_type_name(item['type']))}','{_sql_literal(item['name'])}','{_sql_literal(item['location'])}', '{_sql_literal(item['date_created'])}','{_sql_literal(item['date_modified'])}', (select max(keyid) +1 from arms.t_mig_dim_objects),(select migration_key_id from arms.t_mig_dim_request where request_id={request_id}));"
        for item in object_properties
    ]


def _normalize_email_recipients(raw_value):
    if raw_value is None:
        return []

    raw_text = str(raw_value).strip()
    if not raw_text or raw_text.lower() == "nan":
        return []

    recipients = []
    for value in re.split(r"[;,]", raw_text):
        normalized_value = value.strip()
        if normalized_value:
            recipients.append(normalized_value)

    return recipients


def _delete_existing_migration_if_present(connection, migration_guid, *, force=True):
    migration_guid = str(migration_guid or "").strip()
    if not migration_guid:
        print("No existing migration GUID found; skipping delete and creating a new migration.")
        return None

    try:
        current_migration = Migration(connection, id=migration_guid)
    except Exception as exc:
        print(
            f"Migration GUID '{migration_guid}' is missing or invalid; skipping delete and creating a new migration. "
            f"Error: {exc}"
        )
        return None

    try:
        current_migration.delete(force=force)
        print(f"Deleted existing migration '{migration_guid}' before creating a new package.")
    except Exception as exc:
        print(
            f"Could not delete existing migration '{migration_guid}'; continuing with new migration creation. "
            f"Error: {exc}"
        )

    return current_migration


def _resolve_requestor_recipient_ids(connection, raw_recipients):
    resolved_recipient_ids = []

    for recipient in raw_recipients:
        if re.fullmatch(r"[0-9A-Fa-f]{32}", recipient):
            resolved_recipient_ids.append(recipient.upper())
            continue

        try:
            resolved_user = User(connection, username=recipient)
        except Exception:
            try:
                resolved_user = User(connection, id=recipient)
            except Exception:
                resolved_user = User(connection, name=recipient)

        resolved_recipient_ids.append(resolved_user.id)

    return resolved_recipient_ids


def _get_requestor_recipients(request_row):
    candidate_keys = [
        "Requestor@ID",
        "Requestor",
        "Requestor ID",
        "Requestor@NAME",
    ]

    for key in candidate_keys:
        recipients = _normalize_email_recipients(request_row.get(key))
        if recipients:
            return recipients

    for key in request_row.index:
        if str(key).lower().startswith("requestor"):
            recipients = _normalize_email_recipients(request_row.get(key))
            if recipients:
                return recipients

    raise ValueError("No requestor user ID found in the request report.")

def main() -> int:
    print("Connecting to environment...")
    myConn = Connection(BASE_URL, MSTR_USERNAME, MSTR_PASSWORD, project_id=PROJECT_ID)
    db_instance = DatasourceInstance(connection=myConn, id=DATASOURCE_INSTANCE_ID)

    request_list = Report(connection=myConn, id=REQUEST_REPORT_ID)
    df = request_list.to_dataframe()

    source_project_name = str(df['Project@Name'].iloc[0]).strip()
    source_project_id = str(df['Project@ID'].iloc[0]).strip()

    if re.fullmatch(r"[0-9A-Fa-f]{32}", source_project_id):
        source_project_conn = Connection(BASE_URL, MSTR_USERNAME, MSTR_PASSWORD, project_id=source_project_id)
    else:
        source_project_conn = Connection(BASE_URL, MSTR_USERNAME, MSTR_PASSWORD, project_name=source_project_name)

    myPackageSettings = PackageSettings(
        Action.REPLACE,
        PackageSettings.UpdateSchema.UPDATE_SCHEMA_LOGICAL_INFO,
        PackageSettings.AclOnReplacingObjects.USE_EXISTING,
        PackageSettings.AclOnNewObjects.INHERIT_ACL_AS_DEST_FOLDER,
    )
    project_name = source_project_name
    migration_content_path = str(df['Migration Obj@Content Path'].iloc[0])
    shortcut_path = _normalize_mstr_folder_path(project_name, migration_content_path)
    request_id = df['Request@ID'].iloc[0]
    target_project_name = str(df['Target Project@NAME'].iloc[0]).strip()
    requestor_recipients = _resolve_requestor_recipient_ids(
        myConn,
        _get_requestor_recipients(df.iloc[0]),
    )
    print(
        "Retrieved migration parameters:\n"
        f"  Request ID: {request_id}\n"
        f"  Source project: {project_name}\n"
        f"  Target project: {target_project_name}\n"
        f"  Shortcut path: {shortcut_path}"
    )
    shortcuts4Migration = Folder(source_project_conn, id=_resolve_folder_id(source_project_conn, shortcut_path, project_name)).get_contents()

    objects4Migration_properties = []
    objects4Migration = []
    seen_object_ids = set()

    for obj in shortcuts4Migration:
        target_entry, target_obj = _resolve_shortcut_target_entry(obj, source_project_conn)
        root_obj = target_obj or _resolve_shortcut_root_object(obj, source_project_conn)
        if root_obj is None:
            print(f"Skipping shortcut '{getattr(obj, 'name', obj.id)}': no target object found.")
            continue

        obj_id = _canonical_object_id((target_entry or {}).get("id") or getattr(root_obj, "id", None))
        obj_type = (target_entry or {}).get("type") or getattr(root_obj, "type", None)
        obj_name = (target_entry or {}).get("name") or getattr(root_obj, "name", getattr(obj, "name", None))
        obj_date_modified = (target_entry or {}).get("date_modified") or getattr(root_obj, "date_modified", getattr(obj, "date_modified", None))
        if _is_shortcut_root_entry({"id": obj_id, "type": obj_type, "name": obj_name}) and obj_id not in seen_object_ids:
            objects4Migration.append({"id": obj_id, "type": obj_type, "name": obj_name, "date_modified": obj_date_modified})
            objects4Migration_properties.append({
                "id": obj_id,
                "type": obj_type,
                "name": obj_name,
                "location": getattr(root_obj, "location", None),
                "date_created": getattr(root_obj, "date_created", None),
                "date_modified": obj_date_modified,
            })
            seen_object_ids.add(obj_id)

        dependencies = []
        try:
            dependencies = root_obj.list_dependencies() or []
        except Exception:
            dependencies = []
        if not dependencies:
            print(f"Skipping shortcut '{getattr(root_obj, 'name', obj_id)}' ({obj_id}): no target dependency found.")
            continue

        for dependency in dependencies:
            if not _is_allowed_dependency_entry(dependency):
                continue

            dep_id = _canonical_object_id(dependency.get("id"))
            dep_type = dependency.get("type")
            if not dep_id or dep_id in seen_object_ids:
                continue

            dep_obj = Object(connection=source_project_conn, id=dep_id, type=dep_type)
            objects4Migration.append({"id": dep_id, "type": dep_type, "name": dependency.get("name") or dep_obj.name, "date_modified": getattr(dep_obj, "date_modified", dependency.get("date_modified"))})
            objects4Migration_properties.append({"id": dep_obj.id, "type": dep_obj.type, "name": dep_obj.name, "location": getattr(dep_obj, "location", None), "date_created": getattr(dep_obj, "date_created", None), "date_modified": dep_obj.date_modified})
            seen_object_ids.add(dep_id)

            try:
                target_dependencies = dep_obj.list_dependencies() or []
            except Exception:
                target_dependencies = []

            for target_dependency in target_dependencies:
                if not _is_allowed_dependency_entry(target_dependency):
                    continue

                added_dep_id = _canonical_object_id(target_dependency.get("id"))
                added_dep_type = target_dependency.get("type")
                if not added_dep_id or added_dep_id in seen_object_ids:
                    continue

                added_dep_obj = Object(connection=source_project_conn, id=added_dep_id, type=added_dep_type)
                objects4Migration.append({"id": added_dep_id, "type": added_dep_type, "name": target_dependency.get("name") or added_dep_obj.name, "date_modified": getattr(added_dep_obj, "date_modified", target_dependency.get("date_modified"))})
                objects4Migration_properties.append({"id": added_dep_obj.id, "type": added_dep_obj.type, "name": added_dep_obj.name, "location": getattr(added_dep_obj, "location", None), "date_created": getattr(added_dep_obj, "date_created", None), "date_modified": added_dep_obj.date_modified})
                seen_object_ids.add(added_dep_id)

    extra_dependencies = _collect_package_dependencies(shortcuts4Migration, connection=source_project_conn)
    for dependency in extra_dependencies:
        dep_id = _canonical_object_id(dependency["id"])
        if dep_id in seen_object_ids:
            continue
        objects4Migration.append({"id": dep_id, "type": dependency["type"], "name": dependency.get("name"), "date_modified": dependency.get("date_modified")})
        objects4Migration_properties.append({
            "id": dep_id,
            "type": dependency["type"],
            "name": dependency.get("name"),
            "location": dependency.get("location"),
            "date_created": dependency.get("date_created"),
            "date_modified": dependency.get("date_modified"),
        })
        seen_object_ids.add(dep_id)

    print(objects4Migration_properties)

    if not objects4Migration:
        raise ValueError(
            "No valid migration package content was found after dependency resolution. "
            "The shortcut folder produced no allowed dependency objects (metrics, datasets, reports, filters, prompts, cubes)."
        )

    myPackageConfig = Migration.build_package_config(
        connection=source_project_conn,
        content=objects4Migration,
        package_settings=myPackageSettings,
    )

    print("check for existing migrations..")
    migration_guid = str(df['Migration Obj@GUID'].iloc[0]).strip() if 'Migration Obj@GUID' in df.columns else ""
    _delete_existing_migration_if_present(source_project_conn, migration_guid, force=True)

    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    package_name = f"Req_{df['Request@ID'].iloc[0]}_{df['Migration Obj@Name'].iloc[0]}_{timestamp}"
    Migration.create_object_migration(source_project_conn, toc_view=myPackageConfig, name=package_name, project_name=df['Project@Name'].iloc[0])

    print("SQL: clearing existing migration objects")
    db_instance.execute_query(query=f"delete from arms.t_mig_dim_objects where migration_key_id=(select migration_key_id from arms.t_mig_dim_request where request_id={df['Request@ID'].iloc[0]});", project_id=PROJECT_ID)
    sql_queries_insert_objects = _build_object_insert_queries(objects4Migration_properties, request_id)
    print("SQL: inserting migration objects")
    db_instance.execute_query(query="\n".join(sql_queries_insert_objects), project_id=PROJECT_ID)

    current_migration = Migration(source_project_conn, name=package_name)
    migration_id = current_migration.id
    sql_query_insert_comment = f"INSERT INTO arms.t_mig_comments(comment_id,request_id,comment_text,comment_time,comment_author,human_flag) VALUES ((select max (comment_id) + 1 from arms.t_mig_comments),{df['Request@ID'].iloc[0]},'Package Created with mstrio',current_timestamp,'system',0);"
    sql_query_upd_req_table = f"update arms.t_mig_dim_request set status_id=2 where request_id={df['Request@ID'].iloc[0]};"
    sql_query_insert_migration = f"Update arms.t_mig_lu_migration_obj set migration_obj_id='{migration_id}',migration_obj_cre_time=current_timestamp WHERE migration_key_id=(select migration_key_id from arms.t_mig_dim_request where request_id={df['Request@ID'].iloc[0]});"
    print("SQL: recording package creation comment")
    db_instance.execute_query(query=sql_query_insert_comment, project_id=PROJECT_ID)
    print("SQL: updating migration package ID")
    db_instance.execute_query(query=sql_query_insert_migration, project_id=PROJECT_ID)
    print("SQL: updating request status")
    db_instance.execute_query(query=sql_query_upd_req_table, project_id=PROJECT_ID)

    if ENABLE_VALIDATION:
        targetENV = Connection(BASE_URL, MSTR_USERNAME, MSTR_PASSWORD, project_name=target_project_name)
        validation_retry_count = 0
        added_missing_object_ids = set()
        validation_failure_comment_written = False
        while True:
            validation_trigger_result = current_migration.trigger_validation(target_env=targetENV, target_project_name=target_project_name)
            print("Validation trigger response:")
            print(validation_trigger_result)
            print("Monitoring validation status...")
            last_snapshot = None
            while True:
                validation_result = current_migration.validation
                if callable(validation_result):
                    validation_result = validation_result()
                status = validation_result.status
                status_text = getattr(status, "name", str(status))
                snapshot = (status_text, validation_result.progress, validation_result.message)
                if snapshot != last_snapshot:
                    print(f"Validation status: {status_text}; progress: {validation_result.progress}; message: {validation_result.message}; created: {_format_dt_userfriendly(validation_result.creation_date)}; last update: {_format_dt_userfriendly(validation_result.last_update_date)}")
                    last_snapshot = snapshot
                if status in (ValidationStatus.VALIDATED, ValidationStatus.VALIDATION_FAILED):
                    break
                sleep(5)
            if validation_result.status != ValidationStatus.VALIDATION_FAILED:
                break
            print("Validation failed message:", validation_result.message)
            if not validation_failure_comment_written:
                failure_comment = f"Validation failed for {target_project_name}"
                if validation_result.message:
                    failure_comment = f"{failure_comment}: {validation_result.message}"
                failure_comment_escaped = failure_comment.replace("'", "''")
                sql_query_insert_validation_failure_comment = (
                    f"INSERT INTO arms.t_mig_comments(comment_id,request_id,comment_text,comment_time,comment_author,human_flag) "
                    f"VALUES ((select max (comment_id) + 1 from arms.t_mig_comments),{request_id},'{failure_comment_escaped}',current_timestamp,'system',0);"
                )
                print("SQL: recording first validation failure comment")
                db_instance.execute_query(query=sql_query_insert_validation_failure_comment, project_id=PROJECT_ID)
                validation_failure_comment_written = True
            missing_object = _parse_missing_object(validation_result.message)
            if not missing_object:
                print("Could not identify a missing object ID and type; retrying validation.")
                continue
            if missing_object["id"] in added_missing_object_ids:
                print(f"Object {missing_object['id']} is already in the package; retrying validation.")
                continue
            print(f"Validation failed; adding missing object {missing_object['id']} of type {missing_object['type']} and retrying.")
            missing_object_info = Object(connection=myConn, id=missing_object["id"], type=missing_object["type"])
            objects4Migration.append({"id": missing_object_info.id, "type": missing_object_info.type, "name": missing_object_info.name, "date_modified": missing_object_info.date_modified})
            objects4Migration_properties.append({"id": missing_object_info.id, "type": missing_object_info.type, "name": missing_object_info.name, "location": missing_object_info.location, "date_created": missing_object_info.date_created, "date_modified": missing_object_info.date_modified})
            added_missing_object_ids.add(missing_object["id"])
            validation_retry_count += 1
            myPackageConfig = Migration.build_package_config(connection=myConn, content=objects4Migration, package_settings=myPackageSettings)
            current_migration.delete(force=True)
            package_name = f"Req_{request_id}_{df['Migration Obj@Name'].iloc[0]}_{datetime.now().strftime('%Y%m%d%H%M%S')}_retry{validation_retry_count}"
            Migration.create_object_migration(myConn, toc_view=myPackageConfig, name=package_name, project_name=df['Project@Name'].iloc[0])
            current_migration = Migration(myConn, name=package_name)
            migration_id = current_migration.id
            print("SQL: clearing migration objects after retry")
            db_instance.execute_query(query=f"delete from arms.t_mig_dim_objects where migration_key_id=(select migration_key_id from arms.t_mig_dim_request where request_id={request_id});", project_id=PROJECT_ID)
            sql_queries_insert_objects = _build_object_insert_queries(objects4Migration_properties, request_id)
            print("SQL: inserting migration objects after retry")
            db_instance.execute_query(query="\n".join(sql_queries_insert_objects), project_id=PROJECT_ID)
            sql_query_insert_migration = f"Update arms.t_mig_lu_migration_obj set migration_obj_id='{migration_id}',migration_obj_cre_time=current_timestamp WHERE migration_key_id=(select migration_key_id from arms.t_mig_dim_request where request_id={request_id});"
            print("SQL: updating migration package ID after retry")
            db_instance.execute_query(query=sql_query_insert_migration, project_id=PROJECT_ID)

        print("Validation result:")
        print({
            "status": getattr(validation_result.status, "name", str(validation_result.status)),
            "progress": validation_result.progress,
            "message": validation_result.message,
            "creation_date": _format_dt_userfriendly(validation_result.creation_date),
            "last_update_date": _format_dt_userfriendly(validation_result.last_update_date),
        })

        raw_validation_message = (validation_result.message or "").strip()
        if validation_result.status == ValidationStatus.VALIDATED:
            validation_flag = 1
            validation_message = f"Validation Success for {target_project_name}"
            validation_message_escaped = validation_message.replace("'", "''")
            sql_query_insert_validation_comment = (
                f"INSERT INTO arms.t_mig_comments(comment_id,request_id,comment_text,comment_time,comment_author,human_flag) "
                f"VALUES ((select max (comment_id) + 1 from arms.t_mig_comments),{request_id},'{validation_message_escaped}',current_timestamp,'system',0);"
            )
            print("SQL: recording validation success comment")
            db_instance.execute_query(query=sql_query_insert_validation_comment, project_id=PROJECT_ID)
        else:
            validation_flag = 0
            validation_message = f"VALIDATION FAILED for {target_project_name}"
            if raw_validation_message:
                validation_message = f"{validation_message}: {raw_validation_message}"

        sql_query_update_validation_flag = (
            f"UPDATE arms.t_mig_dim_request SET validation_flag={validation_flag} "
            f"WHERE request_id={request_id};"
        )
        print("SQL: updating validation flag")
        db_instance.execute_query(query=sql_query_update_validation_flag, project_id=PROJECT_ID)

        email_subject = f"Migration package validation result for request {request_id}"
        validation_status_text = getattr(validation_result.status, 'name', str(validation_result.status))
        email_content = "\n".join([
            "<html>",
            '    <body style="font-family: Segoe UI, Arial, sans-serif; color: #1f2937;">',
            '        <h2 style="margin-bottom: 12px;">Migration Package Validation Result</h2>',
            f'        <p style="margin-bottom: 16px;">The package workflow has finished for request <strong>{escape(str(request_id))}</strong>.</p>',
            '        <table style="border-collapse: collapse; min-width: 480px;">',
            '            <tr>',
            '                <td style="padding: 8px 12px; border: 1px solid #d1d5db; font-weight: 600;">Request ID</td>',
            f'                <td style="padding: 8px 12px; border: 1px solid #d1d5db;">{escape(str(request_id))}</td>',
            '            </tr>',
            '            <tr>',
            '                <td style="padding: 8px 12px; border: 1px solid #d1d5db; font-weight: 600;">Source project</td>',
            f'                <td style="padding: 8px 12px; border: 1px solid #d1d5db;">{escape(str(project_name))}</td>',
            '            </tr>',
            '            <tr>',
            '                <td style="padding: 8px 12px; border: 1px solid #d1d5db; font-weight: 600;">Target project</td>',
            f'                <td style="padding: 8px 12px; border: 1px solid #d1d5db;">{escape(str(target_project_name))}</td>',
            '            </tr>',
            '            <tr>',
            '                <td style="padding: 8px 12px; border: 1px solid #d1d5db; font-weight: 600;">Package name</td>',
            f'                <td style="padding: 8px 12px; border: 1px solid #d1d5db;">{escape(str(package_name))}</td>',
            '            </tr>',
            '            <tr>',
            '                <td style="padding: 8px 12px; border: 1px solid #d1d5db; font-weight: 600;">Validation status</td>',
            f'                <td style="padding: 8px 12px; border: 1px solid #d1d5db;">{escape(str(validation_status_text))}</td>',
            '            </tr>',
            '            <tr>',
            '                <td style="padding: 8px 12px; border: 1px solid #d1d5db; font-weight: 600;">Validation flag</td>',
            f'                <td style="padding: 8px 12px; border: 1px solid #d1d5db;">{escape(str(validation_flag))}</td>',
            '            </tr>',
            '            <tr>',
            '                <td style="padding: 8px 12px; border: 1px solid #d1d5db; font-weight: 600; vertical-align: top;">Details</td>',
            f'                <td style="padding: 8px 12px; border: 1px solid #d1d5db;">{escape(str(validation_message))}</td>',
            '            </tr>',
            '        </table>',
            '    </body>',
            '</html>',
        ])
        print(f"Sending email notification to: {', '.join(requestor_recipients)}")
        send_email(connection=myConn, users=requestor_recipients, subject=email_subject, content=email_content, is_html=True)

    print("Create package proces completed for Request ID: ", df['Request@ID'].iloc[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

