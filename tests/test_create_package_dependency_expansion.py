from __future__ import annotations

from mstrio.types import ObjectSubTypes
import pandas as pd
import pytest
import create_package_validation_loop

from create_package import (
    _build_object_insert_queries,
    _collect_package_dependencies,
    _canonical_object_type_name,
    _is_allowed_dependency_entry,
    _is_shortcut_root_entry,
)


class Shortcut:
    def __init__(self, dependencies):
        self._dependencies = dependencies

    def list_dependencies(self):
        return self._dependencies


def test_creation_empty_report_exits_cleanly(monkeypatch, capsys):
    module = create_package_validation_loop
    monkeypatch.setattr(module, "API_TOKEN", "test-token")
    closed = []
    connection = type("Connection", (), {"close": lambda self: closed.append(True)})()
    monkeypatch.setattr(module, "Connection", lambda *args, **kwargs: connection)
    monkeypatch.setattr(module, "DatasourceInstance", lambda **kwargs: object())
    monkeypatch.setattr(module, "Report", lambda **kwargs: type(
        "Report", (), {"to_dataframe": lambda self: pd.DataFrame()},
    )())

    assert module.main() == 0
    assert closed == [True]
    assert "No requests found in the request report. No package will be created." in capsys.readouterr().out


@pytest.mark.parametrize("source_id", ["A" * 32, "not-a-guid"])
@pytest.mark.parametrize("workstation", [False, True])
@pytest.mark.parametrize("table_prefix", ["arms.", "custom.", ""])
def test_creation_without_target_or_requestor_fields_skips_target_processing(monkeypatch, capsys, source_id, workstation, table_prefix):
    module = create_package_validation_loop
    monkeypatch.setattr(module, "TABLE_PREFIX", table_prefix)
    monkeypatch.setattr(module, "API_TOKEN", "test-token")
    queries = []
    connections = []
    created = []
    frame = pd.DataFrame([{
        "Project@Name": "DEV - Tutorial",
        "Project@ID": source_id,
        "Migration Obj@Content Path": "Public Objects",
        "Migration Obj@Name": "Test Package",
        "Request@ID": 12,
    }])

    def connect(*args, **kwargs):
        connections.append(kwargs)
        return object()

    class FakeDatasource:
        def __init__(self, **kwargs):
            pass

        def execute_query(self, *, query, project_id):
            queries.append(query)

    class FakeMigration:
        id = "package:created"

        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def build_package_config(**kwargs):
            return kwargs["content"]

        @staticmethod
        def create_object_migration(*args, **kwargs):
            created.append(kwargs)

        def trigger_validation(self, **kwargs):
            raise AssertionError("Creation must not trigger target validation.")

    monkeypatch.setattr(module, "Connection", connect)
    connection_data = {"defaultEnvironment": {"url": "https://workstation.invalid"}}
    monkeypatch.setattr(module, "get_connection", connect)
    if workstation:
        monkeypatch.setattr(module, "getpass", lambda prompt: pytest.fail("Workstation must not prompt for a token."))
    monkeypatch.setattr(module, "DatasourceInstance", FakeDatasource)
    monkeypatch.setattr(module, "Report", lambda **kwargs: type("Report", (), {"to_dataframe": lambda self: frame})())
    monkeypatch.setattr(module, "Folder", lambda *args, **kwargs: type("Folder", (), {"get_contents": lambda self: []})())
    monkeypatch.setattr(module, "_resolve_folder_id", lambda *args: "folder")
    monkeypatch.setattr(module, "_collect_package_dependencies", lambda *args, **kwargs: [{
        "id": "B" * 32, "type": "metric", "name": "Metric",
    }])
    monkeypatch.setattr(module, "_delete_existing_migration_if_present", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "Migration", FakeMigration)

    assert module.main(connection_data=connection_data if workstation else None) == 0
    assert len(connections) == 2
    if workstation:
        assert all(c["connection_data"] is connection_data for c in connections)
        assert all("api_token" not in c for c in connections)
    else:
        assert all(c["api_token"] == "test-token" and c["login_mode"] == 4096 for c in connections)
    assert all("username" not in c and "password" not in c for c in connections)
    if source_id == "A" * 32:
        assert connections[1]["project_id"] == source_id
    else:
        assert connections[1]["project_name"] == "DEV - Tutorial"
    assert len(created) == 1
    assert created[0]["project_name"] == "DEV - Tutorial"
    assert any("set status_id=5" in query for query in queries)
    in_progress = [i for i, q in enumerate(queries) if f"set status_id=17 where request_id=12" in q]
    assert in_progress and in_progress[0] < next(i for i, q in enumerate(queries) if "t_mig_dim_objects" in q)
    assert f"update {table_prefix}t_mig_dim_request set status_id=17" in queries[in_progress[0]]
    assert any("migration_obj_id='package:created'" in query for query in queries)
    assert not any("validation_flag" in query or "Validation" in query for query in queries)
    assert any(f"INSERT INTO {table_prefix}t_mig_dim_objects" in query for query in queries)
    if table_prefix != "arms.":
        assert all("arms." not in query for query in queries)
    assert "Target project processing is deferred to import_package.py" in capsys.readouterr().out


@pytest.mark.parametrize("table_prefix", ["arms.", "custom.", ""])
def test_package_creation_failure_records_server_error_with_missing_dependency_guid(monkeypatch, table_prefix):
    module = create_package_validation_loop
    monkeypatch.setattr(module, "TABLE_PREFIX", table_prefix)
    monkeypatch.setattr(module, "API_TOKEN", "test-token")
    queries = []
    frame = pd.DataFrame([{
        "Project@Name": "DEV - Tutorial",
        "Project@ID": "A" * 32,
        "Target Project@NAME": "T01 - Tutorial",
        "Migration Obj@Content Path": "Public Objects",
        "Migration Obj@Name": "Test Package",
        "Request@ID": 12,
    }])
    server_message = (
        f"Object: <Metric id={'B' * 32}> is not found. "
        "Dependency 'Net Sales' cannot be resolved."
    )

    class FakeDatasource:
        def __init__(self, **kwargs):
            pass

        def execute_query(self, *, query, project_id):
            queries.append(query)
            if query.startswith("SELECT tgt_project_id"):
                return {"results": {"data": {"tgt_project_id": [42]}}}

    class FakeMigration:
        @staticmethod
        def build_package_config(**kwargs):
            return kwargs["content"]

        @staticmethod
        def create_object_migration(*args, **kwargs):
            raise RuntimeError(server_message)

    monkeypatch.setattr(module, "Connection", lambda *args, **kwargs: object())
    monkeypatch.setattr(module, "DatasourceInstance", FakeDatasource)
    monkeypatch.setattr(
        module, "Report",
        lambda **kwargs: type("Report", (), {"to_dataframe": lambda self: frame})(),
    )
    monkeypatch.setattr(
        module, "Folder",
        lambda *args, **kwargs: type("Folder", (), {"get_contents": lambda self: []})(),
    )
    monkeypatch.setattr(module, "_resolve_folder_id", lambda *args: "folder")
    monkeypatch.setattr(module, "_collect_package_dependencies", lambda *args, **kwargs: [{
        "id": "C" * 32, "type": "metric", "name": "Metric",
    }])
    monkeypatch.setattr(module, "_delete_existing_migration_if_present", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "Migration", FakeMigration)

    with pytest.raises(RuntimeError, match="Object: <Metric id=") as exc_info:
        module.main()

    assert str(exc_info.value) == server_message
    history_query = next(query for query in queries if query.startswith(f"INSERT INTO {table_prefix}t_mig_dim_migration"))
    comment_query = next(query for query in queries if query.startswith(f"INSERT INTO {table_prefix}t_mig_comments"))
    escaped_message = server_message.replace("'", "''")
    assert "migration_message" in history_query
    assert ", 12, CURRENT_TIMESTAMP, 42, " in history_query
    assert escaped_message in history_query
    assert escaped_message in comment_query
    assert "B" * 32 in history_query
    assert "B" * 32 in comment_query
    lookup = next(q for q in queries if q.startswith("SELECT tgt_project_id"))
    assert f"FROM {table_prefix}t_mig_lu_tgt_project" in lookup
    assert "LOWER('T01 - Tutorial')" in lookup
    if table_prefix != "arms.":
        assert all("arms." not in query for query in queries)


@pytest.mark.parametrize("validation_ok", [True, False])
def test_creation_validates_against_target_and_records_failure(monkeypatch, validation_ok):
    module = create_package_validation_loop
    monkeypatch.setattr(module, "API_TOKEN", "test-token")
    queries, connections = [], []
    frame = pd.DataFrame([{
        "Project@Name": "DEV - Tutorial", "Project@ID": "A" * 32,
        "Target Project@NAME": "T01 - Tutorial", "Migration Obj@Content Path": "Public Objects",
        "Migration Obj@Name": "Test Package", "Request@ID": 12,
    }])
    guid = "D" * 32
    validated = []

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            connections.append(self)

        def close(self):
            self.closed = True

    class FakeDatasource:
        def __init__(self, **kwargs):
            pass

        def execute_query(self, *, query, project_id):
            queries.append(query)
            if query.startswith("SELECT tgt_project_id"):
                return {"results": {"data": {"tgt_project_id": [42]}}}

    class FakeMigration:
        id = "package:created"

        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def build_package_config(**kwargs):
            return kwargs["content"]

        @staticmethod
        def create_object_migration(*args, **kwargs):
            pass

    def validate(migration, *, target_env, target_project_name, **kwargs):
        validated.append((target_env, target_project_name))
        if not validation_ok:
            raise RuntimeError(f"Package validation failed: Object: <Metric id={guid}> is not found")

    monkeypatch.setattr(module, "Connection", FakeConnection)
    monkeypatch.setattr(module, "DatasourceInstance", FakeDatasource)
    monkeypatch.setattr(module, "Report", lambda **kwargs: type("Report", (), {"to_dataframe": lambda self: frame})())
    monkeypatch.setattr(module, "Folder", lambda *args, **kwargs: type("Folder", (), {"get_contents": lambda self: []})())
    monkeypatch.setattr(module, "_resolve_folder_id", lambda *args: "folder")
    monkeypatch.setattr(module, "_collect_package_dependencies", lambda *args, **kwargs: [{
        "id": "C" * 32, "type": "metric", "name": "Metric",
    }])
    monkeypatch.setattr(module, "_delete_existing_migration_if_present", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "Migration", FakeMigration)
    monkeypatch.setattr(module.import_package, "_validate_before_import", validate)

    if validation_ok:
        assert module.main() == 0
        assert any("set status_id=5" in q for q in queries)
        assert any("Package Created with mstrio" in q for q in queries)
    else:
        with pytest.raises(RuntimeError, match=guid):
            module.main()
        assert not any("set status_id=5" in q for q in queries)
        history = next(q for q in queries if q.startswith("INSERT INTO arms.t_mig_dim_migration"))
        comment = next(q for q in queries if q.startswith("INSERT INTO arms.t_mig_comments"))
        assert guid in history and guid in comment
    assert validated[0][1] == "T01 - Tutorial"
    target = connections[-1]
    assert target.kwargs["project_name"] == "T01 - Tutorial" and target.closed


def test_creation_prompts_for_api_token_without_environment_credentials(monkeypatch):
    module = create_package_validation_loop
    monkeypatch.setattr(module, "API_TOKEN", "")
    monkeypatch.setattr(module, "getpass", lambda prompt: "  entered-token  ")
    calls = []
    connection = type("Connection", (), {"close": lambda self: None})()
    monkeypatch.setattr(module, "Connection", lambda *args, **kwargs: calls.append(kwargs) or connection)
    monkeypatch.setattr(module, "DatasourceInstance", lambda **kwargs: object())
    monkeypatch.setattr(module, "Report", lambda **kwargs: type(
        "Report", (), {"to_dataframe": lambda self: pd.DataFrame()},
    )())
    assert module.main() == 0
    assert calls[0]["api_token"] == "entered-token"
    assert calls[0]["login_mode"] == 4096


def test_creation_rejects_empty_token_before_connecting(monkeypatch):
    module = create_package_validation_loop
    monkeypatch.setattr(module, "API_TOKEN", "")
    monkeypatch.setattr(module, "getpass", lambda prompt: " ")
    def fail(*args, **kwargs):
        pytest.fail("Empty tokens must not reach Connection.")
    monkeypatch.setattr(module, "Connection", fail)
    with pytest.raises(ValueError, match="API token is required"):
        module.main()


def test_workstation_connection_failure_does_not_fall_back_to_token(monkeypatch):
    module = create_package_validation_loop
    monkeypatch.setattr(module, "get_connection", lambda **kwargs: None)
    monkeypatch.setattr(module, "getpass", lambda prompt: pytest.fail("No token fallback allowed."))
    with pytest.raises(RuntimeError, match="Unable to establish the Workstation connection"):
        module.main(connection_data={})


def test_collect_package_dependencies_includes_all_relevant_types_and_deduplicates(monkeypatch):
    class FakeObject:
        def __init__(self, *, connection, id, type):
            self.id = id
            self.type = type
            self.name = f"{type}-{id}"
            self.location = "/Project/Objects"
            self.date_created = "2024-01-01"
            self.date_modified = "2024-01-02"

        def list_dependencies(self):
            if self.id == "A":
                return [
                    {"id": "D", "type": "metric", "name": "Revenue Metric"},
                    {"id": "E", "type": "prompt", "name": "Year Prompt"},
                ]
            return []

    monkeypatch.setattr("create_package.Object", FakeObject)

    shortcuts = [
        Shortcut([
            {"id": "A", "type": "report", "name": "Sales Report"},
            {"id": "B", "type": "dataset", "name": "Dataset 1"},
            {"id": "C", "type": "filter", "name": "Region Filter"},
        ]),
        Shortcut([
            {"id": "B", "type": "dataset", "name": "Dataset 1"},
            {"id": "D", "type": "metric", "name": "Revenue Metric"},
            {"id": "E", "type": "prompt", "name": "Year Prompt"},
            {"id": "F", "type": "folder", "name": "Folder"},
        ]),
        Shortcut([
            {"id": "G", "type": "cube", "name": "Sales Cube"},
            {"id": "H", "type": "schema", "name": "Schema"},
        ]),
    ]

    collected = _collect_package_dependencies(shortcuts, connection=object())

    assert {item["id"] for item in collected} == {"A", "B", "C", "D", "E", "G"}
    assert {item["type"] for item in collected} == {"report", "dataset", "filter", "metric", "prompt", "cube"}


def test_collect_package_dependencies_walks_nested_dependency_tree(monkeypatch):
    class FakeObject:
        def __init__(self, *, connection, id, type):
            self.id = id
            self.type = type
            self.name = f"{type}-{id}"
            self.location = "/Project/Objects"
            self.date_created = "2024-01-01"
            self.date_modified = "2024-01-02"

        def list_dependencies(self):
            if self.id == "A":
                return [{"id": "B", "type": "dataset", "name": "Dataset"}]
            if self.id == "B":
                return [{"id": "C", "type": "metric", "name": "Metric"}]
            if self.id == "C":
                return [{"id": "D", "type": "prompt", "name": "Prompt"}]
            if self.id == "D":
                return [{"id": "E", "type": "filter", "name": "Filter"}]
            return []

    monkeypatch.setattr("create_package.Object", FakeObject)

    shortcuts = [Shortcut([{"id": "A", "type": "report", "name": "Report A"}])]

    collected = _collect_package_dependencies(shortcuts, connection=object())

    assert {item["id"] for item in collected} == {"A", "B", "C", "D", "E"}
    assert {item["type"] for item in collected} == {"report", "dataset", "metric", "prompt", "filter"}


def test_collect_package_dependencies_ignores_builtin_system_objects(monkeypatch):
    class FakeObject:
        def __init__(self, *, connection, id, type):
            self.id = id
            self.type = type
            self.name = f"{type}-{id}"
            self.location = "/Project/Objects"
            self.date_created = "2024-01-01"
            self.date_modified = "2024-01-02"

        def list_dependencies(self):
            if self.id == "REAL":
                return [
                    {"id": "M1", "type": "metric", "name": "Revenue Metric"},
                    {"id": "SYS1", "type": "folder", "name": ">"},
                    {"id": "SYS2", "type": "folder", "name": "*"},
                    {"id": "SYS3", "type": "package", "name": "DSS Built-in Package"},
                ]
            return []

    monkeypatch.setattr("create_package.Object", FakeObject)

    shortcuts = [Shortcut([{"id": "REAL", "type": "report", "name": "Transactional Sales Detail Report by City"}])]

    collected = _collect_package_dependencies(shortcuts, connection=object())

    assert {item["id"] for item in collected} == {"REAL", "M1"}
    assert {item["name"] for item in collected} == {"Transactional Sales Detail Report by City", "Revenue Metric"}


def test_collect_package_dependencies_ignores_schema_objects(monkeypatch):
    class FakeObject:
        def __init__(self, *, connection, id, type):
            self.id = id
            self.type = type
            self.name = f"{type}-{id}"
            self.location = "/Project/Objects"
            self.date_created = "2024-01-01"
            self.date_modified = "2024-01-02"

        def list_dependencies(self):
            if self.id == "REPORT":
                return [
                    {"id": "DATASET", "type": "dataset", "name": "Sales Dataset"},
                    {"id": "SCHEMA", "type": "schema", "name": "Project Schema"},
                ]
            if self.id == "DATASET":
                return [{"id": "METRIC", "type": "metric", "name": "Revenue"}]
            return []

    monkeypatch.setattr("create_package.Object", FakeObject)

    shortcuts = [Shortcut([{"id": "REPORT", "type": "report", "name": "Sales Report"}])]

    collected = _collect_package_dependencies(shortcuts, connection=object())

    assert {item["id"] for item in collected} == {"REPORT", "DATASET", "METRIC"}
    assert all(item["type"] != "schema" for item in collected)


def test_collect_package_dependencies_resolves_shortcut_target_info(monkeypatch):
    class TargetObject:
        def __init__(self, *, connection, id, type):
            self.id = id
            self.type = type
            self.name = f"{type}-{id}"
            self.location = "/Project/Objects"
            self.date_created = "2024-01-01"
            self.date_modified = "2024-01-02"

        def list_dependencies(self):
            return [
                {"id": "D1", "type": "dataset", "name": "Dataset 1"},
                {"id": "M1", "type": "metric", "name": "Revenue"},
            ]

    class ShortcutWithTarget:
        def __init__(self, target_info):
            self.target_info = target_info
            self.type = "shortcut"
            self.id = target_info["id"]
            self.name = "Shortcut Placeholder"

        def list_dependencies(self):
            return []

    monkeypatch.setattr("create_package.Object", TargetObject)

    shortcut = ShortcutWithTarget({"id": "R1", "type": "report", "name": "Sales report"})
    collected = _collect_package_dependencies([shortcut], connection=object())

    assert {item["id"] for item in collected} == {"R1", "D1", "M1"}
    assert {item["type"] for item in collected} == {"report", "dataset", "metric"}


def test_is_allowed_dependency_entry_accepts_enum_and_numeric_report_types():
    assert _is_allowed_dependency_entry({"id": "R1", "type": 3, "name": "Sales Report"}) is True
    assert _is_allowed_dependency_entry({"id": "R2", "type": "report_definition", "name": "Sales Report"}) is True


def test_collect_package_dependencies_deduplicates_same_object_by_id_case_insensitive(monkeypatch):
    class FakeObject:
        def __init__(self, *, connection, id, type):
            self.id = id
            self.type = type
            self.name = f"{type}-{id}"
            self.location = "/Project/Objects"
            self.date_created = "2024-01-01"
            self.date_modified = "2024-01-02"

        def list_dependencies(self):
            if self.id.lower() == "report":
                return [
                    {"id": "abc123", "type": "dataset", "name": "Dataset 1"},
                    {"id": "ABC123", "type": "dataset", "name": "Dataset 1"},
                ]
            return []

    monkeypatch.setattr("create_package.Object", FakeObject)

    shortcut = type("Shortcut", (), {"list_dependencies": lambda self: [{"id": "report", "type": "report", "name": "Sales report"}]})()
    collected = _collect_package_dependencies([shortcut], connection=object())

    assert len(collected) == 2
    assert {item["id"] for item in collected} == {"REPORT", "ABC123"}


def test_collect_package_dependencies_ignores_builtin_metric_functions(monkeypatch):
    class FakeObject:
        def __init__(self, *, connection, id, type):
            self.id = id
            self.type = type
            self.name = f"{type}-{id}"
            self.location = "/Project/Objects"
            self.date_created = "2024-01-01"
            self.date_modified = "2024-01-02"

        def list_dependencies(self):
            if self.id == "REPORT":
                return [
                    {"id": "M1", "type": "metric", "name": "Revenue"},
                    {"id": "M2", "type": "metric", "name": "Average"},
                    {"id": "M3", "type": "metric", "name": "Count"},
                    {"id": "M4", "type": "metric", "name": "Total"},
                ]
            return []

    monkeypatch.setattr("create_package.Object", FakeObject)

    shortcut = type("Shortcut", (), {"list_dependencies": lambda self: [{"id": "REPORT", "type": "report", "name": "Sales report"}]})()
    collected = _collect_package_dependencies([shortcut], connection=object())

    assert {item["id"] for item in collected} == {"REPORT", "M1"}
    assert all(item["name"] != "Average" for item in collected)


def test_is_allowed_dependency_entry_excludes_schema_and_builtins():
    assert _is_allowed_dependency_entry({"id": "SCHEMA", "type": "schema", "name": "Project Schema"}) is False
    assert _is_allowed_dependency_entry({"id": "M1", "type": "metric", "name": "Revenue"}) is True
    assert _is_allowed_dependency_entry({"id": "X", "type": "folder", "name": ">"}) is False


def test_is_shortcut_root_entry_allows_document_roots():
    assert _is_shortcut_root_entry({"id": "DOC1", "type": 55, "name": "Transactional Sales Detail Report by City"}) is True
    assert _is_shortcut_root_entry({"id": "M1", "type": 4, "name": "Revenue"}) is True
    assert _is_shortcut_root_entry({"id": "SCHEMA", "type": "schema", "name": "Project Schema"}) is False


def test_is_allowed_dependency_entry_filters_metric_agg_enum_builtin_functions():
    assert _canonical_object_type_name(ObjectSubTypes.METRIC_AGG) == "agg_metric"
    assert _is_allowed_dependency_entry({"id": "FUNC1", "type": ObjectSubTypes.METRIC_AGG, "name": "Aggregation"}) is False
    assert _is_allowed_dependency_entry({"id": "FUNC2", "type": ObjectSubTypes.METRIC, "name": "Revenue"}) is True


def test_is_allowed_dependency_entry_filters_numeric_metric_builtin_functions():
    assert _is_allowed_dependency_entry({"id": "FUNC3", "type": 4, "name": "Aggregation"}) is False
    assert _is_allowed_dependency_entry({"id": "FUNC4", "type": 4, "name": "Revenue"}) is True


def test_build_object_insert_queries_escapes_single_quotes():
    query = _build_object_insert_queries([
        {
            "id": "ABC",
            "type": 4,
            "name": "Cost of a Day's Sales",
            "location": "/Project/Objects/Cost of a Day's Sales",
            "date_created": "2024-01-01 00:00:00+00:00",
            "date_modified": "2024-01-02 00:00:00+00:00",
        }
    ], 99)[0]

    assert "'metric'" in query
    assert "Cost of a Day''s Sales" in query
    assert "Cost of a Day's Sales" not in query.replace("''", "")
