from __future__ import annotations

from mstrio.types import ObjectSubTypes

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
