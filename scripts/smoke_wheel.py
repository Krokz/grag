"""Install the built wheel outside the checkout and exercise public entry points.

No developer extras, manually copied DLLs, or prewarmed extension/model caches.
PATH excludes other apps during execution (particularly Git/Strawberry/OpenSSL).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SMOKE = r'''
import asyncio, hashlib, json, os, sys
from pathlib import Path
from grag import Engine, GragConfig
from importlib.resources import files
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

assert not str(files('grag')).startswith(str(Path(sys.argv[1]) / 'src'))
assert files('grag').joinpath('api/static/index.html').is_file()
db = Path.cwd() / 'smoke.lbdb'
engine = Engine(GragConfig(db_path=db))
engine.execute_write('CREATE NODE TABLE Memory(id STRING PRIMARY KEY, body STRING)')
engine.execute_write('CREATE (:Memory {id:$id, body:$body})', {'id':'one', 'body':'hello graph 🌍'})
engine.close()
engine = Engine(GragConfig(db_path=db))
assert engine.execute('MATCH (n:Memory) RETURN n.body').rows == [['hello graph 🌍']]
engine.close()

async def mcp_smoke():
    params = StdioServerParameters(command=sys.executable, args=['-m','grag.cli','--db',str(db),'mcp'], env=dict(os.environ))
    async with stdio_client(params) as (r,w), ClientSession(r,w) as session:
        await session.initialize()
        tools = await session.list_tools()
        assert len(tools.tools) == 10
        result = await session.call_tool('cypher_query', {'cypher':'MATCH (n:Memory) RETURN n.body'})
        assert not result.is_error, result
        assert 'hello graph' in str(result)
asyncio.run(mcp_smoke())
print('wheel: native write/reopen/query, UI asset, real stdio MCP handshake/read passed')
'''


def main() -> None:
    arguments = [arg for arg in sys.argv[1:] if arg != "--code"]
    wheels = [Path(arguments[0]).resolve()] if arguments else list((ROOT / "dist").glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Expected one built wheel in dist/")
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if sys.platform == "win32":
            assert wheel.name.endswith("py3-none-win_amd64.whl")
            for name in ("libcrypto-3-x64.dll", "libssl-3-x64.dll", "LICENSE.txt", "provenance.json"):
                assert f"grag/_runtime/{name}" in names
        else:
            assert wheel.name.endswith("py3-none-any.whl")
            assert not any("grag/_runtime/" in name for name in names)
    with tempfile.TemporaryDirectory(prefix="grag-wheel-") as temporary:
        root = Path(temporary)
        envdir = root / "venv"
        venv.EnvBuilder(with_pip=True).create(envdir)
        bindir = envdir / ("Scripts" if os.name == "nt" else "bin")
        python = bindir / ("python.exe" if os.name == "nt" else "python")
        subprocess.run([str(python), "-m", "pip", "install", str(wheel)], check=True, cwd=root)
        home = root / "home"
        home.mkdir()
        # Keep OS bootstrapping variables; exclude project/provider/cache settings.
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GRAG_", "PYTHON", "HF_", "FASTEMBED", "TREE_SITTER"))}
        system = str(Path(env.get("SystemRoot", "C:/Windows")) / "System32") if os.name == "nt" else "/usr/bin:/bin"
        env.update(HOME=str(home), USERPROFILE=str(home), XDG_CACHE_HOME=str(home / "cache"),
                   LOCALAPPDATA=str(home / "local"), APPDATA=str(home / "roaming"),
                   PATH=str(bindir) + os.pathsep + system, PYTHONIOENCODING="cp1252",
                   GRAG_EMBED_PROVIDER="", HF_HUB_OFFLINE="1", NO_PROXY="127.0.0.1,localhost")
        script = root / "smoke.py"
        script.write_text(SMOKE, encoding="utf-8")
        subprocess.run([str(python), str(script), str(ROOT)], check=True, cwd=root, env=env, timeout=90)
        command = [str(python), "-m", "grag.cli"]
        subprocess.run([*command, "init", "--client", "claude", "--dry-run"], check=True, cwd=root, env=env, timeout=30)
        result = subprocess.run([*command, "doctor", "--json"], cwd=root, env=env, capture_output=True, timeout=60)
        report = json.loads(result.stdout)
        checks = {c["key"]: c for c in report["checks"]}
        assert checks["engine"]["status"] == "ready", report
        assert checks["fts"]["status"] == "unavailable", report  # No hidden first-use downloads.
        assert result.returncode == 1, report
        assert checks["model"]["status"] == "disabled", report
        assert not list(home.rglob("*.lbug_extension")), "doctor downloaded an extension"
        print("wheel: legacy encoding and truthful cold-cache doctor passed")
        result = subprocess.run([*command, "doctor", "--prepare", "--json"], cwd=root, env=env, capture_output=True, timeout=660)
        report = json.loads(result.stdout)
        assert result.returncode == 0 and report["ready"], (report, result.stderr)
        result = subprocess.run([*command, "doctor", "--json"], cwd=root, env=env, capture_output=True, timeout=90)
        assert result.returncode == 0 and json.loads(result.stdout)["ready"], result.stderr
        print("wheel: explicit first-use extension preparation and offline FTS search passed")
        if "--code" in sys.argv[1:]:
            subprocess.run([str(python), "-m", "pip", "install", f"{wheel}[code]"], check=True, cwd=root, env=env)
            for attempt in range(3):
                result = subprocess.run([*command, "doctor", "--prepare", "--json"], cwd=root, env=env, capture_output=True, timeout=900)
                report = json.loads(result.stdout)
                failed = [c for c in report["checks"] if c["status"] == "unavailable"]
                transient = any(c["key"] == "grammar_assets" and re.search(r"http status: (429|500|502|503|504)\b", c["detail"]) for c in failed)
                if report["ready"] or not transient or any(not c["key"].startswith("grammar") for c in failed):
                    break
                if attempt < 2:
                    print(f"Grammar host returned a transient HTTP error; preparation retry {attempt + 1}/2", flush=True)
                    time.sleep(5 * (attempt + 1))
            assert result.returncode == 0 and report["ready"], (report, result.stderr)
            result = subprocess.run([*command, "doctor", "--json"], cwd=root, env=env, capture_output=True, timeout=150)
            report = json.loads(result.stdout)
            assert result.returncode == 0 and report["ready"], (report, result.stderr)
            assert sum(c["key"].startswith("grammar:") for c in report["checks"]) >= 19
            print("wheel: optional code extra, grammar preparation and offline parsing passed")


if __name__ == "__main__":
    main()
