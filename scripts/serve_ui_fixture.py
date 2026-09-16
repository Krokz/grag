"""Disposable browser-test server; never opens a user's graph or configuration."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import closing
from pathlib import Path

import uvicorn

from grag.api.main import create_app
from grag.config import GragConfig
from grag.core.types import DefineSchemaRequest, UpsertNodesRequest
from grag.service import GragService


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="grag-ui-") as temporary:
        root = Path(temporary)
        for name in ("alpha", "beta", "sources"):
            with closing(
                GragService(
                    GragConfig(
                        db_path=root / f"{name}.lbdb", buffer_pool_size=128 * 1024**2
                    )
                )
            ) as service:
                service.engine.execute_write(
                    "CREATE NODE TABLE Module(id STRING PRIMARY KEY, name STRING, path STRING, code_coverage STRING, _source STRING, _source_state STRING)"
                )
                source = root / f"{name}.go"
                source.write_text("package fixture\nfunc Run() {}\n")
                service.engine.execute_write(
                    "CREATE (:Module {id:'worker.go', name:'worker', path:'worker.go', code_coverage:$coverage, _source:$source, _source_state:'current'})",
                    {
                        "source": str(source),
                        "coverage": json.dumps(
                            {
                                "language": "go",
                                "mode": "partial_static",
                                "counts": {"calls_resolved": 2, "calls_unresolved": 5},
                                "examples": [
                                    {
                                        "kind": "calls",
                                        "line": 2,
                                        "reason": "Synthetic unresolved target",
                                    }
                                ],
                                "limits": "Fixture diagnostics; empty edges do not prove absence.",
                            }
                        ),
                    },
                )
                if name == "sources":
                    continue
                service.define_schema(
                    DefineSchemaRequest.model_validate(
                        {
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
                                    "name": "Dependency",
                                    "primary_key": "name",
                                    "properties": [
                                        {"name": "name"},
                                        {"name": "version"},
                                        {"name": "purpose"},
                                    ],
                                },
                                {
                                    "name": "Concept",
                                    "properties": [
                                        {"name": "id"},
                                        {"name": "title"},
                                        {"name": "body"},
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
                            ],
                            "rel_tables": [
                                {
                                    "name": "SUPPORTED_BY",
                                    "from_label": "Decision",
                                    "to_label": "Module",
                                }
                            ],
                        }
                    )
                )
                service.upsert_nodes(
                    UpsertNodesRequest.model_validate(
                        {
                            "nodes": [
                                {
                                    "label": "Decision",
                                    "key": "Use local storage",
                                    "properties": {
                                        "summary": f"{name} context stays on this machine.",
                                        "rationale": "Keep setup small and queries available offline.",
                                    },
                                    "source": "docs/architecture.md",
                                    "evidence": {
                                        "actor": "Fixture agent",
                                        "reason": "Initial project choice",
                                    },
                                },
                                {
                                    "label": "Dependency",
                                    "key": "ladybug",
                                    "properties": {
                                        "version": "0.20.1",
                                        "purpose": "Embedded graph storage fixture",
                                    },
                                },
                                {
                                    "label": "Concept",
                                    "key": "events",
                                    "properties": {
                                        "title": "Event pipeline",
                                        "body": "Events enter through validation and then reach the worker.",
                                    },
                                    "source": "src/worker.go",
                                },
                                {
                                    "label": "Task",
                                    "key": "investigate",
                                    "properties": {
                                        "title": "Investigate duplicate events",
                                        "body": "Compare worker retries with event IDs.",
                                        "status": "open",
                                    },
                                },
                                {
                                    "label": "Task",
                                    "key": "done",
                                    "properties": {
                                        "title": "Document setup",
                                        "status": "done",
                                    },
                                },
                                {
                                    "label": "Finding",
                                    "key": 42,
                                    "properties": {
                                        "description": "Numeric primary keys are supported."
                                    },
                                },
                            ],
                            "edges": [
                                {
                                    "type": "SUPPORTED_BY",
                                    "from_label": "Decision",
                                    "from_key": "Use local storage",
                                    "to_label": "Module",
                                    "to_key": "worker.go",
                                }
                            ],
                        }
                    )
                )
        app = create_app(
            GragConfig(db_dir=root, buffer_pool_size=128 * 1024**2, mcp_path="/mcp")
        )
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=int(os.environ.get("GRAG_UI_TEST_PORT", "47836")),
        )


if __name__ == "__main__":
    main()
