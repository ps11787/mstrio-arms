"""List MicroStrategy projects and insert them into arms.t_mig_lu_project via ARMS_WH.

Connects to a MicroStrategy environment, lists all projects, and (optionally) inserts
one row per project into arms.t_mig_lu_project using DatasourceInstance.execute_query()
against the ARMS_WH datasource. No direct warehouse DB credentials are required since
the query is executed through the MicroStrategy REST API.

Example:
    python list_projects_and_insert.py
    python list_projects_and_insert.py --insert
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Sequence

from mstrio.connection import Connection
from mstrio.datasources.datasource_instance import DatasourceInstance
from mstrio.server.project import list_projects


DEFAULT_BASE_URL = "https://env-371825.ma.cloud.microstrategy.com/MicroStrategyLibrary"
DEFAULT_USERNAME = "mstr"
DEFAULT_PASSWORD = "EW85AcqE1SoT"
DEFAULT_LOGIN_MODE = 1
DEFAULT_DATASOURCE_NAME = "ARMS_WH"
DEFAULT_TABLE_NAME = "arms.t_mig_lu_project"
DEFAULT_TGT_TABLE_NAME = "arms.t_mig_lu_tgt_project"
DEFAULT_STATUS_TABLE_NAME = "arms.t_mig_lu_status"
STATUS_LOOKUP = [
    (1, "Create Package"),
    (2, "Package Created"),
    (3, "Ready for Migrate"),
    (4, "Migrated"),
]
DEFAULT_MIGRATION_OBJ_TABLE_NAME = "arms.t_mig_lu_migration_obj"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List MicroStrategy projects and insert them into ARMS_WH.t_mig_lu_project."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--login-mode", type=int, default=DEFAULT_LOGIN_MODE)
    parser.add_argument("--ssl-verify", action="store_true", default=True)
    parser.add_argument("--datasource-name", default=DEFAULT_DATASOURCE_NAME)
    parser.add_argument(
        "--query-project-id",
        help="Project ID used as execution context for the ARMS_WH query. Defaults to first listed project.",
    )
    parser.add_argument(
        "--insert",
        action="store_true",
        help="Execute INSERT statements. Without this flag, only project listing runs.",
    )
    return parser.parse_args(argv)


def _escape_sql_literal(value: str) -> str:
    return value.replace("'", "''")


def _get_existing_project_guids(
    datasource: DatasourceInstance, query_project_id: str
) -> tuple[set[str], int]:
    """Return (existing project_guid values, max project_id) from t_mig_lu_project."""
    result = datasource.execute_query(
        project_id=query_project_id,
        query=f"SELECT project_id, project_guid FROM {DEFAULT_TABLE_NAME}",
    )
    data = result.get("results", {}).get("data", {})
    guids = {str(guid) for guid in data.get("project_guid", [])}
    ids = [int(value) for value in data.get("project_id", [])]
    return guids, (max(ids) if ids else 0)


def _build_insert_statement(project: dict[str, Any], project_id: int) -> str:
    # TODO: column mapping for env_id/project_source_id/etc. is not defined yet.
    guid = _escape_sql_literal(str(project.get("id", "")))
    name = _escape_sql_literal(str(project.get("name", "")))
    return (
        f"INSERT INTO {DEFAULT_TABLE_NAME} (project_id, project_guid, project_name) "
        f"VALUES ({project_id}, '{guid}', '{name}')"
    )


def _build_tgt_insert_statement(project: dict[str, Any], project_id: int) -> str:
    # TODO: column mapping for tgt_env_id/project_source_id/etc. is not defined yet.
    guid = _escape_sql_literal(str(project.get("id", "")))
    name = _escape_sql_literal(str(project.get("name", "")))
    return (
        f"INSERT INTO {DEFAULT_TGT_TABLE_NAME} (tgt_project_id, tgt_project_guid, tgt_project_name) "
        f"VALUES ({project_id}, '{guid}', '{name}')"
    )


def _get_existing_status_ids(
    datasource: DatasourceInstance, query_project_id: str
) -> set[int]:
    result = datasource.execute_query(
        project_id=query_project_id,
        query=f"SELECT status_id FROM {DEFAULT_STATUS_TABLE_NAME}",
    )
    data = result.get("results", {}).get("data", {})
    return {int(value) for value in data.get("status_id", [])}


def _build_status_insert_statement(status_id: int, status_name: str) -> str:
    name = _escape_sql_literal(status_name)
    return (
        f"INSERT INTO {DEFAULT_STATUS_TABLE_NAME} (status_id, status_name) "
        f"VALUES ({status_id}, '{name}')"
    )


def _migration_obj_id_exists(
    datasource: DatasourceInstance, query_project_id: str, migration_obj_id: str
) -> bool:
    result = datasource.execute_query(
        project_id=query_project_id,
        query=f"SELECT migration_obj_id FROM {DEFAULT_MIGRATION_OBJ_TABLE_NAME}",
    )
    data = result.get("results", {}).get("data", {})
    existing_ids = {str(value) for value in data.get("migration_obj_id", [])}
    return migration_obj_id in existing_ids


def _build_migration_obj_insert_statement(migration_obj_id: str, migration_obj_name: str) -> str:
    obj_id = _escape_sql_literal(migration_obj_id)
    name = _escape_sql_literal(migration_obj_name)
    return (
        f"INSERT INTO {DEFAULT_MIGRATION_OBJ_TABLE_NAME} (migration_obj_id, migration_obj_name) "
        f"VALUES ('{obj_id}', '{name}')"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    connection = Connection(
        base_url=args.base_url,
        username=args.username,
        password=args.password,
        login_mode=args.login_mode,
        ssl_verify=args.ssl_verify,
    )
    connection.connect()

    try:
        projects = list_projects(connection, to_dictionary=True)
        if not projects:
            print("No projects found.")
            return 0

        print(f"Found {len(projects)} project(s):")
        for project in projects:
            print(f"  - {project.get('name')} (id={project.get('id')})")

        if not args.insert:
            print("\n--insert not specified: skipping ARMS_WH insert step.")
            return 0

        query_project_id = args.query_project_id or projects[0]["id"]
        datasource = DatasourceInstance(connection=connection, name=args.datasource_name)

        existing_status_ids = _get_existing_status_ids(datasource, query_project_id)
        new_statuses = [
            (status_id, status_name)
            for status_id, status_name in STATUS_LOOKUP
            if status_id not in existing_status_ids
        ]
        if new_statuses:
            print(f"\nPopulating {DEFAULT_STATUS_TABLE_NAME}...")
            for status_id, status_name in new_statuses:
                status_insert_sql = _build_status_insert_statement(status_id, status_name)
                print(f"[SQL] {status_insert_sql}")
                status_result = datasource.execute_query(
                    project_id=query_project_id, query=status_insert_sql
                )
                print(f"  -> execution status: {status_result.get('status')}")
        else:
            print(f"\nAll statuses already exist in {DEFAULT_STATUS_TABLE_NAME}.")

        existing_guids, max_project_id = _get_existing_project_guids(datasource, query_project_id)
        new_projects = [
            project for project in projects if str(project.get("id", "")) not in existing_guids
        ]
        if not new_projects:
            print("\nAll projects already exist in t_mig_lu_project. Nothing to insert.")
            return 0

        print(f"\nExecuting inserts against datasource '{args.datasource_name}' "
              f"in project context '{query_project_id}'...")
        for offset, project in enumerate(new_projects, start=1):
            project_id = max_project_id + offset
            insert_sql = _build_insert_statement(project, project_id)
            print(f"[SQL] {insert_sql}")
            result = datasource.execute_query(project_id=query_project_id, query=insert_sql)
            print(f"  -> execution status: {result.get('status')}")

            tgt_insert_sql = _build_tgt_insert_statement(project, project_id)
            print(f"[SQL] {tgt_insert_sql}")
            tgt_result = datasource.execute_query(project_id=query_project_id, query=tgt_insert_sql)
            print(f"  -> execution status: {tgt_result.get('status')}")

        if _migration_obj_id_exists(datasource, query_project_id, "1"):
            print(f"\nDummy migration_obj_id '1' already exists in {DEFAULT_MIGRATION_OBJ_TABLE_NAME}.")
        else:
            migration_obj_insert_sql = _build_migration_obj_insert_statement("1", "Dummy")
            print(f"\n[SQL] {migration_obj_insert_sql}")
            migration_obj_result = datasource.execute_query(
                project_id=query_project_id, query=migration_obj_insert_sql
            )
            print(f"  -> execution status: {migration_obj_result.get('status')}")

        print("\nDone.")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
