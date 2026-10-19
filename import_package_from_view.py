"""Import a package using one initial ARMS view read and existing migration helpers."""

from __future__ import annotations

from datetime import datetime
from time import monotonic, sleep

import import_package as app


INPUT_VIEW = "arms.v_mig_import_input"
ROLLBACK_REQUEST_STATUS = 18
ROLLBACK_SUCCESS_STATUS = 19
ROLLBACK_FAILED_STATUS = 11
INPUT_COLUMNS = (
    "request_id", "request_status_id", "source_project_name", "migration_key_id",
    "migration_obj_id", "tgt_env_id", "target_env_url", "target_project_name",
    "tgt_project_id", "status_id_success", "status_id_failed",
    "resolution_failed_status_id", "latest_migration_id", "resolution_error", "is_ready",
)
RESOLUTION_FAILURE_COMMENT = (
    "Unable to resolve matchine Target Project name. "
    "Please check Environment/Status/Project mapping."
)


def _text(row: dict[str, object], column: str) -> str | None:
    return app._get_first_value_from_result_data(row, column)


def _required_text(row: dict[str, object], column: str) -> str:
    value = _text(row, column)
    if value is None:
        raise ValueError(f"Import input is missing {column}.")
    return value


def _integer(row: dict[str, object], column: str) -> int:
    try:
        return app._coerce_int(_required_text(row, column))
    except ValueError as exc:
        raise ValueError(f"Invalid {column} in import input: {exc}") from exc


def _is_ready(row: dict[str, object]) -> bool:
    value = _required_text(row, "is_ready").lower()
    if value in {"true", "t"}:
        return True
    if value in {"false", "f"}:
        return False
    number = app._coerce_int(value)
    if number not in (0, 1):
        raise ValueError(f"Invalid is_ready value in import input: {value!r}.")
    return bool(number)


def _execute(datasource: app.DatasourceInstance, query: str) -> dict:
    return app._execute_query(datasource, project_id=app.PROJECT_ID, query=query)


def _fetch_inputs(datasource: app.DatasourceInstance) -> list[dict[str, object]]:
    result = _execute(
        datasource,
        f"SELECT {', '.join(INPUT_COLUMNS)} FROM {INPUT_VIEW} ORDER BY request_id DESC;",
    )
    rows = [
        {app._normalize_lookup_key(key): value for key, value in row.items()}
        for row in app._rows_from_result(result)
    ]
    for row in rows:
        missing = set(INPUT_COLUMNS) - row.keys()
        if missing:
            raise ValueError(f"{INPUT_VIEW} is missing required columns: {', '.join(sorted(missing))}.")
    return rows


def _record_resolution_failure(
    datasource: app.DatasourceInstance, row: dict[str, object], error: ValueError,
) -> None:
    request_id = _integer(row, "request_id")
    print(RESOLUTION_FAILURE_COMMENT)
    print(f"Target project resolution failed for request {request_id}: {error}")
    try:
        failed_status = _integer(row, "resolution_failed_status_id")
        latest_id = (
            _integer(row, "latest_migration_id")
            if _text(row, "latest_migration_id") is not None else None
        )
        app._insert_comment(
            datasource, app.PROJECT_ID, request_id, RESOLUTION_FAILURE_COMMENT,
        )
        _execute(
            datasource,
            f"UPDATE {app.REQUEST_TABLE} SET status_id={failed_status} WHERE request_id={request_id};",
        )
        if latest_id is not None:
            _execute(
                datasource,
                f"UPDATE arms.t_mig_dim_migration SET status_id={failed_status} "
                f"WHERE request_id={request_id} AND migration_id={latest_id};",
            )
        print(f"Set request {request_id} and its cached latest migration, if present, to status_id={failed_status}.")
    except Exception as exc:
        raise RuntimeError(
            f"Target project resolution failed: {error}. "
            f"Unable to persist resolution-failure comment/statuses: {exc}"
        ) from exc


def _insert_history(
    datasource: app.DatasourceInstance, *, request_id: int, migration_key_id: int,
    target_project_id: int, status_id: int,
    migration_message: str | None = None,
) -> None:
    message_column = ", migration_message" if migration_message is not None else ""
    message_value = f", {app._sql_literal(migration_message)}" if migration_message is not None else ""
    _execute(
        datasource,
        "INSERT INTO arms.t_mig_dim_migration "
        f"(migration_id, migration_key_id, request_id, migration_time, project_id, status_id{message_column}) "
        "VALUES ((SELECT COALESCE(MAX(migration_id), 0) + 1 FROM arms.t_mig_dim_migration), "
        f"{migration_key_id}, {request_id}, CURRENT_TIMESTAMP, {target_project_id}, {status_id}{message_value});",
    )


def _import_request(
    source: app.Connection, datasource: app.DatasourceInstance, row: dict[str, object],
) -> int:
    request_id = _integer(row, "request_id")
    migration_obj_id = _required_text(row, "migration_obj_id")
    try:
        if not _is_ready(row):
            raise ValueError(_text(row, "resolution_error") or "Input view marked the request not ready.")
        if _text(row, "resolution_error"):
            raise ValueError(_required_text(row, "resolution_error"))
        request_status = _integer(row, "request_status_id")
        if request_status not in (5, 6, 7):
            raise ValueError(f"Unexpected request status {request_status}; expected 5, 6 or 7.")
        source_name = _required_text(row, "source_project_name")
        target_name = _required_text(row, "target_project_name")
        target_env_name = _required_text(row, "tgt_env_id")
        migration_key_id = _integer(row, "migration_key_id")
        target_project_id = _integer(row, "tgt_project_id")
        success_status = _integer(row, "status_id_success")
        failed_status = _integer(row, "status_id_failed")
        target_url = _text(row, "target_env_url") or app._resolve_target_environment_url(
            app.BASE_URL, app.SOURCE_ENVIRONMENT, target_env_name, app.ENVIRONMENT_URLS,
        )
    except ValueError as exc:
        _record_resolution_failure(datasource, row, exc)
        raise

    print(
        f"Retrieved request number: {request_id}\n"
        f"  Source project: {source_name}\n"
        f"  Target project: {target_name}\n"
        f"  Target environment: {target_env_name}\n"
        f"  Target URL: {target_url}\n"
        f"  Migration object GUID: {migration_obj_id}\n"
        f"  Request status ID: {request_status}"
    )
    target = None
    try:
        try:
            target = app.Connection(
                target_url, app.MSTR_USERNAME, app.MSTR_PASSWORD, project_name=target_name,
            )
            migration = app.Migration(source, id=migration_obj_id)
            app._list_active_migration_jobs(source, migration_obj_id, limit=20)

            def record_reused_migration(reused: app.Migration) -> None:
                _execute(
                    datasource,
                    f"UPDATE {app.MIGRATION_OBJECT_TABLE} "
                    f"SET migration_obj_id={app._sql_literal(reused.id)}, "
                    "migration_obj_cre_time=CURRENT_TIMESTAMP "
                    f"WHERE migration_key_id={migration_key_id};",
                )

            print(f"Starting migration at {datetime.now():%Y-%m-%d %H:%M:%S} with undo generation enabled.")
            result, active = app._migrate_with_lock_retry(
                migration, target_env=target, target_project_name=target_name,
                generate_undo=True, poll_seconds=5, max_attempts=1,
                on_reuse=record_reused_migration,
            )
            status = app._wait_for_migration_completion(active)
            print(f"Migration import status final value: {getattr(status, 'name', str(status))}")
            if status != app.ImportStatus.IMPORTED:
                raise RuntimeError(
                    f"Migration did not complete a successful import: {getattr(status, 'name', str(status))}."
                )
            app._wait_for_post_import_readiness(
                active, poll_seconds=app.POST_IMPORT_POLL_SECONDS,
                timeout_seconds=app.POST_IMPORT_TIMEOUT_SECONDS,
                require_unlocked_package=app.REQUIRE_UNLOCKED_PACKAGE_AFTER_IMPORT,
            )
            app._ensure_undo_request_not_pending(
                active, target_connection=target,
                require_requested=app.REQUIRE_UNDO_REQUESTED_AFTER_IMPORT,
            )
        except Exception as exc:
            message = f"Migration failed for request {request_id} on target environment '{target_env_name}': {exc}"
            print(message)
            try:
                _execute(
                    datasource,
                    f"UPDATE {app.REQUEST_TABLE} SET status_id={failed_status} WHERE request_id={request_id};",
                )
                _insert_history(
                    datasource, request_id=request_id, migration_key_id=migration_key_id,
                    target_project_id=target_project_id, status_id=failed_status,
                    migration_message=message,
                )
                app._insert_comment(datasource, app.PROJECT_ID, request_id, message)
            except Exception as writeback_exc:
                raise RuntimeError(
                    f"{message}\nFailed to persist failure status/history/comment: {writeback_exc}"
                ) from writeback_exc
            raise

        print(f"Import result:\n{result}")
        _insert_history(
            datasource, request_id=request_id, migration_key_id=migration_key_id,
            target_project_id=target_project_id, status_id=success_status,
        )
        app._insert_comment(
            datasource, app.PROJECT_ID, request_id,
            f"Migration completed successfully for request {request_id}. "
            f"Imported into target project '{target_name}' on environment '{target_env_name}'.",
        )
        _execute(
            datasource,
            f"UPDATE {app.REQUEST_TABLE} SET status_id={success_status} WHERE request_id={request_id};",
        )
        return 0
    finally:
        if target is not None:
            target.close()


def _start_or_resume_reverse(
    migration: app.Migration, target: app.Connection, target_name: str,
) -> None:
    migration.fetch()
    info = migration.import_info
    status = getattr(info, "status", None)
    if status in (app.ImportStatus.UNDO_SUCCESS, app.ImportStatus.UNDOING):
        print(f"Reverse already {status.name}; no undo request will be resubmitted.")
        return
    if status == app.ImportStatus.UNDO_FAILED:
        raise RuntimeError("Migration reverse previously completed with UNDO_FAILED.")
    request = getattr(info, "undo_request_status", None)
    request_name = str(getattr(request, "name", request or "unknown")).upper()
    if request_name == "REQUESTED":
        print("Undo already requested; approving the existing request.")
        app.migration_api.update_migration(
            connection=target,
            migration_id=migration.id,
            body={"importInfo": {"undoRequestStatus": "approved"}},
            prefer="respond-async",
            project_id=target.project_id,
        )
    elif request_name == "APPROVED":
        print("Undo already approved; waiting for completion.")
    elif request_name == "PENDING":
        migration.reverse(target_env=target, target_project_name=target_name)
    else:
        raise ValueError(f"Cannot reverse migration with undo request status {request_name}.")


def _wait_for_reverse(migration: app.Migration) -> None:
    deadline = monotonic() + app.POST_IMPORT_TIMEOUT_SECONDS
    while True:
        migration.fetch()
        status = getattr(getattr(migration, "import_info", None), "status", None)
        if status == app.ImportStatus.UNDO_SUCCESS:
            return
        if status == app.ImportStatus.UNDO_FAILED:
            raise RuntimeError("Migration reverse completed with UNDO_FAILED.")
        if monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for migration reverse: {getattr(status, 'name', str(status))}."
            )
        sleep(app.POST_IMPORT_POLL_SECONDS)


def _record_reverse_result(
    datasource: app.DatasourceInstance, row: dict[str, object], status: int, message: str,
) -> None:
    request_id = _integer(row, "request_id")
    latest_id = _text(row, "latest_migration_id")
    if latest_id is not None:
        _execute(
            datasource,
            f"UPDATE arms.t_mig_dim_migration SET status_id={status}, "
            f"migration_message={app._sql_literal(message)} "
            f"WHERE request_id={request_id} AND migration_id={_integer(row, 'latest_migration_id')};",
        )
    app._insert_comment(datasource, app.PROJECT_ID, request_id, message)
    _execute(
        datasource,
        f"UPDATE {app.REQUEST_TABLE} SET status_id={status} WHERE request_id={request_id};",
    )


def _record_reverse_success(datasource: app.DatasourceInstance, row: dict[str, object], message: str) -> None:
    request_id = _integer(row, "request_id")
    migration_key_id = _integer(row, "migration_key_id")
    target_project_id = _integer(row, "tgt_project_id")
    _insert_history(
        datasource,
        request_id=request_id,
        migration_key_id=migration_key_id,
        target_project_id=target_project_id,
        status_id=ROLLBACK_SUCCESS_STATUS,
        migration_message=message,
    )
    app._insert_comment(datasource, app.PROJECT_ID, request_id, message)
    _execute(
        datasource,
        f"UPDATE {app.REQUEST_TABLE} SET status_id={ROLLBACK_SUCCESS_STATUS} WHERE request_id={request_id};",
    )


def _reverse_request(datasource: app.DatasourceInstance, row: dict[str, object]) -> int:
    request_id = _integer(row, "request_id")
    target = None
    try:
        try:
            if not _is_ready(row) or _text(row, "resolution_error"):
                raise ValueError(_text(row, "resolution_error") or "Rollback input is not ready.")
            latest_id = _integer(row, "latest_migration_id")
            migration_obj_id = _required_text(row, "migration_obj_id")
            target_name = _required_text(row, "target_project_name")
            target_env = _required_text(row, "tgt_env_id")
            url = _text(row, "target_env_url") or app._resolve_target_environment_url(
                app.BASE_URL, app.SOURCE_ENVIRONMENT, target_env, app.ENVIRONMENT_URLS,
            )
            target = app.Connection(
                url, app.MSTR_USERNAME, app.MSTR_PASSWORD, project_name=target_name,
            )
            migration = app.Migration(target, id=migration_obj_id)
            print(f"Reversing migration {latest_id} ({migration_obj_id}) for request {request_id}.")
            _start_or_resume_reverse(migration, target, target_name)
            _wait_for_reverse(migration)
        except Exception as exc:
            message = f"Migration rollback failed for request {request_id}: {exc}"
            print(message)
            try:
                _record_reverse_result(datasource, row, ROLLBACK_FAILED_STATUS, message)
            except Exception as writeback_exc:
                raise RuntimeError(
                    f"{message}\nFailed to persist rollback failure: {writeback_exc}"
                ) from writeback_exc
            raise
        message = f"Migration rollback completed successfully for request {request_id}, migration {latest_id}."
        print(message)
        _record_reverse_success(datasource, row, message)
        return 0
    finally:
        if target is not None:
            target.close()


def main() -> int:
    print("Connecting to environment...")
    source = app.Connection(
        app.BASE_URL, app.MSTR_USERNAME, app.MSTR_PASSWORD, project_id=app.PROJECT_ID,
    )
    try:
        datasource = app.DatasourceInstance(connection=source, id=app.DATASOURCE_INSTANCE_ID)
        rows = _fetch_inputs(datasource)
        if not rows:
            print("No requests found in the import input view. No package will be imported.")
            return 0
        for row in rows:
            request_id = _integer(row, "request_id")
            if _integer(row, "request_status_id") == ROLLBACK_REQUEST_STATUS:
                return _reverse_request(datasource, row)
            if _text(row, "migration_obj_id") is None:
                print(f"Skipping request {request_id}: no migration object GUID. Create its package before importing.")
                continue
            return _import_request(source, datasource, row)
        print("No matching requests have a migration package available to import.")
        return 0
    finally:
        source.close()


if __name__ == "__main__":
    raise SystemExit(main())
