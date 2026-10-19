from __future__ import annotations

import pytest

import create_package_validation_loop
import import_package
from import_package import (
    _ensure_undo_request_not_pending,
    _wait_for_post_import_readiness,
    _insert_migration_dim_record,
    _is_package_locked_error,
    _list_active_migration_jobs,
    _migrate_with_lock_retry,
    _resolve_target_arms_project_id,
    _resolve_target_project_name_from_sql,
    _select_request_with_migration,
)
from mstrio.object_management.migration.package import ImportStatus


def test_fetch_requests_joins_source_names_to_each_request():
    class FakeDatasource:
        def execute_query(self, **kwargs):
            query = kwargs["query"]
            assert f"FROM {import_package.REQUEST_TABLE} r" in query
            assert f"LEFT JOIN {import_package.PROJECT_LOOKUP_TABLE} p ON r.project_id = p.project_id" in query
            assert "p.project_name AS source_project_name" in query
            assert "WHERE r.status_id IN (5, 6, 7)" in query
            assert "ORDER BY r.request_id DESC" in query
            return {"results": {"data": {
                "request_id": [13, 12, 11],
                "project_id": [1, 2, None],
                "source_project_name": ["E01 - Tutorial", "E01 - Finance", None],
            }}}

    rows = import_package._fetch_requests_for_statuses(FakeDatasource(), "query-project", (5, 6, 7))
    assert rows[0]["source_project_name"] == "E01 - Tutorial"
    assert rows[1]["source_project_name"] == "E01 - Finance"
    assert rows[2]["source_project_name"] is None
    assert rows[2]["request_id"] == 11


def test_select_request_with_migration_skips_missing_packages(capsys):
    class FakeDatasource:
        def execute_query(self, **kwargs):
            assert "migration_key_id = 22" in kwargs["query"]
            return {"results": {"data": {"migration_obj_guid": ["package:import"]}}}

    ready_request = {"request_id": 2, "migration_key_id": 22}
    result = _select_request_with_migration(
        [{"request_id": 1}, ready_request], FakeDatasource(), "query-project"
    )
    assert result == (ready_request, "package:import")
    assert "Skipping request 1" in capsys.readouterr().out


def test_select_request_with_migration_returns_none_for_missing_lookup(capsys):
    class FakeDatasource:
        def execute_query(self, **kwargs):
            return {"results": {"data": {"migration_obj_guid": [None]}}}

    assert _select_request_with_migration(
        [{"request_id": 1, "migration_key_id": 11}], FakeDatasource(), "query-project"
    ) is None
    assert "Create its package before importing" in capsys.readouterr().out


class LockingMigration:
    def __init__(self):
        self.calls = 0

    def migrate(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("Cannot do current operation for now when `packageInfo.status` is 'locked'.; code: 'ERR006'")
        return {"ok": True}

    def fetch(self):
        return None


def test_is_package_locked_error_recognizes_lock_message():
    exc = RuntimeError("Cannot do current operation for now when `packageInfo.status` is 'locked'.; code: 'ERR006'")
    assert _is_package_locked_error(exc) is True


def test_migrate_with_lock_retry_fails_immediately_when_locked(monkeypatch):
    migration = LockingMigration()
    sleep_calls = []

    monkeypatch.setattr("import_package.sleep", lambda seconds: sleep_calls.append(seconds))

    with pytest.raises(RuntimeError, match="locked"):
        _migrate_with_lock_retry(
            migration,
            target_env="target",
            target_project_name="TGT - Sales",
            generate_undo=True,
            poll_seconds=1,
            max_attempts=3,
        )

    assert migration.calls == 1
    assert sleep_calls == []


def test_list_active_migration_jobs_lists_jobs_for_migration(monkeypatch):
    calls = []

    def fake_list_jobs(*, connection, object_id, limit=None, **kwargs):
        calls.append((object_id, limit))
        return [
            type("Job", (), {"id": "Job-1", "status": "EXECUTING", "description": "migration 123", "user": "system"})(),
            type("Job", (), {"id": "Job-2", "status": "COMPLETED", "description": "migration 123", "user": "system"})(),
        ]

    monkeypatch.setattr("import_package.list_jobs", fake_list_jobs)

    jobs = _list_active_migration_jobs(connection=object(), migration_obj_guid="123", limit=10)

    assert jobs[0]["job_id"] == "Job-1"
    assert jobs[0]["status"] == "EXECUTING"
    assert calls == [("123", 10)]


def test_migrate_with_lock_retry_does_not_delete_migration_before_import(monkeypatch):
    class SafeMigration:
        def __init__(self):
            self.migrate_calls = 0
            self.delete_calls = 0

        def migrate(self, **kwargs):
            self.migrate_calls += 1
            if self.migrate_calls == 1:
                raise RuntimeError("Cannot do current operation for now when `packageInfo.status` is 'locked'.; code: 'ERR006'")
            return {"ok": True}

        def delete(self, *, force=False):
            self.delete_calls += 1
            raise AssertionError("delete() must not be called before import.")

    migration = SafeMigration()
    monkeypatch.setattr("import_package.FORCE_UNLOCK_LOCKED_PACKAGE", True)

    with pytest.raises(RuntimeError, match="locked"):
        _migrate_with_lock_retry(
            migration,
            target_env="target",
            target_project_name="TGT - Sales",
            generate_undo=True,
            poll_seconds=1,
            max_attempts=1,
        )

    assert migration.migrate_calls == 1
    assert migration.delete_calls == 0


def test_wait_for_post_import_readiness_succeeds_when_unlocked_and_undo_exists(monkeypatch):
    class Status:
        def __init__(self, name):
            self.name = name

    class UndoStorage:
        def __init__(self, path):
            self.path = path

    class FakeImportInfo:
        def __init__(self, status, undo_request_status, undo_storage):
            self.status = status
            self.undo_request_status = undo_request_status
            self.undo_storage = undo_storage

    class FakePackageInfo:
        def __init__(self, status):
            self.status = status

    class FakeMigration:
        def __init__(self):
            self._step = 0
            self.import_info = FakeImportInfo(ImportStatus.IMPORTED, Status("PENDING"), UndoStorage(None))
            self.package_info = FakePackageInfo(Status("LOCKED"))

        def fetch(self):
            self._step += 1
            if self._step >= 2:
                self.import_info = FakeImportInfo(
                    ImportStatus.IMPORTED,
                    Status("PENDING"),
                    UndoStorage("mstr/shared/migrations/undo/file.mmp"),
                )
                self.package_info = FakePackageInfo(Status("READY"))

    monkeypatch.setattr("import_package.sleep", lambda seconds: None)
    _wait_for_post_import_readiness(FakeMigration(), poll_seconds=0, timeout_seconds=5)


def test_wait_for_post_import_readiness_times_out_when_still_locked(monkeypatch):
    class Status:
        def __init__(self, name):
            self.name = name

    class FakeImportInfo:
        def __init__(self):
            self.status = ImportStatus.IMPORTED
            self.undo_request_status = Status("PENDING")
            self.undo_storage = None

    class FakePackageInfo:
        def __init__(self):
            self.status = Status("LOCKED")

    class FakeMigration:
        def __init__(self):
            self.import_info = FakeImportInfo()
            self.package_info = FakePackageInfo()

        def fetch(self):
            return None

    monkeypatch.setattr("import_package.sleep", lambda seconds: None)
    with pytest.raises(TimeoutError, match="Post-import checks timed out"):
        _wait_for_post_import_readiness(
            FakeMigration(),
            poll_seconds=0,
            timeout_seconds=0,
            require_unlocked_package=True,
        )


def test_wait_for_post_import_readiness_succeeds_when_locked_allowed(monkeypatch):
    class Status:
        def __init__(self, name):
            self.name = name

    class UndoStorage:
        def __init__(self, path):
            self.path = path

    class FakeImportInfo:
        def __init__(self):
            self.status = ImportStatus.IMPORTED
            self.undo_request_status = Status("PENDING")
            self.undo_storage = UndoStorage("mstr/shared/migrations/undo/file.mmp")

    class FakePackageInfo:
        def __init__(self):
            self.status = Status("LOCKED")

    class FakeMigration:
        def __init__(self):
            self.import_info = FakeImportInfo()
            self.package_info = FakePackageInfo()

        def fetch(self):
            return None

    monkeypatch.setattr("import_package.sleep", lambda seconds: None)
    _wait_for_post_import_readiness(
        FakeMigration(),
        poll_seconds=0,
        timeout_seconds=0,
        require_unlocked_package=False,
    )


def test_ensure_undo_request_not_pending_updates_to_requested(monkeypatch):
    class Status:
        def __init__(self, name):
            self.name = name

    class FakeImportInfo:
        def __init__(self, status_name):
            self.undo_request_status = Status(status_name)

    class FakeMigration:
        def __init__(self):
            self.id = "m-1"
            self.import_info = FakeImportInfo("PENDING")

        def fetch(self):
            self.import_info = FakeImportInfo("REQUESTED")

    calls = []

    def fake_update_migration(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr("import_package.migration_api.update_migration", fake_update_migration)

    migration = FakeMigration()
    target_connection = type("Conn", (), {"project_id": "P1"})()
    _ensure_undo_request_not_pending(
        migration,
        target_connection=target_connection,
        require_requested=True,
    )

    assert len(calls) == 1
    assert calls[0]["body"] == {"importInfo": {"undoRequestStatus": "requested"}}
    assert calls[0]["project_id"] == "P1"


def test_ensure_undo_request_not_pending_raises_when_still_pending(monkeypatch):
    class Status:
        def __init__(self, name):
            self.name = name

    class FakeImportInfo:
        def __init__(self, status_name):
            self.undo_request_status = Status(status_name)

    class FakeMigration:
        def __init__(self):
            self.id = "m-1"
            self.import_info = FakeImportInfo("PENDING")

        def fetch(self):
            return None

    monkeypatch.setattr("import_package.migration_api.update_migration", lambda **kwargs: object())

    with pytest.raises(RuntimeError, match="still PENDING"):
        _ensure_undo_request_not_pending(
            FakeMigration(),
            target_connection=type("Conn", (), {"project_id": "P1"})(),
            require_requested=True,
        )


def test_resolve_target_project_name_prefers_status_env_and_prefix_match():
    class FakeDatasource:
        def execute_query(self, **kwargs):
            return {
                "results": {
                    "data": {
                        "status_id": [5, 5],
                        "tgt_env_id": ["ENV-TEST", "ENV-TEST"],
                        "tgt_project_name": [
                            "Consolidated Education Project",
                            "T01 Microstrategy Tutorial",
                        ],
                        "project_prefix": ["", "T01"],
                        "source_env_name": ["dev", "dev"],
                    }
                }
            }

    result = _resolve_target_project_name_from_sql(
        datasource=FakeDatasource(),
        project_id="4B0544627D49F8DA03C26D817D208263",
        target_env_id="ENV-TEST",
        request_status_id=5,
        source_project_name="Microstrategy Tutorial",
        target_prefix="T01",
        fallback_name="Consolidated Education Project",
    )

    assert result == "T01 Microstrategy Tutorial"


def test_resolve_target_project_name_ignores_misleading_source_name_when_prefix_matches():
    class FakeDatasource:
        def execute_query(self, **kwargs):
            return {
                "results": {
                    "data": {
                        "status_id": [5, 5],
                        "tgt_env_id": ["ENV-TEST", "ENV-TEST"],
                        "tgt_project_name": [
                            "Consolidated Education Project",
                            "T01 - Microstrategy Tutorial",
                        ],
                        "project_prefix": ["", "T01 -"],
                        "source_env_name": ["dev", "dev"],
                    }
                }
            }

    result = _resolve_target_project_name_from_sql(
        datasource=FakeDatasource(),
        project_id="4B0544627D49F8DA03C26D817D208263",
        target_env_id="ENV-TEST",
        request_status_id=5,
        source_project_name="Consolidated Education Project",
        target_prefix="T01",
        fallback_name="Consolidated Education Project",
    )

    assert result == "T01 - Microstrategy Tutorial"


def test_resolve_target_project_name_prefers_exact_target_project_id():
    class FakeDatasource:
        def execute_query(self, **kwargs):
            query = kwargs["query"]
            if "CAST(tgt_project_id AS VARCHAR) = '11'" in query:
                return {
                    "results": {
                        "data": {
                            "tgt_project_id": [11],
                            "tgt_project_name": ["T01 - Microstrategy Tutorial"],
                            "status_id": [8],
                        }
                    }
                }
            return {"results": {"data": {}}}

    result = _resolve_target_project_name_from_sql(
        datasource=FakeDatasource(),
        project_id="4B0544627D49F8DA03C26D817D208263",
        target_env_id="ENV-TEST",
        request_status_id=5,
        source_project_name="Consolidated Education Project",
        target_prefix="T01",
        fallback_name="Consolidated Education Project",
        target_project_id=11,
    )

    assert result == "T01 - Microstrategy Tutorial"


@pytest.mark.parametrize("names", [["Status Target"], ["Status Target", "Status Target"]])
def test_strict_status_resolution_ignores_request_id_and_name_hints(names):
    class FakeDatasource:
        def execute_query(self, **kwargs):
            assert "status_id = 5" in kwargs["query"]
            assert "CAST(tgt_env_id AS VARCHAR) = 'ENV-TEST'" in kwargs["query"]
            assert "CAST(tgt_project_id AS VARCHAR)" not in kwargs["query"]
            return {"results": {"data": {"tgt_project_name": names}}}

    assert _resolve_target_project_name_from_sql(
        FakeDatasource(), "query-project", "ENV-TEST", 5,
        "E01 - Target", "Status", "Wrong Target", 99,
        strict_status=True,
    ) == "Status Target"


@pytest.mark.parametrize("names", [[], [None, " ", "-"], ["T01 - A", "T01 - B"]])
def test_strict_status_resolution_rejects_missing_or_ambiguous_projects(names):
    class FakeDatasource:
        def __init__(self):
            self.calls = 0

        def execute_query(self, **kwargs):
            self.calls += 1
            assert self.calls == 1, "Strict resolution must not query name fallbacks."
            return {"results": {"data": {"tgt_project_name": names}}}

    with pytest.raises(ValueError, match="requires exactly one target project"):
        _resolve_target_project_name_from_sql(
            FakeDatasource(), "query-project", "ENV-TEST", 5,
            "Source", "T01", "Request Target", strict_status=True,
        )


@pytest.mark.parametrize("status_id,env_id", [(None, "ENV-TEST"), (5, None), ("", "")])
def test_strict_status_resolution_requires_status_and_environment(status_id, env_id):
    with pytest.raises(ValueError, match="requires a request status and target environment"):
        _resolve_target_project_name_from_sql(
            None, "query-project", env_id, status_id, "Source", "T01",
            strict_status=True,
        )


@pytest.mark.parametrize("prefix", ["T01", "t01", "T01 -"])
def test_strict_status_resolution_filters_by_environment_prefix(prefix):
    class FakeDatasource:
        def execute_query(self, **kwargs):
            assert "status_id = 5" in kwargs["query"]
            assert "CAST(tgt_env_id AS VARCHAR) = 'test'" in kwargs["query"]
            return {"results": {"data": {"tgt_project_name": [
                "Release Management", "T010 - Wrong Project", "T01 - MicroStrategy Tutorial",
            ]}}}

    assert _resolve_target_project_name_from_sql(
        FakeDatasource(), "query-project", "test", 5, "E01 - MicroStrategy Tutorial", prefix,
        strict_status=True,
    ) == "T01 - MicroStrategy Tutorial"


def test_strict_status_resolution_requires_environment_prefix():
    with pytest.raises(ValueError, match="requires a target project prefix"):
        _resolve_target_project_name_from_sql(
            None, "query-project", "test", 5, "Source", "", strict_status=True,
        )


@pytest.mark.parametrize("env,prefix", [("test", "T01"), ("qa", "Q01"), ("prod", "P01")])
@pytest.mark.parametrize("source", ["E01 - MicroStrategy Tutorial", "MicroStrategy Tutorial"])
def test_strict_resolution_matches_source_family(env, prefix, source):
    expected = f"{prefix} - microstrategy tutorial"

    class FakeDatasource:
        def execute_query(self, **kwargs):
            assert "status_id = 5" in kwargs["query"]
            assert f"= '{env}'" in kwargs["query"]
            return {"results": {"data": {"tgt_project_name": [
                f"{prefix} - Finance", expected, "Release Management",
            ]}}}

    assert _resolve_target_project_name_from_sql(
        FakeDatasource(), "query-project", env, 5, source, prefix, strict_status=True,
    ) == expected


def test_strict_resolution_rejects_unrelated_same_prefix_project():
    class FakeDatasource:
        def execute_query(self, **kwargs):
            return {"results": {"data": {"tgt_project_name": ["T01 - Finance"]}}}

    with pytest.raises(ValueError, match="source suffix='MicroStrategy Tutorial'"):
        _resolve_target_project_name_from_sql(
            FakeDatasource(), "query-project", "test", 5,
            "E01 - MicroStrategy Tutorial", "T01", strict_status=True,
        )


def test_strict_resolution_requires_source_not_request_target():
    with pytest.raises(ValueError, match="requires the source project name"):
        _resolve_target_project_name_from_sql(
            None, "query-project", "test", 5, "", "T01",
            "T01 - Tutorial", strict_status=True,
        )


@pytest.mark.parametrize("status_id", [None, 5, 6, 7])
def test_main_strict_status_resolution_rejects_missing_mapping(monkeypatch, capsys, status_id):
    connections = []
    queries = []
    closed = []

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            connections.append(kwargs)

        def close(self):
            closed.append(True)

    class FakeDatasource:
        def execute_query(self, **kwargs):
            queries.append(kwargs["query"])

    request = {"request_id": 1, "target_project_name": "Must Not Be Used", "status_id": status_id}
    monkeypatch.setattr(import_package, "FORCE_STATUS_TARGET_PROJECT_RESOLUTION", True)
    monkeypatch.setattr(import_package, "Connection", FakeConnection)
    monkeypatch.setattr(import_package, "DatasourceInstance", lambda **kwargs: FakeDatasource())
    monkeypatch.setattr(import_package, "_fetch_requests_for_statuses", lambda *args: [request])
    monkeypatch.setattr(import_package, "_select_request_with_migration", lambda *args: (request, "package"))
    monkeypatch.setattr(import_package, "_get_status_target_env_id", lambda *args: "qa" if status_id in (6, 7) else None)
    monkeypatch.setattr(import_package, "_get_target_environment_details", lambda *args: {
        "env_name": "qa", "prefix": "Q01", "url": "https://example.invalid",
    })

    def unresolved(*args, **kwargs):
        raise ValueError("No matching source/target project")

    monkeypatch.setattr(import_package, "_resolve_target_project_name_from_sql", unresolved)
    def failed_status(*args, **kwargs):
        if status_id == 7:
            raise ValueError("Missing status_id_failed")
        return 14

    monkeypatch.setattr(import_package, "_get_result_status_id_for_environment", failed_status)

    with pytest.raises(ValueError):
        import_package.main()
    assert len(connections) == 1, "No target connection should be opened."
    message = "Unable to resolve matchine Target Project name. Please check Environment/Status/Project mapping."
    assert len(queries) == 3
    assert "INSERT INTO arms.t_mig_comments" in queries[0]
    assert f"1, '{message}'" in queries[0]
    expected_status = 14 if status_id == 6 else 11
    assert f"UPDATE {import_package.REQUEST_TABLE} SET status_id={expected_status} WHERE request_id=1;" == queries[1]
    assert f"UPDATE arms.t_mig_dim_migration SET status_id={expected_status}" in queries[2]
    assert "SELECT MAX(migration_id)" in queries[2]
    assert "WHERE request_id=1" in queries[2]
    assert message in capsys.readouterr().out
    assert closed == [True]


def test_insert_migration_dim_record_uses_arms_project_key_not_guid():
    captured = {}

    class FakeDatasource:
        def execute_query(self, **kwargs):
            captured.update(kwargs)
            return {"results": {"data": {}}}

    _insert_migration_dim_record(
        datasource=FakeDatasource(),
        project_id="4B0544627D49F8DA03C26D817D208263",
        request_id=11,
        source_project_name="Consolidated Education Project",
        target_project_name="Consolidated Education Project",
        target_env_name="test",
        migration_obj_guid="D982F0570CFC4E03A95756FDC0B7D802:8D0F3CFD087E402CBA2CAEC152EA5BB6",
        migration_id="migration-1",
        migration_name="Req_11_Test",
        status_id=13,
        arms_project_id=42,
    )

    assert captured["project_id"] == "4B0544627D49F8DA03C26D817D208263"
    assert "COALESCE(MAX(migration_id), 0) + 1" in captured["query"]
    assert "(SELECT migration_key_id FROM arms.t_mig_dim_request WHERE request_id = 11)" in captured["query"]
    assert "CURRENT_TIMESTAMP, 42, 13" in captured["query"]
    assert "4B0544627D49F8DA03C26D817D208263" not in captured["query"]


@pytest.mark.parametrize("project_key", [77, 77.0, "77.0", "77.000"])
def test_resolve_target_arms_project_id_uses_target_lookup(monkeypatch, project_key):
    monkeypatch.setattr(import_package, "TARGET_PROJECT_TABLE", "arms.custom_target_projects")

    class FakeDatasource:
        def execute_query(self, **kwargs):
            assert kwargs["project_id"] == "query-project"
            assert kwargs["query"] == (
                "SELECT tgt_project_id FROM arms.custom_target_projects "
                "WHERE LOWER(TRIM(tgt_project_name)) = LOWER('Q01 - Manager''s Tutorial');"
            )
            return {"results": {"data": {"tgt_project_id": [project_key, project_key]}}}

    result = _resolve_target_arms_project_id(
        datasource=FakeDatasource(),
        query_project_id="query-project",
        target_project_name=" Q01 - Manager's Tutorial ",
    )

    assert result == 77


@pytest.mark.parametrize("ids", [[], [None], ["bad-id"], ["13.5"], ["NaN"], ["Infinity"], [11, 99]])
def test_resolve_target_arms_project_id_rejects_invalid_mapping(ids):
    class FakeDatasource:
        def execute_query(self, **kwargs):
            return {"results": {"data": {"tgt_project_id": ids}}}

    with pytest.raises(ValueError, match="tgt_project_id"):
        _resolve_target_arms_project_id(FakeDatasource(), "query-project", "Q01 - Tutorial")


@pytest.mark.parametrize("project_key", [None, "guid-not-a-key", "13.5"])
def test_history_insert_rejects_invalid_target_key(project_key):
    class FakeDatasource:
        def execute_query(self, **kwargs):
            pytest.fail("Invalid target keys must not be inserted.")

    with pytest.raises(ValueError, match="numeric tgt_project_id"):
        _insert_migration_dim_record(
            FakeDatasource(), "query-project", 12, "", "Q01 - Tutorial", "qa",
            "package:import", "package:import", "Req_12", status_id=14,
            arms_project_id=project_key,
        )


def test_delete_existing_migration_if_present_skips_missing_id(monkeypatch):
    calls = []

    class FakeMigration:
        def __init__(self, connection, id=None, name=None):
            calls.append(("init", id))
            if id == "missing-guid":
                raise RuntimeError("The migration with id missing-guid is not found.")

        def delete(self, **kwargs):
            calls.append(("delete", kwargs))
            raise AssertionError("delete() should not run when migration id is missing")

    monkeypatch.setattr(create_package_validation_loop, "Migration", FakeMigration)

    assert create_package_validation_loop._delete_existing_migration_if_present(object(), "") is None
    assert create_package_validation_loop._delete_existing_migration_if_present(object(), "missing-guid") is None
    assert calls == [("init", "missing-guid")]


@pytest.mark.parametrize("succeeded,expected", [(True, 71), (False, 72)])
def test_result_status_comes_from_target_environment(succeeded, expected):
    class FakeDatasource:
        def execute_query(self, **kwargs):
            assert import_package.TARGET_ENV_TABLE in kwargs["query"]
            assert import_package.STATUS_TABLE not in kwargs["query"]
            return {"results": {"data": {
                "tgt_env_id": ["test", "qa"],
                "status_id_success": [81, 71],
                "status_id_failed": [82, 72],
            }}}

    assert import_package._get_result_status_id_for_environment(
        FakeDatasource(), "query-project", "qa", succeeded=succeeded
    ) == expected


@pytest.mark.parametrize("value", [None, "", "failed", "13.5"])
def test_result_status_rejects_missing_or_invalid_mapping(value):
    class FakeDatasource:
        def execute_query(self, **kwargs):
            return {"results": {"data": {
                "tgt_env_id": ["qa"], "status_id_failed": [value],
            }}}

    with pytest.raises(ValueError, match="status_id_failed"):
        import_package._get_result_status_id_for_environment(
            FakeDatasource(), "query-project", "qa", succeeded=False
        )


@pytest.mark.parametrize("outcome", [
    "success", "failed_status", "undo", "start_error", "connection_error",
    "mapping_error", "writeback_error", "reuse_start_error",
])
def test_main_writes_environment_result_status(monkeypatch, outcome):
    succeeded = outcome == "success"
    queries = []
    connections = []
    request = {
        "request_id": 12, "status_id": 6, "source_project_name": "DEV - Tutorial",
        "tgt_project_id": 999,
    }

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            if outcome == "connection_error" and "project_name" in kwargs:
                raise RuntimeError("Target connection failed")
            self.project_id = kwargs.get("project_id", "qa-project")
            self.closed = False
            connections.append(self)

        def close(self):
            self.closed = True

    class FakeDatasource:
        def __init__(self, **kwargs):
            pass

        def execute_query(self, **kwargs):
            if kwargs["query"].startswith("SELECT tgt_project_id"):
                assert import_package.TARGET_PROJECT_TABLE in kwargs["query"]
                assert "LOWER('Q01 - Tutorial')" in kwargs["query"]
                return {"results": {"data": {"tgt_project_id": [42]}}}
            queries.append(kwargs["query"])
            if outcome == "writeback_error":
                raise RuntimeError("Database unavailable")
            return {"results": {"data": {}}}

    migration = type("FakeMigration", (), {"id": "package:import", "name": "Req_12"})()
    monkeypatch.setattr(import_package, "Connection", FakeConnection)
    monkeypatch.setattr(import_package, "DatasourceInstance", FakeDatasource)
    monkeypatch.setattr(import_package, "Migration", lambda *args, **kwargs: migration)
    monkeypatch.setattr(import_package, "_fetch_requests_for_statuses", lambda *args: [request])
    monkeypatch.setattr(import_package, "_select_request_with_migration", lambda *args: (request, migration.id))
    monkeypatch.setattr(import_package, "_get_status_target_env_id", lambda *args: "qa")
    monkeypatch.setattr(import_package, "_get_target_environment_details", lambda *args: {
        "env_name": "qa", "prefix": "Q01", "url": "https://example.invalid",
    })
    def result_status(*args, succeeded):
        if outcome == "mapping_error":
            raise ValueError("Missing status_id_failed")
        return 71 if succeeded else 72

    monkeypatch.setattr(import_package, "_get_result_status_id_for_environment", result_status)
    monkeypatch.setattr(import_package, "_resolve_target_project_name_from_sql", lambda *args, **kwargs: "Q01 - Tutorial")
    monkeypatch.setattr(import_package, "_list_active_migration_jobs", lambda *args, **kwargs: [])
    def migrate(*args, **kwargs):
        if outcome == "reuse_start_error":
            reused = type("Reused", (), {"id": "package:new-import", "name": "Reused"})()
            kwargs["on_reuse"](reused)
            raise RuntimeError("Reused import could not start")
        if outcome in {"start_error", "writeback_error"}:
            raise RuntimeError("Package is locked")
        return None, migration

    monkeypatch.setattr(import_package, "_migrate_with_lock_retry", migrate)
    monkeypatch.setattr(import_package, "_wait_for_migration_completion",
                        lambda *args: (
                            ImportStatus.IMPORTED if succeeded
                            else ImportStatus.UNDO_SUCCESS if outcome == "undo"
                            else ImportStatus.IMPORT_FAILED
                        ))
    monkeypatch.setattr(import_package, "_wait_for_post_import_readiness", lambda *args, **kwargs: None)
    monkeypatch.setattr(import_package, "_ensure_undo_request_not_pending", lambda *args, **kwargs: None)

    if outcome == "mapping_error":
        with pytest.raises(ValueError, match="Missing status_id_failed"):
            import_package.main()
        assert queries == []
        assert len(connections) == 1
        assert connections[0].closed
        return
    if outcome == "writeback_error":
        with pytest.raises(RuntimeError, match="Failed to persist failure status/history/comment: Database unavailable"):
            import_package.main()
        assert all(connection.closed for connection in connections)
        return
    if succeeded:
        assert import_package.main() == 0
    else:
        with pytest.raises(RuntimeError):
            import_package.main()
    expected = 71 if succeeded else 72
    assert any(f"SET status_id={expected}" in query and "request_id=12" in query for query in queries)
    inserts = [query for query in queries if query.startswith("INSERT INTO arms.t_mig_dim_migration")]
    assert len(inserts) == 1
    assert f"CURRENT_TIMESTAMP, 42, {expected}" in inserts[0]
    assert any("arms.t_mig_comments" in query for query in queries)
    if succeeded:
        assert "migration_message" not in inserts[0]
    else:
        assert "migration_message" in inserts[0]
        comment = next(query for query in queries if query.startswith("INSERT INTO arms.t_mig_comments"))
        message_literal = comment.split(", 12, ", 1)[1].split(", CURRENT_TIMESTAMP", 1)[0]
        assert inserts[0].endswith(f", {message_literal});")
    if outcome == "reuse_start_error":
        assert any(
            f"UPDATE {import_package.MIGRATION_OBJECT_TABLE}" in query
            and "SET migration_obj_id='package:new-import', migration_obj_cre_time=CURRENT_TIMESTAMP" in query
            and "WHERE request_id=12" in query
            for query in queries
        )
    assert all(connection.closed for connection in connections)


@pytest.mark.parametrize("bound_project,bound_url", [
    ("wrong-project", "https://qa.invalid"),
    ("qa-project", "https://wrong.invalid"),
    ("", ""),
])
def test_locked_retry_rejects_wrong_binding_before_status_updates(monkeypatch, bound_project, bound_url):
    migration = LockingMigration()
    migration.id = "package:import"
    migration.connection = object()
    migration.package_info = type("Package", (), {"id": "package"})()
    clone = type("Clone", (), {"id": "package:new", "connection": migration.connection})()
    monkeypatch.setattr(import_package, "Migration", lambda *args, **kwargs: clone)
    monkeypatch.setattr(import_package.migration_api, "create_new_migration",
                        lambda **kwargs: type("Response", (), {"json": lambda self: {"id": clone.id}})())
    target = type("Target", (), {"project_id": "qa-project", "base_url": "https://qa.invalid"})()

    class Response:
        def json(self):
            return {"importInfo": {
                "project": {"id": bound_project},
                "environment": {"id": bound_url},
            }}

    monkeypatch.setattr(import_package.migration_api, "get_migration", lambda **kwargs: Response())

    def unexpected_call(**kwargs):
        pytest.fail("A mismatched locked migration must not be modified or started.")

    monkeypatch.setattr(import_package.migration_api, "update_migration", unexpected_call)
    monkeypatch.setattr(import_package.migration_api, "start_migration", unexpected_call)
    with pytest.raises(RuntimeError, match="saved import project"):
        _migrate_with_lock_retry(migration, target_env=target, target_project_name="QA")


def test_locked_retry_starts_only_with_matching_saved_binding(monkeypatch):
    migration = LockingMigration()
    migration.id = "package:import"
    migration.connection = object()
    migration.package_info = type("Package", (), {"id": "package"})()
    clone = type("Clone", (), {"id": "package:new", "connection": migration.connection})()
    monkeypatch.setattr(import_package, "Migration", lambda *args, **kwargs: clone)
    target = type("Target", (), {"project_id": "qa-project", "base_url": "https://qa.invalid"})()
    updates = []
    events = []

    def create(**kwargs):
        assert kwargs["package_id"] == "package"
        assert kwargs["body"]["importInfo"]["project"] == {"id": "qa-project", "name": "QA"}
        events.append("create")
        return type("Response", (), {"json": lambda self: {"id": clone.id}})()

    monkeypatch.setattr(import_package.migration_api, "create_new_migration", create)

    class Response:
        def json(self):
            return {
                "packageInfo": {},
                "importInfo": {
                    "project": {"id": "QA-PROJECT"},
                    "environment": {"id": "https://qa.invalid/"},
                },
            }

    def start(**kwargs):
        assert events == ["create", "persist"]
        assert kwargs["migration_id"] == clone.id
        assert kwargs["connection"] is target
        assert kwargs["project_id"] == "qa-project"
        assert kwargs["body"]["importInfo"]["project"]["id"] == "QA-PROJECT"
        return "started"

    monkeypatch.setattr(import_package.migration_api, "get_migration", lambda **kwargs: Response())
    monkeypatch.setattr(import_package.migration_api, "update_migration", lambda **kwargs: updates.append(kwargs["body"]))
    monkeypatch.setattr(import_package.migration_api, "start_migration", start)
    def persist(reused):
        assert reused is clone
        events.append("persist")

    assert _migrate_with_lock_retry(
        migration, target_env=target, target_project_name="QA", on_reuse=persist,
    ) == ("started", clone)
    assert updates == []


def test_sql_is_printed_before_execution(capsys):
    class FakeDatasource:
        def execute_query(self, **kwargs):
            assert capsys.readouterr().out == "Executing SQL:\nSELECT 1;\n"
            return {"results": {"data": {}}}

    import_package._execute_query(FakeDatasource(), project_id="query-project", query="SELECT 1;")


@pytest.mark.parametrize("value,expected", [
    ("10.0", 10), (10.0, 10), ("10.000", 10),
    ("9007199254740993.0", 9007199254740993),
])
def test_coerce_int_preserves_whole_number_values(value, expected):
    assert import_package._coerce_int(value) == expected


def test_history_insert_normalizes_decimal_target_key():
    class FakeDatasource:
        def execute_query(self, **kwargs):
            assert "CURRENT_TIMESTAMP, 10, 14" in kwargs["query"]
            assert "10.0" not in kwargs["query"]

    _insert_migration_dim_record(
        FakeDatasource(), "query-project", 12, "", "Q01 - Tutorial", "qa",
        "package:import", "package:import", "Req_12", status_id=14,
        arms_project_id="10.0",
    )


def test_job_lookup_does_not_send_composite_migration_id(monkeypatch, capsys):
    def unexpected_call(**kwargs):
        pytest.fail("The job monitor only accepts a single object GUID.")

    monkeypatch.setattr(import_package, "list_jobs", unexpected_call)
    assert _list_active_migration_jobs(object(), "package:import") == []
    assert "Job state is unknown" in capsys.readouterr().out


def test_import_polling_surfaces_fetch_errors():
    class FakeMigration:
        def fetch(self):
            raise RuntimeError("Cannot read migration status")

    with pytest.raises(RuntimeError, match="Cannot read migration status"):
        import_package._wait_for_migration_completion(FakeMigration())


def test_import_polling_times_out():
    class FakeMigration:
        import_info = type("Info", (), {"status": "RUNNING"})()

        def fetch(self):
            pass

    with pytest.raises(TimeoutError, match="last status: RUNNING"):
        import_package._wait_for_migration_completion(FakeMigration(), timeout_seconds=0)
