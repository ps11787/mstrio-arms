from __future__ import annotations

from create_package import _collect_package_dependencies


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
