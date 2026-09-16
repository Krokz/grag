"""UI memory browsing reuses evidence semantics without requiring a preset schema."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from grag.api.main import create_app
from grag.config import GragConfig
from grag.core.limits import WORK_LIMITS


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        GragConfig(db_path=tmp_path / "ui.lbdb", buffer_pool_size=128 * 1024**2)
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/schema/define",
            json={
                "node_tables": [
                    {
                        "name": "Decision",
                        "primary_key": "name",
                        "properties": [
                            {"name": "name"},
                            {"name": "summary"},
                            {"name": "rationale"},
                        ],
                    },
                    {
                        "name": "Task",
                        "properties": [
                            {"name": "id"},
                            {"name": "title"},
                            {"name": "body"},
                            {"name": "status"},
                        ],
                    },
                    {
                        "name": "Finding",
                        "primary_key": "number",
                        "properties": [
                            {"name": "number", "type": "INT64"},
                            {"name": "description"},
                        ],
                    },
                ]
            },
        )
        assert response.status_code == 200, response.text
        response = client.post(
            "/api/nodes/upsert",
            json={
                "nodes": [
                    {
                        "label": "Decision",
                        "key": "local-first",
                        "properties": {
                            "summary": "Keep context local",
                            "rationale": "Fewer services",
                        },
                        "source": "docs/architecture.md",
                        "evidence": {},
                    },
                    {
                        "label": "Task",
                        "key": "one",
                        "properties": {
                            "title": "Review citations",
                            "body": "Verify source references",
                            "status": "open",
                        },
                    },
                    {
                        "label": "Task",
                        "key": "two",
                        "properties": {"title": "Finished work", "status": "done"},
                    },
                    {
                        "label": "Task",
                        "key": "three",
                        "properties": {
                            "title": "Custom workflow",
                            "status": "waiting_for_team",
                        },
                    },
                    {
                        "label": "Finding",
                        "key": 42,
                        "properties": {
                            "description": "Works with a custom numeric key"
                        },
                    },
                ]
            },
        )
        assert response.status_code == 200, response.text
        yield client


def read(client, **kwargs):
    response = client.post("/api/memories", json=kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def revise(client, key="local-first", **kwargs):
    old = client.post(
        "/api/query", json={"cypher": f"MATCH (n:Decision {{name:'{key}'}}) RETURN n"}
    ).json()["rows"][0][0]
    response = client.post(
        "/api/nodes/upsert",
        json={
            "nodes": [
                {
                    "label": "Decision",
                    "key": key,
                    "expected_revision": old["_revision"],
                    **kwargs,
                }
            ]
        },
    )
    assert response.status_code == 200, response.text


def test_custom_schema_paging_and_open_task_semantics(client):
    result = read(client, limit=2)
    assert result["total"] == 5
    assert result["next_offset"] == 2
    pages = [
        *result["items"],
        *read(client, offset=2, limit=2)["items"],
        *read(client, offset=4, limit=2)["items"],
    ]
    assert len({item["id"] for item in pages}) == 5
    assert next(item for item in pages if item["label"] == "Finding")["key"] == 42
    assert [item["id"] for item in read(client, view="tasks")["items"]] == ["Task:one"]
    assert read(client, label="Task")["total"] == 3  # unknown statuses stay inspectable


def test_literal_search_matches_beyond_list_excerpt_and_never_executes_input(client):
    text = "long context " * 300 + "needle<&🧭"
    revise(client, properties={"summary": text})
    result = read(client, query="needle<&🧭")
    assert result["total"] == 1
    assert "needle" not in result["items"][0]["preview"]
    assert len(result["items"][0]["preview"]) <= 320
    assert read(client, query="'); MATCH (n) DELETE n; //")["total"] == 0
    assert read(client)["total"] == 5


@pytest.mark.parametrize(
    "evidence,reason",
    [
        ({"state": "retracted"}, "retracted"),
        ({"review": "disputed"}, "disputed"),
        ({"expires_at": "2000-01-01T00:00:00Z"}, "expired"),
    ],
)
def test_lifecycle_is_shared_with_retrieval(client, evidence, reason):
    revise(client, evidence=evidence)
    assert read(client, label="Decision")["total"] == 0
    item = read(client, label="Decision", include_inactive=True)["items"][0]
    assert item["excluded_reason"] == reason


def test_recent_changes_use_latest_history_with_creation_fallback(client):
    revise(
        client,
        properties={"summary": "Revised choice"},
        evidence={"actor": "UI test", "reason": "New evidence"},
    )
    result = read(client, view="recent")
    assert result["items"][0]["id"] == "Decision:local-first"
    assert result["items"][0]["change_kind"] == "recorded"
    assert all(item["change_kind"] == "created" for item in result["items"][1:])
    # Browse only needs compact metadata; it doesn't fetch any history snapshots.
    assert read(client, label="Decision")["items"][0]["tracked"]


@pytest.mark.parametrize(
    "marker",
    ["_document_owner", "_document_state", "_document_identity", "_document_file"],
)
def test_generated_code_and_custom_document_rows_are_explicitly_inspectable(
    client, marker
):
    engine = client.app.state.service.engine
    engine.execute_write(
        "CREATE NODE TABLE Module(id STRING PRIMARY KEY, name STRING, _source_state STRING)"
    )
    engine.execute_write(
        "CREATE (:Module {id:'module',name:'worker',_source_state:'current'})"
    )
    engine.execute_write(
        f"ALTER TABLE Finding ADD {marker} STRING DEFAULT CAST(NULL AS STRING)"
    )
    engine.execute_write(
        f"CREATE (:Finding {{number:99,description:'Generated source chunk',{marker}:'docs'}})"
    )
    assert read(client)["total"] == 5
    assert read(client, label="Module")["items"][0]["managed"]
    assert read(client, label="Finding")["total"] == 2
    assert next(
        item for item in read(client, label="Finding")["items"] if item["key"] == 99
    )["managed"]


def test_bad_label_and_admission_limits_do_not_leak_internal_tables(client):
    for label in ("_grag_evidence_history", "Decision) RETURN n //", "Missing"):
        assert client.post("/api/memories", json={"label": label}).status_code == 400
    for request in (
        {"limit": 101},
        {"offset": -1},
        {"query": "x" * 257},
        {"freshness": "invalid"},
    ):
        assert client.post("/api/memories", json=request).status_code == 422


def test_scan_budget_failure_does_not_look_like_an_empty_list(client, monkeypatch):
    # Prime schema so this limit specifically interrupts node scanning.
    read(client)
    monkeypatch.setitem(WORK_LIMITS, "result_rows", 1)
    response = client.post("/api/memories", json={})
    assert response.status_code == 413
    assert response.json()["code"] == "resource_limit"
    assert "items" not in response.json()


def test_new_ui_paths_require_authentication_and_follow_selected_database(tmp_path):
    from grag.core.engine import Engine

    root = tmp_path / "graphs"
    root.mkdir()
    for name in ("alpha", "beta"):
        with Engine(
            GragConfig(db_path=root / f"{name}.lbdb", buffer_pool_size=128 * 1024**2)
        ) as engine:
            engine.execute_write("CREATE NODE TABLE Note(id STRING PRIMARY KEY)")
            engine.execute_write("CREATE (:Note {id:$value})", {"value": name})
    app = create_app(
        GragConfig(db_dir=root, buffer_pool_size=128 * 1024**2, api_token="fixture")  # noqa: S106
    )
    with TestClient(app) as client:
        assert client.post("/api/memories?db=beta", json={}).status_code == 401
        headers = {"Authorization": "Bearer fixture"}
        response = client.post("/api/memories?db=beta", json={}, headers=headers)
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == ["Note:beta"]
        status = client.get("/api/index/status?db=beta", headers=headers)
        assert status.status_code == 200
        assert "embedding" in status.json()
        assert "alpha" not in json.dumps(response.json()["items"])
