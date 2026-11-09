from types import SimpleNamespace

import pandas as pd

import create_package_workstation as script
from mstrio.object_management.migration.package import ValidationStatus


def test_configured_credentials_are_used_for_package_and_target_validation(monkeypatch):
    connections = []
    queries = []
    validation_calls = []
    frame = pd.DataFrame([{
        "Project@Name": "Source",
        "Project@ID": "A" * 32,
        "Migration Obj@Content Path": "Public Objects",
        "Migration Obj@Name": "Test Package",
        "Migration Obj@GUID": "",
        "Request@ID": 12,
        "Target Project@NAME": "Target",
    }])

    def connect(*args, **kwargs):
        connection = object()
        connections.append((args, kwargs, connection))
        return connection

    class FakeDatasource:
        def __init__(self, **kwargs):
            pass

        def execute_query(self, *, query, project_id):
            queries.append(query)

    class FakeMigration:
        id = "package:created"
        validation = SimpleNamespace(
            status=ValidationStatus.VALIDATED,
            progress=100,
            message=None,
            creation_date=None,
            last_update_date=None,
        )

        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def build_package_config(**kwargs):
            return kwargs["content"]

        @staticmethod
        def create_object_migration(*args, **kwargs):
            pass

        def trigger_validation(self, **kwargs):
            validation_calls.append(kwargs)

    monkeypatch.setattr(script, "ENABLE_VALIDATION", True)
    monkeypatch.setattr(script, "Connection", connect)
    monkeypatch.setattr(script, "DatasourceInstance", FakeDatasource)
    monkeypatch.setattr(
        script,
        "Report",
        lambda **kwargs: SimpleNamespace(to_dataframe=lambda: frame),
    )
    monkeypatch.setattr(
        script,
        "Folder",
        lambda *args, **kwargs: SimpleNamespace(get_contents=lambda: []),
    )
    monkeypatch.setattr(script, "_resolve_folder_id", lambda *args: "folder")
    monkeypatch.setattr(
        script,
        "_collect_package_dependencies",
        lambda *args, **kwargs: [{
            "id": "B" * 32,
            "type": "metric",
            "name": "Metric",
            "location": "Public Objects",
            "date_created": None,
            "date_modified": None,
        }],
    )
    monkeypatch.setattr(script, "_delete_existing_migration_if_present", lambda *args, **kwargs: None)
    monkeypatch.setattr(script, "Migration", FakeMigration)

    assert script.main() == 0

    assert len(connections) == 3
    assert all(call[0] == (script.BASE_URL, script.LOGIN, script.PASSWORD) for call in connections)
    assert connections[0][1]["project_id"] == script.PROJECT_ID
    assert connections[1][1]["project_id"] == "A" * 32
    assert connections[2][1]["project_name"] == "Target"
    assert validation_calls == [{
        "target_env": connections[2][2],
        "target_project_name": "Target",
    }]
    assert any("Package Created with mstrio" in query for query in queries)
