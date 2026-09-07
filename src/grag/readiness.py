"""Isolated install checks. Normal checks never download or open a user DB."""

from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from grag.config import GragConfig


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _probe(kind: str, config: GragConfig, *, prepare: bool, timeout: float) -> dict:
    """A bad native binary must not take the diagnostic command down with it."""
    with tempfile.TemporaryDirectory(prefix="grag-doctor-") as directory:
        root = Path(directory)
        result_path = root / "result.json"
        log_path = root / "probe.log"
        payload = {"kind": kind, "prepare": prepare,
                   "embedder": config.embedder.model_dump() if config.embedder else None}
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "HF_HUB_DISABLE_TELEMETRY": "1",
               "DO_NOT_TRACK": "1"}
        if not prepare:
            env["HF_HUB_OFFLINE"] = "1"
        try:
            with log_path.open("wb") as log:
                result = subprocess.run(  # noqa: S603 — fixed module, same interpreter, JSON over stdin
                    [sys.executable, "-m", "grag.readiness", str(result_path)],
                    input=json.dumps(payload), text=True, encoding="utf-8",
                    stdout=log, stderr=log, cwd=root, env=env, timeout=timeout, check=False,
                )
            if result.returncode == 0 and result_path.is_file():
                return json.loads(result_path.read_text(encoding="utf-8"))
            detail = f"probe exited with code {result.returncode} (native crash or startup failure)"
        except subprocess.TimeoutExpired:
            detail = f"probe timed out after {timeout:g}s"
        except OSError as exc:
            detail = f"could not start probe: {exc}"
        with log_path.open("rb") as log:
            log.seek(max(0, log_path.stat().st_size - 2048))
            tail = log.read().decode("utf-8", errors="replace").strip()
        return {"status": "unavailable", "detail": f"{detail}. {tail}".strip()}


def check_install(config: GragConfig, *, prepare: bool = False, timeout: float | None = None) -> list[dict]:
    """Report capabilities separately from installed optional packages."""
    timeout = timeout if timeout is not None else (300.0 if prepare else 30.0)
    checks: list[dict] = []

    def add(kind: str, label: str, *, required: bool = True) -> None:
        if prepare:
            print(f"Checking/preparing {label} ...", file=sys.stderr, flush=True)
        try:
            result = _probe(kind, config, prepare=prepare, timeout=timeout)
        except OSError as exc:
            result = {"status": "unavailable", "detail": f"Cannot create isolated probe workspace: {exc}. Check temporary-directory permissions."}
        checks.append({"key": kind, "label": label, "required": required, **result})

    add("engine", "core engine (ladybug)")
    if checks[0]["status"] == "ready":
        add("fts", "FTS search extension")
        add("vector", "VECTOR extension (legacy index migration)", required=False)
    else:
        checks.append({"key": "fts", "label": "FTS search extension", "required": True,
                       "status": "unavailable", "detail": "Fix the native engine before checking extensions."})
    if config.embedder and config.embedder.provider == "fastembed":
        add("model", f"local embeddings ({config.embedder.model})")
    else:
        remote = config.embedder is not None
        checks.append({"key": "model", "label": "embeddings", "required": False,
                       "status": "unverified" if remote else "disabled",
                       "detail": "remote provider configured; no request sent" if remote else
                       f"not configured; FTS is the default (fastembed package {'installed' if _installed('fastembed') else 'not installed'})"})
    checks.append({"key": "python", "label": "Python code parsing", "required": True,
                   "status": "ready", "detail": "stdlib ast; no grammar download"})
    if _installed("tree_sitter") and checks[0]["status"] == "ready":
        from grag.ingest.code_ts import _SUFFIX_LANGUAGES

        # Every distinct grammar supported by grag, not every grammar in the pack.
        unique = {spec: suffix for suffix, spec in _SUFFIX_LANGUAGES.items()}
        for (_, _, language), suffix in sorted(unique.items()):
            add(f"grammar:{suffix}", f"code grammar ({language}, {suffix})")
    elif _installed("tree_sitter"):
        checks.append({"key": "grammars", "label": "tree-sitter code parsing", "required": False,
                       "status": "unverified", "detail": "package installed; fix the native engine before checking code ingestion"})
    else:
        checks.append({"key": "grammars", "label": "tree-sitter code parsing", "required": False,
                       "status": "disabled", "detail": "not installed; install 'gragdb[code]' for non-Python code"})
    return checks


def install_ready(checks: list[dict]) -> bool:
    return all(not c["required"] or c["status"] == "ready" for c in checks)


def _native_check(kind: str, prepare: bool) -> str:
    from grag.core.engine import Engine

    with tempfile.TemporaryDirectory(prefix="grag-probe-db-") as temporary:
        engine = Engine(GragConfig(db_path=Path(temporary) / "probe.lbdb"))
        try:
            if engine.execute("RETURN 42 AS answer").rows != [[42]]:
                raise RuntimeError("native query returned an unexpected result")
            if kind == "engine":
                return "installed and usable; temporary database + Cypher query passed"
            extension = kind.upper()
            if prepare:
                engine.load_extension(extension)
            else:
                engine.execute_write(f"LOAD EXTENSION {extension}")
            if kind == "fts":
                engine.execute_write("CREATE NODE TABLE Probe(id STRING PRIMARY KEY, body STRING)")
                engine.execute_write("CREATE (:Probe {id:'probe', body:'readinesscheck'})")
                engine.execute_write("CALL CREATE_FTS_INDEX('Probe', 'probe_fts', ['body'])")
                result = engine.execute("CALL QUERY_FTS_INDEX('Probe', 'probe_fts', 'readinesscheck') RETURN node.id")
                if result.rows != [["probe"]]:
                    raise RuntimeError("FTS search did not return its test document")
                return "cached extension loaded; real FTS index + search passed"
            return "cached extension loaded; not needed for ordinary exact cosine retrieval"
        finally:
            engine.close()


def _grammar_check(suffix: str, prepare: bool) -> str:
    from grag.ingest.code_ts import _SUFFIX_LANGUAGES, _build_parser

    module, factory, _ = _SUFFIX_LANGUAGES[suffix]
    if module == "vue":
        from grag.ingest.code_ts import parse_file

        parse_file(".vue", Path("probe.vue"), '<script lang="ts">function probe() {}</script>',
                   repo="probe", rel_path="probe.vue")
        return "Vue TypeScript script extraction + parse passed (JS/TS grammars checked separately)"
    if module == "pack" and not prepare:
        import tree_sitter_language_pack as pack
        from tree_sitter import Parser

        # The standalone registry only loads libraries. The convenience
        # get_parser() function can auto-download; it is NOT an offline probe.
        cache = pack.cache_dir()
        os.environ["TREE_SITTER_LANGUAGE_PACK_LIBS_DIR"] = cache
        parser = Parser(pack.LanguageRegistry.new().get_language(factory))
        location = f" from {cache}"
    else:
        parser = _build_parser(suffix)
        location = ""
    # Empty input is valid in all supported grammars; this executes the parser.
    tree = parser.parse(b"")
    if tree is None or tree.root_node.has_error:
        raise RuntimeError("grammar could not parse empty source")
    return f"grammar loaded and parse passed{location}"


def _model_check(settings: dict[str, Any], prepare: bool) -> str:
    from grag.config import EmbedderConfig
    from grag.retrieval.vectors import FastembedEmbedder

    config = EmbedderConfig.model_validate(settings)
    model = FastembedEmbedder(config, local_files_only=not prepare)
    vectors = model.embed(["grag installation readiness check"])
    if (len(vectors) != 1 or len(vectors[0]) != config.dim
            or not all(math.isfinite(x) for x in vectors[0]) or not any(vectors[0])):
        raise RuntimeError(f"model output does not match the configured {config.dim} dimensions")
    return f"model loaded and real inference passed ({config.dim} dimensions); cache: {os.environ.get('FASTEMBED_CACHE_PATH', 'FastEmbed default temp cache')}"


def _worker(payload: dict) -> dict:
    kind, prepare = payload["kind"], payload["prepare"]
    try:
        if kind in {"engine", "fts", "vector"}:
            detail = _native_check(kind, prepare)
        elif kind == "model":
            detail = _model_check(payload["embedder"], prepare)
        elif kind.startswith("grammar:"):
            detail = _grammar_check(kind.split(":", 1)[1], prepare)
        else:
            raise ValueError(f"Unknown probe: {kind}")
        return {"status": "ready", "detail": detail}
    except Exception as exc:  # noqa: BLE001 — diagnostic boundary; report all native/provider failures
        from grag.core.errors import GragError

        hint = (
            "Reinstall gragdb in this Python environment; check native library dependencies."
            if kind == "engine" else
            "Run grag doctor --prepare while online. Check cache permissions and installed extras; "
            "preparation does not reinstall packages or replace incompatible/corrupt cached binaries."
        )
        if isinstance(exc, GragError) and exc.hint:
            hint = ""
        return {"status": "unavailable", "detail": f"{type(exc).__name__}: {exc} {hint}".strip()[:4096]}


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(json.dumps(_worker(json.load(sys.stdin))), encoding="utf-8")
