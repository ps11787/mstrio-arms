"""Importiert ein vorhandenes MicroStrategy-Migration-Package.

Das Skript nutzt das gleiche Grundmuster wie das Erstellen des Pakets:
- Verbindung zur MicroStrategy-Umgebung
- Laden eines Reports mit den Request-Parametern
- Auslesen der Migration-Objekt-ID und des Zielprojekts
- Import des Migration-Pakets in das Zielprojekt

Hinweis:
Die genaue Import-Logik hängt von der MicroStrategy-Umgebung und der vorhandenen
Migration ab. Dieses Skript richtet sich nach der in mstrio-py vorhandenen
Migration.migrate()-API.
"""

from __future__ import annotations

from mstrio.connection import Connection
from mstrio.datasources.datasource_instance import DatasourceInstance
from mstrio.object_management.migration import Migration
from mstrio.project_objects.report import Report


BASE_URL = "https://env-371825.ma.cloud.microstrategy.com/MicroStrategyLibrary"
MSTR_USERNAME = ""
MSTR_PASSWORD = ""
PROJECT_ID = "4B0544627D49F8DA03C26D817D208263"
REQUEST_REPORT_ID = "A62CDD7147492E20EE83C5841B7C9183"
DATASOURCE_INSTANCE_ID = "A23BBC514D336D5B4FCE919FE19661A3"
TARGET_PROJECT_TABLE = "arms.t_mig_lu_tgt_project"


def _get_target_project_status_id(datasource: DatasourceInstance, target_project_name: str) -> int:
    """Return the status_id for the target project from the ARMS lookup table."""
    escaped_name = target_project_name.replace("'", "''")
    query = (
        f"SELECT status_id FROM {TARGET_PROJECT_TABLE} "
        f"WHERE tgt_project_name = '{escaped_name}' "
        "ORDER BY status_id DESC"
    )
    result = datasource.execute_query(project_id=PROJECT_ID, query=query)
    data = result.get("results", {}).get("data", {})
    values = data.get("status_id", [])
    if not values:
        raise ValueError(
            f"No status_id found in {TARGET_PROJECT_TABLE} for target project '{target_project_name}'."
        )
    return int(values[0])


def main() -> int:
    print("Connecting to environment...")

    my_conn = Connection(
        BASE_URL,
        MSTR_USERNAME,
        MSTR_PASSWORD,
        project_id=PROJECT_ID,
    )
    db_instance = DatasourceInstance(connection=my_conn, id=DATASOURCE_INSTANCE_ID)

    # Request-Report wie im Package-Erstellungs-Skript lesen.
    request_list = Report(connection=my_conn, id=REQUEST_REPORT_ID)
    df = request_list.to_dataframe()

    if df.empty:
        raise ValueError("No rows found in the migration request report.")

    request_row = df.iloc[0]
    request_id = request_row.get("Request@ID")
    migration_obj_guid = str(request_row.get("Migration Obj@GUID", "")).strip()
    target_project_name = str(request_row.get("Target Project@NAME", "")).strip()
    source_project_name = str(request_row.get("Project@Name", "")).strip()

    if not migration_obj_guid:
        raise ValueError(
            f"Request {request_id} does not contain a Migration Obj@GUID; "
            "import cannot start."
        )

    if not target_project_name:
        raise ValueError(
            f"Request {request_id} does not contain a Target Project@NAME; "
            "import cannot start."
        )

    print(
        "Retrieved migration parameters:\n"
        f"  Request ID: {request_id}\n"
        f"  Source project: {source_project_name}\n"
        f"  Target project: {target_project_name}\n"
        f"  Migration object GUID: {migration_obj_guid}"
    )

    # Ziel-Umgebung mit dem Zielprojekt verbinden.
    target_env = Connection(
        BASE_URL,
        MSTR_USERNAME,
        MSTR_PASSWORD,
        project_name=target_project_name,
    )

    try:
        current_migration = Migration(my_conn, id=migration_obj_guid)
        print("Starting package import...")

        # mstrio Migration API: migrate(self, target_env, target_project=None,
        # target_project_id=None, target_project_name=None, generate_undo=True)
        import_result = current_migration.migrate(
            target_env=target_env,
            target_project_name=target_project_name,
            generate_undo=False,
        )

        print("Import result:")
        print(import_result)

        target_status_id = _get_target_project_status_id(db_instance, target_project_name)
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
        target_env.close()
        my_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
