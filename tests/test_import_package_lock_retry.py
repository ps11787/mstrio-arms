from __future__ import annotations

import pytest

import create_package_validation_loop
from import_package import (
    _ensure_undo_request_not_pending,
    _wait_for_post_import_readiness,
    _insert_migration_dim_record,
    _is_package_locked_error,
    _list_active_migration_jobs,
    _migrate_with_lock_retry,
    _resolve_target_arms_project_id,
    _resolve_target_project_name_from_sql,
)
from mstrio.object_management.migration.package import ImportStatus


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


def test_resolve_target_arms_project_id_prefers_request_tgt_project_id():
    class FakeDatasource:
        def execute_query(self, **kwargs):
            raise AssertionError("Lookup should not execute when tgt_project_id is present.")

    result = _resolve_target_arms_project_id(
        request_row={"tgt_project_id": [11], "target_project_id": [99]},
        datasource=FakeDatasource(),
        query_project_id="4B0544627D49F8DA03C26D817D208263",
        target_project_name="T01 - Microstrategy Tutorial",
    )

    assert result == 11


def test_resolve_target_arms_project_id_falls_back_to_target_project_name_lookup():
    class FakeDatasource:
        def execute_query(self, **kwargs):
            return {"results": {"data": {"project_id": [77]}}}

    result = _resolve_target_arms_project_id(
        request_row={},
        datasource=FakeDatasource(),
        query_project_id="4B0544627D49F8DA03C26D817D208263",
        target_project_name="T01 - Microstrategy Tutorial",
    )

    assert result == 77


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
