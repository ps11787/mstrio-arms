from mstrio.object_management.object import Object
from mstrio.datasources.datasource_instance import DatasourceInstance
from mstrio.project_objects.report import Report
from mstrio.object_management.folder import Folder, get_folder_id_from_path, list_folders
from mstrio.object_management.migration.package import Action, ValidationStatus
from mstrio.object_management.migration import PackageSettings, Migration
from datetime import datetime
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
print(shortcut_path)
# obtain list of shortcuts located in a scpecified folder
shortcuts4Migration = Folder(myConn, id=_resolve_folder_id(myConn, shortcut_path, project_name)).get_contents()   # make sure to create this location and put some shortcuts there before You run this script

# transform list of shortcuts into a list of dictionaries, containing target object id's and object types
objects4Migration_properties=[]
objects4Migration = []
for obj in shortcuts4Migration:
    target = obj.list_dependencies()[0]         # for each object, list its dependencies, and select the first result (Shortcut can point to only one target, so there is always one dependent)
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

db_instance.execute_query(query = f"delete from  arms.t_mig_dim_objects where migration_key_id=(select migration_key_id from arms.t_mig_dim_request where request_id={df['Request@ID'][0]});", project_id=project_id)
for item in objects4Migration_properties:
    sql_query_insert_objects = f"INSERT INTO arms.t_mig_dim_objects(id,type,name,location,date_created,date_modified,keyid,migration_key_id) VALUES ('{item['id']}','{item['type']}','{item['name']}','{item['location']}', '{item['date_created']}','{item['date_modified']}', (select max(keyid) +1 from arms.t_mig_dim_objects),(select migration_key_id from arms.t_mig_dim_request where request_id={df['Request@ID'][0]}));"
    db_instance.execute_query(query=sql_query_insert_objects, project_id=project_id)

current_migration = Migration(myConn, name=package_name)
migration_id=current_migration.id
sql_query_insert_comment=f"INSERT INTO arms.t_mig_comments(comment_id,request_id,comment_text,comment_time,comment_author,human_flag) VALUES ((select max (comment_id) + 1 from arms.t_mig_comments),{df['Request@ID'][0]},'Package Created with mstrio',current_timestamp,'system',0);"
sql_query_upd_req_table=f"update arms.t_mig_dim_request set status_id=2 where request_id={df['Request@ID'][0]};"
sql_query_insert_migration=f"Update arms.t_mig_lu_migration_obj set migration_obj_id='{migration_id}',migration_obj_cre_time=current_timestamp WHERE migration_key_id=(select migration_key_id from arms.t_mig_dim_request where request_id={df['Request@ID'][0]});"
db_instance.execute_query(query = sql_query_insert_comment, project_id=project_id)
db_instance.execute_query(query = sql_query_insert_migration, project_id=project_id)
db_instance.execute_query(query = sql_query_upd_req_table, project_id=project_id)

#package validation for target env retrieved from request
target_project_name = str(df['Target Project@NAME'][0]).strip()
targetENV = Connection(base_url, mstr_username, mstr_password, project_name=target_project_name)
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
    validation_message = f"Validation Success for {target_project_name}"
else:
    validation_message = f"VALIDATION FAILED for {target_project_name}"
    if raw_validation_message:
        validation_message = f"{validation_message}: {raw_validation_message}"

validation_message_escaped = str(validation_message).replace("'", "''")
sql_query_insert_validation_comment = (
    f"INSERT INTO arms.t_mig_comments(comment_id,request_id,comment_text,comment_time,comment_author,human_flag) "
    f"VALUES ((select max (comment_id) + 1 from arms.t_mig_comments),{df['Request@ID'][0]},'{validation_message_escaped}',current_timestamp,'system',0);"
)
db_instance.execute_query(query=sql_query_insert_validation_comment, project_id=project_id)
