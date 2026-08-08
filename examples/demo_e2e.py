"""End-to-end demo: open examples/knowledge.lbdb and answer natural questions
with service.search_knowledge (FTS seeds + 1-hop graph expansion + packed
context). Run examples/build_example.py first.

    .venv/bin/python examples/build_example.py && .venv/bin/python examples/demo_e2e.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from grag.config import GragConfig
from grag.core.types import SearchRequest
from grag.service import GragService

DB_PATH = Path(__file__).resolve().parent / "knowledge.lbdb"

QUESTIONS = [
    "Who owns the ingestion gateway?",
    "What caused the March outage?",
    "Which customers use Pulse?",
]


def main() -> int:
    if not DB_PATH.exists():
        print(f"{DB_PATH} not found - run examples/build_example.py first.")
        return 1
    service = GragService(GragConfig(db_path=DB_PATH))
    try:
        for q in QUESTIONS:
            resp = service.search_knowledge(SearchRequest(query=q, top_k=4, hops=1))
            print(f"\n=== {q}")
            print("seeds:", ", ".join(s.node.id for s in resp.seeds) or "(none)")
            print(resp.context or "(empty context)")
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
