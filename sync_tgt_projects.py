from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence

from mstrio.connection import Connection
from mstrio.datasources.datasource_instance import DatasourceInstance
from mstrio.server.project import list_projects


DEFAULT_BASE_URL = "https://env-371825.ma.cloud.microstrategy.com/MicroStrategyLibrary"
DEFAULT_USERNAME = "mstr"
DEFAULT_PASSWORD = os.getenv("MSTR_PASSWORD", "")
DEFAULT_PROJECT_ID = "4B0544627D49F8DA03C26D817D208263"
DEFAULT_DATASOURCE_ID = "A23BBC514D336D5B4FCE919FE19661A3"
DEFAULT_TGT_TABLE = "arms.t_mig_lu_tgt_project"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Synchronize server projects into arms.t_mig_lu_tgt_project by project GUID. "
            "Default mode is dry-run; use --apply to write changes."
        )
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--datasource-id", default=DEFAULT_DATASOURCE_ID)
    parser.add_argument("--table", default=DEFAULT_TGT_TABLE)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute INSERT/UPDATE statements. Without this flag, only preview changes.",
    )
    return parser.parse_args(argv)


def _escape_sql_literal(value: str) -> str:
    return value.replace("'", "''")


def _fetch_existing_rows(
    datasource: DatasourceInstance,
    project_id: str,
    table_name: str,
) -> tuple[dict[str, dict[str, str | int]], int]:
    result = datasource.execute_query(
        project_id=project_id,
        query=(
            f"SELECT tgt_project_id, tgt_project_guid, tgt_project_name "
            f"FROM {table_name}"
        ),
    )
    data = result.get("results", {}).get("data", {})

    ids = data.get("tgt_project_id", [])
    guids = data.get("tgt_project_guid", [])
    names = data.get("tgt_project_name", [])

    rows: dict[str, dict[str, str | int]] = {}
    max_id = 0

    for i in range(len(guids)):
        guid = str(guids[i]).strip()
        if not guid:
            continue

        name = "" if i >= len(names) or names[i] is None else str(names[i]).strip()
        pid = int(ids[i]) if i < len(ids) and ids[i] is not None else 0

        max_id = max(max_id, pid)
        rows[guid] = {"id": pid, "name": name}

    return rows, max_id


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    connection = Connection(
        base_url=args.base_url,
        username=args.username,
        password=args.password,
        project_id=args.project_id,
    )
    connection.connect()

    try:
        datasource = DatasourceInstance(connection=connection, id=args.datasource_id)

        projects = list_projects(connection, to_dictionary=True)
        server_by_guid = {
            str(project.get("id", "")).strip(): str(project.get("name", "")).strip()
            for project in projects
            if project.get("id")
        }

        existing_rows, max_id = _fetch_existing_rows(
            datasource,
            args.project_id,
            args.table,
        )

        inserts: list[tuple[int, str, str]] = []
        updates: list[tuple[str, str, str]] = []
        unchanged = 0
        next_id = max_id

        for guid, name in sorted(server_by_guid.items(), key=lambda item: item[1].lower()):
            row = existing_rows.get(guid)
            if row is None:
                next_id += 1
                inserts.append((next_id, guid, name))
            elif str(row.get("name", "")) != name:
                updates.append((guid, name, str(row.get("name", ""))))
            else:
                unchanged += 1

        print("SYNC SUMMARY")
        print(f"- Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
        print(f"- Server projects: {len(server_by_guid)}")
        print(f"- Existing target rows before sync: {len(existing_rows)}")
        print(f"- Pending inserts: {len(inserts)}")
        print(f"- Pending name updates: {len(updates)}")
        print(f"- Unchanged matches: {unchanged}")

        if inserts:
            print("\nINSERTS")
            for pid, guid, name in inserts:
                print(f"- id={pid} guid={guid} name={name}")

        if updates:
            print("\nUPDATES")
            for guid, new_name, old_name in updates:
                print(f"- guid={guid} old='{old_name}' new='{new_name}'")

        if not args.apply:
            print("\nDry-run finished. Re-run with --apply to execute changes.")
            return 0

        for pid, guid, name in inserts:
            insert_sql = (
                f"INSERT INTO {args.table} (tgt_project_id, tgt_project_guid, tgt_project_name) "
                f"VALUES ({pid}, '{_escape_sql_literal(guid)}', '{_escape_sql_literal(name)}')"
            )
            datasource.execute_query(project_id=args.project_id, query=insert_sql)

        for guid, new_name, _old_name in updates:
            update_sql = (
                f"UPDATE {args.table} "
                f"SET tgt_project_name='{_escape_sql_literal(new_name)}' "
                f"WHERE tgt_project_guid='{_escape_sql_literal(guid)}'"
            )
            datasource.execute_query(project_id=args.project_id, query=update_sql)

        count_result = datasource.execute_query(
            project_id=args.project_id,
            query=f"SELECT COUNT(*) AS cnt FROM {args.table}",
        )
        final_count = count_result.get("results", {}).get("data", {}).get("cnt", [None])[0]

        print("\nAPPLY RESULT")
        print(f"- Inserted: {len(inserts)}")
        print(f"- Updated: {len(updates)}")
        print(f"- Final target row count: {final_count}")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
