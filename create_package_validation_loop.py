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


def _build_object_insert_queries(object_properties, request_id):
    return [
        f"INSERT INTO arms.t_mig_dim_objects(id,type,name,location,date_created,date_modified,keyid,migration_key_id) VALUES ('{item['id']}','{item['type']}','{item['name']}','{item['location']}', '{item['date_created']}','{item['date_modified']}', (select max(keyid) +1 from arms.t_mig_dim_objects),(select migration_key_id from arms.t_mig_dim_request where request_id={request_id}));"
        for item in object_properties
    ]

print("Connecting to environment...")
# define myConn variable to establish connection to Environment and be able to reuse it later in the script
base_url = "https://env-371825.ma.cloud.microstrategy.com/MicroStrategyLibrary"
mstr_username = "mstr"
mstr_password = "EW85AcqE1SoT"
project_id = "4B0544627D49F8DA03C26D817D208263"
myConn = Connection(base_url, mstr_username, mstr_password,project_id=project_id)
db_instance = DatasourceInstance(connection=myConn, id="A23BBC514D336D5B4FCE919FE19661A3")

# Connect to dataset
request_list = Report(connection=myConn, id='A62CDD7147492E20EE83C5841B7C9183')
df = request_list.to_dataframe()
# Document structure:
#'Target Project@NAME', 'Target Project@ID', 'Status@Name', 'Status@ID','Team', 'Requestor', 'Request@ID', 'Request@TimeStamp',
#'Migration Obj@ID', 'Migration Obj@Name', 'Migration Obj@Content Path','Migration Obj@GUID', 'Project@ID', 'Project@Name'


# specify settings for a package
myPackageSettings = PackageSettings(
    Action.USE_EXISTING,
    PackageSettings.UpdateSchema.UPDATE_SCHEMA_LOGICAL_INFO,
    PackageSettings.AclOnReplacingObjects.USE_EXISTING,
    PackageSettings.AclOnNewObjects.INHERIT_ACL_AS_DEST_FOLDER
)
# mstrio expects folder paths in Unix style, e.g. /Project/Public Objects/Reports.
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
# obtain list of shortcuts located in a scpecified folder
shortcuts4Migration = Folder(myConn, id=_resolve_folder_id(myConn, shortcut_path, project_name)).get_contents()   # make sure to create this location and put some shortcuts there before You run this script

# transform list of shortcuts into a list of dictionaries, containing target object id's and object types
objects4Migration_properties=[]
objects4Migration = []
for obj in shortcuts4Migration:
    dependencies = obj.list_dependencies()
    if not dependencies:
        print(
            f"Skipping shortcut '{obj.name}' ({obj.id}): no target dependency found."
        )
        continue
    target = dependencies[0]         # a shortcut points to its first dependency
    obj_info=Object(connection = myConn, id = target['id'], type=target['type'])
    objects4Migration.append({"id": target['id'], "type": target['type'], "name": target['name'],  "date_modified": target['date_modified']})            # add a dictionary with id and type to a list objects4Migration
    objects4Migration_properties.append ({"id": obj_info.id, "type": obj_info.type, "name": obj_info.name,  "location": obj_info.location, "date_created": obj_info.date_created, "date_modified":obj_info.date_modified}) 


print (objects4Migration_properties)

myPackageConfig = Migration.build_package_config(
	connection = myConn,
	content = objects4Migration,
	package_settings = myPackageSettings
)



print ("check for existing migrations..")
#if there is no previous migration for this request
if df['Migration Obj@GUID'][0] != "":
    current_migration = Migration(myConn, id=df['Migration Obj@GUID'][0])
    current_migration.delete(force=True)

timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
package_name = f"Req_{df['Request@ID'][0]}_{df['Migration Obj@Name'][0]}_{timestamp}"
Migration.create_object_migration(myConn, 
toc_view = myPackageConfig, 
name = package_name, 
project_name = df['Project@Name'][0]
)

print("SQL: clearing existing migration objects")
db_instance.execute_query(query = f"delete from  arms.t_mig_dim_objects where migration_key_id=(select migration_key_id from arms.t_mig_dim_request where request_id={df['Request@ID'][0]});", project_id=project_id)
sql_queries_insert_objects = _build_object_insert_queries(
    objects4Migration_properties,
    request_id,
)
print("SQL: inserting migration objects")
db_instance.execute_query(
    query="\n".join(sql_queries_insert_objects),
    project_id=project_id,
)

current_migration = Migration(myConn, name=package_name)
migration_id=current_migration.id
sql_query_insert_comment=f"INSERT INTO arms.t_mig_comments(comment_id,request_id,comment_text,comment_time,comment_author,human_flag) VALUES ((select max (comment_id) + 1 from arms.t_mig_comments),{df['Request@ID'][0]},'Package Created with mstrio',current_timestamp,'system',0);"
sql_query_upd_req_table=f"update arms.t_mig_dim_request set status_id=2 where request_id={df['Request@ID'][0]};"
sql_query_insert_migration=f"Update arms.t_mig_lu_migration_obj set migration_obj_id='{migration_id}',migration_obj_cre_time=current_timestamp WHERE migration_key_id=(select migration_key_id from arms.t_mig_dim_request where request_id={df['Request@ID'][0]});"
print("SQL: recording package creation comment")
db_instance.execute_query(query = sql_query_insert_comment, project_id=project_id)
print("SQL: updating migration package ID")
db_instance.execute_query(query = sql_query_insert_migration, project_id=project_id)
print("SQL: updating request status")
db_instance.execute_query(query = sql_query_upd_req_table, project_id=project_id)

#package validation for target env retrieved from request
targetENV = Connection(base_url, mstr_username, mstr_password, project_name=target_project_name)
validation_retry_count = 0
added_missing_object_ids = set()
validation_failure_comment_written = False
while True:
    validation_trigger_result = current_migration.trigger_validation(
        target_env=targetENV,
        target_project_name=target_project_name
    )

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
            print(
                f"Validation status: {status_text}; "
                f"progress: {validation_result.progress}; "
                f"message: {validation_result.message}; "
                f"created: {_format_dt_userfriendly(validation_result.creation_date)}; "
                f"last update: {_format_dt_userfriendly(validation_result.last_update_date)}"
            )
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
        db_instance.execute_query(
            query=sql_query_insert_validation_failure_comment,
            project_id=project_id,
        )
        validation_failure_comment_written = True

    missing_object = _parse_missing_object(validation_result.message)
    if not missing_object:
        print("Could not identify a missing object ID and type; retrying validation.")
        continue

    if missing_object["id"] in added_missing_object_ids:
        print(
            f"Object {missing_object['id']} is already in the package; "
            "retrying validation."
        )
        continue

    print(
        f"Validation failed; adding missing object {missing_object['id']} "
        f"of type {missing_object['type']} and retrying."
    )
    missing_object_info = Object(
        connection=myConn,
        id=missing_object["id"],
        type=missing_object["type"],
    )
    objects4Migration.append(
        {
            "id": missing_object_info.id,
            "type": missing_object_info.type,
            "name": missing_object_info.name,
            "date_modified": missing_object_info.date_modified,
        }
    )
    print("Added missing object to package:", objects4Migration[-1])
    added_object_comment = (
        f"Added object to migration package: {missing_object_info.name} "
        f"(ID: {missing_object_info.id}, type: {missing_object_info.type})"
    )
    added_object_comment_escaped = added_object_comment.replace("'", "''")
    sql_query_insert_added_object_comment = (
        f"INSERT INTO arms.t_mig_comments(comment_id,request_id,comment_text,comment_time,comment_author,human_flag) "
        f"VALUES ((select max (comment_id) + 1 from arms.t_mig_comments),{request_id},'{added_object_comment_escaped}',current_timestamp,'system',0);"
    )
    print("SQL: recording added object comment")
    db_instance.execute_query(
        query=sql_query_insert_added_object_comment,
        project_id=project_id,
    )
    objects4Migration_properties.append(
        {
            "id": missing_object_info.id,
            "type": missing_object_info.type,
            "name": missing_object_info.name,
            "location": missing_object_info.location,
            "date_created": missing_object_info.date_created,
            "date_modified": missing_object_info.date_modified,
        }
    )
    added_missing_object_ids.add(missing_object["id"])
    validation_retry_count += 1

    myPackageConfig = Migration.build_package_config(
        connection=myConn,
        content=objects4Migration,
        package_settings=myPackageSettings,
    )
    current_migration.delete(force=True)
    package_name = (
        f"Req_{request_id}_{df['Migration Obj@Name'][0]}_"
        f"{datetime.now().strftime('%Y%m%d%H%M%S')}_retry{validation_retry_count}"
    )
    Migration.create_object_migration(
        myConn,
        toc_view=myPackageConfig,
        name=package_name,
        project_name=df['Project@Name'][0],
    )
    current_migration = Migration(myConn, name=package_name)
    migration_id = current_migration.id
    print("SQL: clearing migration objects after retry")
    db_instance.execute_query(
        query=f"delete from arms.t_mig_dim_objects where migration_key_id=(select migration_key_id from arms.t_mig_dim_request where request_id={request_id});",
        project_id=project_id,
    )
    sql_queries_insert_objects = _build_object_insert_queries(
        objects4Migration_properties,
        request_id,
    )
    print("SQL: inserting migration objects after retry")
    db_instance.execute_query(
        query="\n".join(sql_queries_insert_objects),
        project_id=project_id,
    )
    sql_query_insert_migration = (
        f"Update arms.t_mig_lu_migration_obj set migration_obj_id='{migration_id}',"
        f"migration_obj_cre_time=current_timestamp WHERE migration_key_id="
        f"(select migration_key_id from arms.t_mig_dim_request where request_id={request_id});"
    )
    print("SQL: updating migration package ID after retry")
    db_instance.execute_query(query=sql_query_insert_migration, project_id=project_id)

print("Validation result:")
print(
    {
        "status": getattr(validation_result.status, "name", str(validation_result.status)),
        "progress": validation_result.progress,
        "message": validation_result.message,
        "creation_date": _format_dt_userfriendly(validation_result.creation_date),
        "last_update_date": _format_dt_userfriendly(validation_result.last_update_date),
    }
)

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
    db_instance.execute_query(query=sql_query_insert_validation_comment, project_id=project_id)
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
db_instance.execute_query(query=sql_query_update_validation_flag, project_id=project_id)

print("Create package proces completed for Request ID: ", df['Request@ID'][0])

