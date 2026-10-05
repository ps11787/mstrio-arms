import os
from mstrio.object_management.object import Object
from mstrio.datasources.datasource_instance import DatasourceInstance
from mstrio.project_objects.report import Report
from mstrio.object_management.folder import Folder, get_folder_id_from_path, list_folders
from mstrio.object_management.migration.package import Action, ValidationStatus
from mstrio.object_management.migration import PackageSettings, Migration
from mstrio.types import ObjectTypes
from datetime import datetime
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
    text = str(value).split(".")[-1] if "." in str(value) else str(value)
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _is_schema_object_type(obj_type):
    normalized = _normalize_object_type_name(obj_type)
    return normalized in {"schema", "schema_definition", "project_schema"}


def _required_dependency_types():
    return {
        "metric",
        "agg_metric",
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
    normalized = _normalize_object_type_name(obj_type)
    return normalized in _required_dependency_types()


def _is_builtin_system_dependency(dep):
    if dep is None:
        return True

    dep_name = str(dep.get("name") or "").strip()
    dep_type = str(dep.get("type") or "").strip()
    dep_id = str(dep.get("id") or "").strip()
    if not dep_id or not dep_type:
        return True

    if dep_name in {"*", ">", "DSS Built-in Package"}:
        return True
    if dep_name.lower().startswith("dss built-in"):
        return True
    if dep_name and dep_type.lower() in {"folder", "package", "project"} and set(dep_name) <= {"*", ">"}:
        return True

    return False


def _collect_package_dependencies(shortcut_objects, connection=None):
    collected = []
    seen_ids = set()

    def add_dependency(dep, *, include_if_required=True):
        if _is_builtin_system_dependency(dep):
            return

        dep_id = dep.get("id")
        dep_type = dep.get("type")
        if not dep_id or dep_id in seen_ids:
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

    def walk_object_dependencies(target_obj):
        if target_obj is None:
            return

        try:
            dependencies = target_obj.list_dependencies() or []
        except Exception:
            return

        for dependency in dependencies:
            if _is_builtin_system_dependency(dependency):
                continue

            dep_id = dependency.get("id")
            dep_type = dependency.get("type")
            if not dep_id:
                continue

            dep_obj = None
            if connection is not None and dep_type:
                try:
                    dep_obj = Object(connection=connection, id=dep_id, type=dep_type)
                except Exception:
                    dep_obj = None

            if dep_type is not None and _is_required_dependency_type(dep_type):
                add_dependency(dependency, include_if_required=True)
            elif dep_obj is not None:
                walk_object_dependencies(dep_obj)

            if dep_obj is not None:
                walk_object_dependencies(dep_obj)

    for shortcut in shortcut_objects or []:
        for dependency in shortcut.list_dependencies() or []:
            if _is_builtin_system_dependency(dependency):
                continue

            dep_id = dependency.get("id")
            dep_type = dependency.get("type")
            if not dep_id:
                continue

            dep_obj = None
            if connection is not None and dep_type:
                try:
                    dep_obj = Object(connection=connection, id=dep_id, type=dep_type)
                except Exception:
                    dep_obj = None

            if dep_type is not None and _is_required_dependency_type(dep_type):
                add_dependency(dependency, include_if_required=True)
            elif dep_obj is not None:
                walk_object_dependencies(dep_obj)

            if dep_obj is not None:
                walk_object_dependencies(dep_obj)

    return collected


def _parse_missing_object(validation_message):
    if not validation_message:
        return None

    object_match = re.search(
        r"Object:\s*<(?P<type>[^>]+?)\s+id=(?P<id>[0-9A-Fa-f]{32})>\s+is\s+not\s+found",
        validation_message,
        re.IGNORECASE,
    )
    if not object_match:
        return _parse_missing_object_legacy(validation_message)

    object_type_name = re.sub(r"[^A-Za-z0-9]+", "_", object_match.group("type")).strip("_")
    object_type = getattr(ObjectTypes, object_type_name.upper(), None)
    if object_type is None:
        return None

    return {
        "id": object_match.group("id"),
        "type": object_type,
    }


def _parse_missing_object_legacy(validation_message):
    """Parse older validation messages that expose ID and type separately."""
    object_id_match = re.search(r"\b[0-9A-Fa-f]{32}\b", validation_message)
    object_type_match = re.search(
        r"(?:object\s+type|type)\s*[:=]\s*['\"]?([A-Za-z_][A-Za-z0-9_-]*|\d+)",
        validation_message,
        re.IGNORECASE,
    )
    if not object_type_match:
        object_type_match = re.search(
            r"\b(?:type|object)\s+([A-Za-z_][A-Za-z0-9_-]*|\d+)\b",
            validation_message,
            re.IGNORECASE,
        )
    if not object_id_match or not object_type_match:
        return None

    object_type = object_type_match.group(1)
    if object_type.isdigit():
        object_type = int(object_type)
    else:
        object_type = getattr(ObjectTypes, object_type.upper(), object_type)

    return {
        "id": object_id_match.group(0),
        "type": object_type,
    }


def _build_object_insert_queries(object_properties, request_id):
    return [
        f"INSERT INTO arms.t_mig_dim_objects(id,type,name,location,date_created,date_modified,keyid,migration_key_id) VALUES ('{item['id']}','{item['type']}','{item['name']}','{item['location']}', '{item['date_created']}','{item['date_modified']}', (select max(keyid) +1 from arms.t_mig_dim_objects),(select migration_key_id from arms.t_mig_dim_request where request_id={request_id}));"
        for item in object_properties
    ]


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


def main() -> int:
    print("Connecting to environment...")
    myConn = Connection(BASE_URL, MSTR_USERNAME, MSTR_PASSWORD, project_id=PROJECT_ID)
    db_instance = DatasourceInstance(connection=myConn, id=DATASOURCE_INSTANCE_ID)

    request_list = Report(connection=myConn, id=REQUEST_REPORT_ID)
    df = request_list.to_dataframe()

    myPackageSettings = PackageSettings(
        Action.REPLACE,
        PackageSettings.UpdateSchema.UPDATE_SCHEMA_LOGICAL_INFO,
        PackageSettings.AclOnReplacingObjects.USE_EXISTING,
        PackageSettings.AclOnNewObjects.INHERIT_ACL_AS_DEST_FOLDER,
    )
    project_name = str(df['Project@Name'][0]).strip()
    migration_content_path = str(df['Migration Obj@Content Path'][0])
    shortcut_path = _normalize_mstr_folder_path(project_name, migration_content_path)
    request_id = df['Request@ID'][0]
    target_project_name = str(df['Target Project@NAME'][0]).strip()
    print(
        "Retrieved migration parameters:\n"
        f"  Request ID: {request_id}\n"
        f"  Source project: {project_name}\n"
        f"  Target project: {target_project_name}\n"
        f"  Shortcut path: {shortcut_path}"
    )
    shortcuts4Migration = Folder(myConn, id=_resolve_folder_id(myConn, shortcut_path, project_name)).get_contents()

    objects4Migration_properties = []
    objects4Migration = []
    seen_object_ids = set()

    for obj in shortcuts4Migration:
        dependencies = obj.list_dependencies() or []
        if not dependencies:
            print(f"Skipping shortcut '{obj.name}' ({obj.id}): no target dependency found.")
            continue

        for dependency in dependencies:
            dep_id = dependency.get("id")
            dep_type = dependency.get("type")
            if not dep_id or dep_id in seen_object_ids:
                continue

            if dep_type is not None and _is_required_dependency_type(dep_type):
                dep_obj = Object(connection=myConn, id=dep_id, type=dep_type)
                objects4Migration.append({"id": dep_id, "type": dep_type, "name": dependency.get("name") or dep_obj.name, "date_modified": getattr(dep_obj, "date_modified", dependency.get("date_modified"))})
                objects4Migration_properties.append({"id": dep_obj.id, "type": dep_obj.type, "name": dep_obj.name, "location": getattr(dep_obj, "location", None), "date_created": getattr(dep_obj, "date_created", None), "date_modified": dep_obj.date_modified})
                seen_object_ids.add(dep_id)
                continue

            target = dependency
            obj_info = Object(connection=myConn, id=target['id'], type=target['type'])
            objects4Migration.append({"id": target['id'], "type": target['type'], "name": target.get('name') or obj_info.name, "date_modified": target.get('date_modified') or obj_info.date_modified})
            objects4Migration_properties.append({"id": obj_info.id, "type": obj_info.type, "name": obj_info.name, "location": obj_info.location, "date_created": obj_info.date_created, "date_modified": obj_info.date_modified})
            seen_object_ids.add(target['id'])

            try:
                target_dependencies = obj_info.list_dependencies() or []
            except Exception:
                target_dependencies = []

            for target_dependency in target_dependencies:
                added_dep_id = target_dependency.get("id")
                added_dep_type = target_dependency.get("type")
                if not added_dep_id or added_dep_id in seen_object_ids:
                    continue
                if not _is_required_dependency_type(added_dep_type):
                    continue

                added_dep_obj = Object(connection=myConn, id=added_dep_id, type=added_dep_type)
                objects4Migration.append({"id": added_dep_id, "type": added_dep_type, "name": target_dependency.get("name") or added_dep_obj.name, "date_modified": getattr(added_dep_obj, "date_modified", target_dependency.get("date_modified"))})
                objects4Migration_properties.append({"id": added_dep_obj.id, "type": added_dep_obj.type, "name": added_dep_obj.name, "location": getattr(added_dep_obj, "location", None), "date_created": getattr(added_dep_obj, "date_created", None), "date_modified": added_dep_obj.date_modified})
                seen_object_ids.add(added_dep_id)

    extra_dependencies = _collect_package_dependencies(shortcuts4Migration, connection=myConn)
    for dependency in extra_dependencies:
        dep_id = dependency["id"]
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

    myPackageConfig = Migration.build_package_config(
        connection=myConn,
        content=objects4Migration,
        package_settings=myPackageSettings,
    )

    print("check for existing migrations..")
    migration_guid = str(df['Migration Obj@GUID'][0]).strip() if 'Migration Obj@GUID' in df.columns else ""
    _delete_existing_migration_if_present(myConn, migration_guid, force=True)

    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    package_name = f"Req_{df['Request@ID'][0]}_{df['Migration Obj@Name'][0]}_{timestamp}"
    Migration.create_object_migration(myConn, toc_view=myPackageConfig, name=package_name, project_name=df['Project@Name'][0])

    print("SQL: clearing existing migration objects")
    db_instance.execute_query(query=f"delete from arms.t_mig_dim_objects where migration_key_id=(select migration_key_id from arms.t_mig_dim_request where request_id={df['Request@ID'][0]});", project_id=PROJECT_ID)
    sql_queries_insert_objects = _build_object_insert_queries(objects4Migration_properties, request_id)
    print("SQL: inserting migration objects")
    db_instance.execute_query(query="\n".join(sql_queries_insert_objects), project_id=PROJECT_ID)

    current_migration = Migration(myConn, name=package_name)
    migration_id = current_migration.id
    sql_query_insert_comment = f"INSERT INTO arms.t_mig_comments(comment_id,request_id,comment_text,comment_time,comment_author,human_flag) VALUES ((select max (comment_id) + 1 from arms.t_mig_comments),{df['Request@ID'][0]},'Package Created with mstrio',current_timestamp,'system',0);"
    sql_query_upd_req_table = f"update arms.t_mig_dim_request set status_id=2 where request_id={df['Request@ID'][0]};"
    sql_query_insert_migration = f"Update arms.t_mig_lu_migration_obj set migration_obj_id='{migration_id}',migration_obj_cre_time=current_timestamp WHERE migration_key_id=(select migration_key_id from arms.t_mig_dim_request where request_id={df['Request@ID'][0]});"
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
            package_name = f"Req_{request_id}_{df['Migration Obj@Name'][0]}_{datetime.now().strftime('%Y%m%d%H%M%S')}_retry{validation_retry_count}"
            Migration.create_object_migration(myConn, toc_view=myPackageConfig, name=package_name, project_name=df['Project@Name'][0])
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

    print("Create package proces completed for Request ID: ", df['Request@ID'][0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

