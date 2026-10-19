from types import SimpleNamespace

import pytest

import import_package_from_view as script


@pytest.fixture
def runtime(monkeypatch):
    row = {
        "request_id": "13.0", "request_status_id": "5.0",
        "source_project_name": "E01 - Tutorial", "migration_key_id": "12.0",
        "migration_obj_id": "package:import", "tgt_env_id": "test",
        "target_env_url": "https://target/library", "target_project_name": "T01 - Tutorial",
        "tgt_project_id": "10.0", "status_id_success": "8.0", "status_id_failed": "13.0",
        "resolution_failed_status_id": "13.0", "latest_migration_id": "42.0",
        "resolution_error": None, "is_ready": "1",
    }
    state = SimpleNamespace(
        rows=[row], queries=[], connections=[], migrated=[], post_checks=[], reused=False,
        final_status=script.app.ImportStatus.IMPORTED, write_error=None,
    )

    class Connection:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.closed = False
            state.connections.append(self)

        def close(self):
            self.closed = True

    class Datasource:
        def __init__(self, **kwargs):
            pass

        def execute_query(self, *, query, **kwargs):
            state.queries.append(query)
            if query.startswith("SELECT "):
                assert len([q for q in state.queries if q.startswith("SELECT ")]) == 1
                assert f"FROM {script.INPUT_VIEW} ORDER BY request_id DESC" in query
                return {"results": {"data": {
                    column: [r[column] for r in state.rows] for column in script.INPUT_COLUMNS
                }}}
            if state.write_error:
                raise RuntimeError(state.write_error)
            return {}

    def migrate(migration, **kwargs):
        state.migrated.append((migration, kwargs))
        if state.reused:
            kwargs["on_reuse"](SimpleNamespace(id="package:reused"))
        return {"started": True}, migration

    monkeypatch.setattr(script.app, "Connection", Connection)
    monkeypatch.setattr(script.app, "DatasourceInstance", Datasource)
    monkeypatch.setattr(script.app, "Migration", lambda connection, id: SimpleNamespace(id=id))
    monkeypatch.setattr(script.app, "_list_active_migration_jobs", lambda *a, **kw: [])
    monkeypatch.setattr(script.app, "_migrate_with_lock_retry", migrate)
    monkeypatch.setattr(script.app, "_wait_for_migration_completion", lambda migration: state.final_status)
    monkeypatch.setattr(
        script.app, "_wait_for_post_import_readiness",
        lambda *a, **kw: state.post_checks.append("readiness"),
    )
    monkeypatch.setattr(
        script.app, "_ensure_undo_request_not_pending",
        lambda *a, **kw: state.post_checks.append("undo"),
    )
    return state


def test_success_uses_one_view_read_and_cached_writeback_keys(runtime):
    assert script.main() == 0
    assert len([q for q in runtime.queries if q.startswith("SELECT ")]) == 1
    history = next(q for q in runtime.queries if q.startswith("INSERT INTO arms.t_mig_dim_migration"))
    assert "12, 13, CURRENT_TIMESTAMP, 10, 8" in history
    assert "SELECT migration_key_id" not in history
    assert "migration_message" not in history
    assert runtime.queries[-1].endswith("SET status_id=8 WHERE request_id=13;")
    assert runtime.post_checks == ["readiness", "undo"]
    assert runtime.connections[1].kwargs["project_name"] == "T01 - Tutorial"
    assert all(connection.closed for connection in runtime.connections)


def test_empty_inputs_exit_cleanly(runtime, capsys):
    runtime.rows = []
    assert script.main() == 0
    assert "No requests found" in capsys.readouterr().out
    assert len(runtime.queries) == 1
    assert runtime.connections[0].closed


def test_missing_migration_is_skipped_without_extra_queries(runtime, capsys):
    runtime.rows.insert(0, dict(runtime.rows[0], request_id=14, migration_obj_id=None))
    assert script.main() == 0
    assert "Skipping request 14" in capsys.readouterr().out
    assert len(runtime.migrated) == 1


def test_all_missing_migrations_exit_cleanly(runtime, capsys):
    runtime.rows[0]["migration_obj_id"] = None
    assert script.main() == 0
    assert "No matching requests" in capsys.readouterr().out
    assert len(runtime.queries) == 1


@pytest.mark.parametrize("value", [False, "false", "f", "0", "0.0"])
def test_mapping_failure_writes_comment_request_and_cached_history(runtime, value):
    runtime.rows[0].update(is_ready=value, resolution_error="Missing matching target project")
    with pytest.raises(ValueError, match="Missing matching target project"):
        script.main()
    assert any(script.RESOLUTION_FAILURE_COMMENT in q for q in runtime.queries)
    assert any("SET status_id=13 WHERE request_id=13;" in q for q in runtime.queries)
    assert any("WHERE request_id=13 AND migration_id=42;" in q for q in runtime.queries)
    assert not runtime.migrated
    assert len(runtime.connections) == 1
    assert runtime.connections[0].closed


def test_mapping_failure_without_history_uses_view_fallback(runtime):
    runtime.rows[0].update(is_ready="0", resolution_error="Missing environment",
                           latest_migration_id=None, resolution_failed_status_id=11)
    with pytest.raises(ValueError, match="Missing environment"):
        script.main()
    assert any("SET status_id=11 WHERE request_id=13;" in q for q in runtime.queries)
    assert not any(q.startswith("UPDATE arms.t_mig_dim_migration") for q in runtime.queries)


@pytest.mark.parametrize("column,value", [
    ("tgt_project_id", "10.5"), ("status_id_success", None),
    ("status_id_failed", "NaN"), ("is_ready", "2"), ("target_project_name", ""),
    ("migration_key_id", "invalid"), ("request_status_id", "8"),
])
def test_invalid_cached_inputs_never_start_import(runtime, column, value):
    runtime.rows[0][column] = value
    with pytest.raises(ValueError):
        script.main()
    assert not runtime.migrated
    assert runtime.connections[0].closed
    assert any(script.RESOLUTION_FAILURE_COMMENT in q for q in runtime.queries)


@pytest.mark.parametrize("status", [
    script.app.ImportStatus.IMPORT_FAILED, script.app.ImportStatus.UNDO_SUCCESS,
])
def test_unsuccessful_import_writes_failed_attempt(runtime, status):
    runtime.final_status = status
    with pytest.raises(RuntimeError, match="did not complete a successful import"):
        script.main()
    history = next(q for q in runtime.queries if q.startswith("INSERT INTO arms.t_mig_dim_migration"))
    assert "12, 13, CURRENT_TIMESTAMP, 10, 13" in history
    assert "migration_message" in history
    comment = next(q for q in runtime.queries if q.startswith("INSERT INTO arms.t_mig_comments"))
    message_literal = comment.split(", 13, ", 1)[1].split(", CURRENT_TIMESTAMP", 1)[0]
    assert history.endswith(f", {message_literal});")
    assert not runtime.post_checks
    assert all(c.closed for c in runtime.connections)


def test_reuse_updates_cached_key_without_request_lookup(runtime):
    runtime.reused = True
    assert script.main() == 0
    update = next(q for q in runtime.queries if q.startswith(f"UPDATE {script.app.MIGRATION_OBJECT_TABLE}"))
    assert "'package:reused'" in update
    assert "WHERE migration_key_id=12;" in update
    assert "SELECT" not in update


def test_url_falls_back_to_existing_configuration(runtime, monkeypatch):
    runtime.rows[0]["target_env_url"] = ""
    monkeypatch.setattr(script.app, "ENVIRONMENT_URLS", {"test": "https://configured/library"})
    assert script.main() == 0
    assert runtime.connections[1].args[0] == "https://configured/library"


def test_writeback_errors_are_reported_and_connections_closed(runtime):
    runtime.rows[0].update(is_ready="0", resolution_error="No matching project")
    runtime.write_error = "database unavailable"
    with pytest.raises(RuntimeError, match="Unable to persist resolution-failure"):
        script.main()
    assert all(c.closed for c in runtime.connections)


def test_post_import_failure_records_failed_status(runtime, monkeypatch):
    def fail(*args, **kwargs):
        raise TimeoutError("Not ready: project's objects\nremain locked")

    monkeypatch.setattr(script.app, "_wait_for_post_import_readiness", fail)
    with pytest.raises(TimeoutError, match="Not ready"):
        script.main()
    assert any("SET status_id=13 WHERE request_id=13;" in q for q in runtime.queries)
    history = next(q for q in runtime.queries if q.startswith("INSERT INTO arms.t_mig_dim_migration"))
    comment = next(q for q in runtime.queries if q.startswith("INSERT INTO arms.t_mig_comments"))
    message_literal = comment.split(", 13, ", 1)[1].split(", CURRENT_TIMESTAMP", 1)[0]
    assert "project''s objects\nremain locked" in message_literal
    assert history.endswith(f", {message_literal});")
    assert all(c.closed for c in runtime.connections)


def test_schema_error_closes_source(runtime, monkeypatch):
    monkeypatch.setattr(script.app, "_rows_from_result", lambda result: [{"request_id": 13}])
    with pytest.raises(ValueError, match="missing required columns"):
        script.main()
    assert runtime.connections[0].closed


def test_reverse_uses_workstation_reverse_and_updates_existing_history(runtime, monkeypatch):
    runtime.rows[0]["request_status_id"] = 18
    calls = []
    states = iter([
        script.app.ImportStatus.IMPORTED, script.app.ImportStatus.IMPORTED,
        script.app.ImportStatus.UNDO_SUCCESS,
    ])

    class Migration:
        def __init__(self, connection, id):
            assert connection is runtime.connections[1]
            assert id == "package:import"
            self.import_info = SimpleNamespace(status=None, undo_request_status="pending")

        def reverse(self, **kwargs):
            calls.append(kwargs)

        def fetch(self):
            self.import_info.status = next(states)

    monkeypatch.setattr(script.app, "Migration", Migration)
    monkeypatch.setattr(script, "sleep", lambda seconds: None)
    assert script.main() == 0
    assert calls[0]["target_project_name"] == "T01 - Tutorial"
    assert not runtime.migrated
    inserts = [q for q in runtime.queries if q.startswith("INSERT INTO arms.t_mig_dim_migration")]
    assert len(inserts) == 1
    assert "12, 13, CURRENT_TIMESTAMP, 10, 19" in inserts[0]
    assert "migration_message" in inserts[0]
    assert runtime.queries[-1].endswith("SET status_id=19 WHERE request_id=13;")
    assert sum(q.startswith("SELECT ") for q in runtime.queries) == 1
    assert all(c.closed for c in runtime.connections)


@pytest.mark.parametrize("failure", ["missing_object", "missing_history", "mapping", "api", "undo_failed", "timeout"])
def test_reverse_failure_writes_status_11_and_matching_message(runtime, monkeypatch, failure):
    runtime.rows[0]["request_status_id"] = 18
    if failure == "missing_object":
        runtime.rows[0]["migration_obj_id"] = None
    elif failure == "missing_history":
        runtime.rows[0]["latest_migration_id"] = None
    elif failure == "mapping":
        runtime.rows[0].update(is_ready=0, resolution_error="Historical target missing")

    class Migration:
        def __init__(self, *args, **kwargs):
            self.import_info = SimpleNamespace(
                status=(script.app.ImportStatus.UNDO_FAILED if failure == "undo_failed"
                        else script.app.ImportStatus.IMPORTED),
                undo_request_status="pending",
            )

        def reverse(self, **kwargs):
            if failure == "api":
                raise RuntimeError("Project's undo is unavailable")

        def fetch(self):
            pass

    monkeypatch.setattr(script.app, "Migration", Migration)
    monkeypatch.setattr(script.app, "POST_IMPORT_TIMEOUT_SECONDS", 0)
    if failure == "timeout":
        monkeypatch.setattr(Migration, "fetch", lambda self: setattr(
            self.import_info, "status", script.app.ImportStatus.IMPORTED,
        ))
    with pytest.raises((ValueError, RuntimeError, TimeoutError)):
        script.main()
    assert runtime.queries[-1].endswith("SET status_id=11 WHERE request_id=13;")
    comment = next(q for q in runtime.queries if q.startswith("INSERT INTO arms.t_mig_comments"))
    literal = comment.split(", 13, ", 1)[1].split(", CURRENT_TIMESTAMP", 1)[0]
    history = [q for q in runtime.queries if q.startswith("UPDATE arms.t_mig_dim_migration")]
    if failure == "missing_history":
        assert not history
    else:
        assert f"migration_message={literal}" in history[0]
    assert not runtime.migrated
    assert all(c.closed for c in runtime.connections)


@pytest.mark.parametrize("undo_request,status,action", [
    ("requested", script.app.ImportStatus.IMPORTED, "approve"),
    ("approved", script.app.ImportStatus.IMPORTED, "wait"),
    ("pending", script.app.ImportStatus.IMPORTED, "reverse"),
    ("approved", script.app.ImportStatus.UNDOING, "wait"),
    ("approved", script.app.ImportStatus.UNDO_SUCCESS, "wait"),
    ("rejected", script.app.ImportStatus.IMPORTED, "error"),
    ("unknown", script.app.ImportStatus.IMPORTED, "error"),
    ("requested", script.app.ImportStatus.UNDO_FAILED, "error"),
])
def test_reverse_resumes_current_state_without_duplicate_requests(monkeypatch, undo_request, status, action):
    calls = []
    migration = SimpleNamespace(
        id="package:import",
        import_info=SimpleNamespace(status=status, undo_request_status=undo_request),
        fetch=lambda: calls.append("fetch"),
        reverse=lambda **kwargs: calls.append("reverse"),
    )
    target = SimpleNamespace(project_id="target-guid")

    def update(**kwargs):
        assert kwargs == {
            "connection": target, "migration_id": "package:import",
            "body": {"importInfo": {"undoRequestStatus": "approved"}},
            "prefer": "respond-async", "project_id": "target-guid",
        }
        calls.append("approve")

    monkeypatch.setattr(script.app.migration_api, "update_migration", update)
    if action == "error":
        with pytest.raises((ValueError, RuntimeError)):
            script._start_or_resume_reverse(migration, target, "T01 - Tutorial")
    else:
        script._start_or_resume_reverse(migration, target, "T01 - Tutorial")
    assert calls == (["fetch", action] if action in {"approve", "reverse"} else ["fetch"])
