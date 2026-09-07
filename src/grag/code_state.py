"""Durable source generations and the exact scope/options used to index them."""

from __future__ import annotations

import hashlib
import json
import subprocess
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from grag.core.engine import Engine
from grag.core.errors import GragError
from grag.core.limits import bounded_sources, read_source
from grag.core.types import CodeIngestRequest

INDEX_COLUMNS = ("_index_options", "_index_generation", "_index_error")


@dataclass(frozen=True)
class Fingerprint:
    head: str
    tree: str

    @property
    def generation(self) -> str:
        return hashlib.sha256(f"{self.head}\0{self.tree}".encode()).hexdigest()


@dataclass
class SourceScan:
    fingerprint: Fingerprint
    files: dict[str, str] = field(default_factory=dict)
    excluded: set[str] = field(default_factory=set)


def options_json(req: CodeIngestRequest) -> str:
    return json.dumps(req.model_dump(), sort_keys=True, separators=(",", ":"))


def saved_request(root: Path, encoded: str) -> CodeIngestRequest:
    """Never substitute defaults for a missing/corrupt saved indexing policy."""
    try:
        value = json.loads(encoded)
        if not isinstance(value, dict) or set(value) != set(
            CodeIngestRequest.model_fields
        ):
            raise ValueError("missing indexing options")
        req = CodeIngestRequest.model_validate(value, strict=True)
        if not req.paths or req.max_file_kb <= 0:
            raise ValueError("empty scope or invalid file limit")
        for item in req.paths:
            path = Path(item)
            if not path.is_absolute() or (path != root and path.parent != root):
                raise ValueError("saved scope does not belong to its indexed root")
        return req
    except (ValueError, TypeError) as exc:
        raise GragError(
            f"Invalid saved indexing options for {root}: {exc}",
            hint="Run ingest_code once with the intended paths, calls, and max_file_kb.",
        ) from exc


def index_records(engine: Engine) -> dict[str, dict]:
    tables = engine.execute("CALL SHOW_TABLES() RETURN name").rows
    if not any(row[0] == "Repo" for row in tables):
        return {}
    columns = {
        row[1] for row in engine.execute("CALL TABLE_INFO('Repo') RETURN *").rows
    }
    # A graph may use Repo for ordinary authored knowledge. A completed ingest
    # stamp or legacy code-containment edges distinguish local code indexes.
    if not {"id", "path"}.issubset(columns):
        return {}
    select = [f"r.{name}" if name in columns else "NULL" for name in INDEX_COLUMNS]
    stamp = "r.ingested_at" if "ingested_at" in columns else "NULL"
    rows = engine.execute(
        f"MATCH (r:Repo) RETURN r.path, {stamp}, {', '.join(select)}"
    ).rows
    legacy_paths = [path for path, ingested, *_ in rows if path and ingested is None]
    legacy_roots = set()
    if legacy_paths and {"Module", "CONTAINS_REPO_MODULE"}.issubset(
        {row[0] for row in tables}
    ):
        legacy_roots = {
            row[0]
            for row in engine.execute(
                "MATCH (r:Repo)-[:CONTAINS_REPO_MODULE]->(:Module) "
                "WHERE r.path IN $paths RETURN DISTINCT r.path",
                {"paths": legacy_paths},
            ).rows
        }
    return {
        str(path): dict(zip(INDEX_COLUMNS, values, strict=True))
        for path, ingested, *values in rows
        if path and (ingested is not None or path in legacy_roots)
    }


def registered_repo_ids(engine: Engine) -> dict[str, str]:
    """Reuse persisted index identities, including after an explicit relocation."""
    records = index_records(engine)
    if not records:
        return {}
    columns = {row[1] for row in engine.execute("CALL TABLE_INFO('Repo') RETURN *").rows}
    stamp = "r.ingested_at" if "ingested_at" in columns else "NULL"
    rows = engine.execute(f"MATCH (r:Repo) RETURN r.path, r.id, {stamp}").rows
    result: dict[str, str] = {}
    for path, key, ingested in rows:
        if path not in records:
            continue
        if ingested is None:
            tables = {row[0] for row in engine.execute("CALL SHOW_TABLES() RETURN name").rows}
            if "CONTAINS_REPO_MODULE" not in tables or not engine.execute(
                "MATCH (r:Repo {id: $id})-[:CONTAINS_REPO_MODULE]->(:Module) RETURN r.id LIMIT 1",
                {"id": key},
            ).rows:
                continue
        if path in result and result[path] != key:
            raise GragError(f"Multiple code indexes claim {path}; reconcile their identities explicitly")
        result[str(path)] = str(key)
    return result


def _head(root: Path) -> str:
    try:
        result = subprocess.run(  # noqa: S603 — fixed git argv over a local path
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],  # noqa: S607
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GragError(f"Cannot inspect git state for {root}: {exc}") from exc
    if result.returncode == 0:
        return result.stdout.decode().strip()
    error = result.stderr.decode(errors="replace")
    # Plain directories and unborn repositories still have verifiable content.
    if "not a git repository" in error or "Needed a single revision" in error:
        return ""
    raise GragError(f"Cannot inspect git state for {root}: {error.strip()}")


@bounded_sources
def scan_sources(root: Path, req: CodeIngestRequest) -> SourceScan:
    from grag import __version__
    from grag.ingest.code import _PARSERS, _walk

    if not root.is_dir():
        raise GragError(
            f"Indexed root is missing or inaccessible: {root}",
            hint="Restore the path or explicitly re-index the relocated checkout.",
        )
    head = _head(root)
    files: dict[str, str] = {}
    excluded: set[str] = set()
    errors: list[str] = []
    for walked_root, walked_file in _walk(
        [Path(p) for p in req.paths],
        req.max_file_kb,
        [],
        excluded=excluded,
        errors=errors,
    ):
        if walked_file.suffix.lower() not in _PARSERS:
            continue
        file = walked_root.resolve() / walked_file.relative_to(walked_root)
        try:
            files[str(file)] = hashlib.sha256(read_source(file, req.max_file_kb * 1024)).hexdigest()
        except OSError as exc:
            errors.append(f"Cannot read {file}: {exc}")
    if errors:
        raise GragError(
            errors[0],
            hint="Resolve the source access error before requiring fresh context.",
        )
    digest = hashlib.sha256(
        f"grag-source-v1\0{__version__}\0{options_json(req)}".encode()
    )
    for name, content in sorted(files.items()):
        digest.update(json.dumps([name, content], ensure_ascii=True).encode())
    # Size exclusions are part of the scope: crossing the threshold changes it.
    for name in sorted(excluded):
        digest.update(json.dumps([name, "excluded"]).encode())
    return SourceScan(Fingerprint(head, digest.hexdigest()), files, excluded)


def fingerprint(root: Path) -> Fingerprint | None:
    """Convenience check of a whole directory with default indexing options."""
    if not root.is_dir():
        return None
    return scan_sources(
        root.resolve(), CodeIngestRequest(paths=[str(root.resolve())])
    ).fingerprint


def scope_requests(
    roots: dict[str, Path],
    input_paths: list[Path],
    req: CodeIngestRequest,
    existing: dict[str, dict],
) -> dict[str, CodeIngestRequest]:
    scopes = {}
    for key, root in roots.items():
        paths = {
            str(p.resolve())
            for p in input_paths
            if p.resolve() == root or (p.is_file() and p.resolve().parent == root)
        }
        encoded = existing.get(str(root), {}).get("_index_options")
        if encoded:
            with suppress(
                GragError
            ):  # An explicit ingest can replace an invalid policy.
                paths.update(saved_request(root, encoded).paths)
        if str(root) in paths:
            paths = {str(root)}
        scopes[key] = req.model_copy(update={"paths": sorted(paths)})
    return scopes


def verify_ingest(
    engine: Engine,
    roots: dict[str, Path],
    scopes: dict[str, CodeIngestRequest],
    parsed_hashes: dict[str, str],
    heads: dict[str, str],
) -> dict[str, tuple[str | None, str | None]]:
    """Verify the bytes actually parsed, before publishing their graph transaction."""
    verified: dict[str, tuple[str | None, str | None]] = {}
    for key, root in roots.items():
        try:
            scan = scan_sources(root, scopes[key])
            parsed = {
                p: h for p, h in parsed_hashes.items() if Path(p).is_relative_to(root)
            }
            if parsed != scan.files or scan.fingerprint.head != heads[key]:
                raise GragError(
                    "Source changed during indexing, a file failed parsing, or the saved scope needs a full refresh."
                )
            if scan.excluded:
                rows = engine.execute(
                    "MATCH (m:Module) WHERE m._source IN $paths RETURN m._source LIMIT 1",
                    {"paths": sorted(scan.excluded)},
                ).rows
                if rows:
                    raise GragError(
                        f"An indexed source now exceeds max_file_kb: {rows[0][0]}",
                        hint="Increase max_file_kb or reconcile the excluded source explicitly.",
                    )
            verified[key] = (scan.fingerprint.generation, None)
        except GragError as exc:
            verified[key] = (None, str(exc))
    return verified


def record_generations(
    engine: Engine,
    scopes: dict[str, CodeIngestRequest],
    verified: dict[str, tuple[str | None, str | None]],
) -> None:
    for key, req in scopes.items():
        generation, error = verified[key]
        # Keep the last successful generation on partial ingests; _index_error
        # explicitly prevents that historical success from claiming freshness.
        engine.execute_write(
            "MATCH (r:Repo {id:$key}) SET r._index_options=$policy_json, "
            "r._index_error=$failure_text, r._index_generation=CASE WHEN $source_gen IS NULL "
            "THEN r._index_generation ELSE $source_gen END",
            {
                "key": key,
                "policy_json": options_json(req),
                "failure_text": error,
                "source_gen": generation,
            },
        )
