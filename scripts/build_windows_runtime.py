"""Build grag's Windows OpenSSL DLLs from a pinned, verified official source.

Run in an x64 MSVC developer environment with Strawberry Perl. There are no
runtime downloads: the resulting files are included by the Windows wheel hook.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

VERSION = "3.5.8"
SHA256 = "a8f84a39918ec6415ce765d9b429d313ba97b8143169c172e734b9514464f5b2"
URL = f"https://github.com/openssl/openssl/releases/download/openssl-{VERSION}/openssl-{VERSION}.tar.gz"
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "src/grag/_runtime"
NAMES = ("libcrypto-3-x64.dll", "libssl-3-x64.dll")


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Run this builder on Windows x64 with MSVC and Strawberry Perl.")
    commands = {name: shutil.which(name) for name in ("perl", "nmake", "cl")}
    if not all(commands.values()):
        raise SystemExit("Use an x64 Visual Studio Developer Command Prompt with Strawberry Perl on PATH.")
    with tempfile.TemporaryDirectory(prefix="grag-openssl-") as temporary:
        work = Path(temporary)
        archive = work / "openssl.tar.gz"
        print(f"Downloading official OpenSSL {VERSION}; verifying SHA256 {SHA256}", flush=True)
        with urllib.request.urlopen(URL, timeout=120) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
            raise SystemExit("OpenSSL source checksum mismatch; refusing to build.")
        with tarfile.open(archive) as source:
            # filter=data is available in every supported Python's current patch.
            source.extractall(work, filter="data")
        source_dir = work / f"openssl-{VERSION}"
        options = ["VC-WIN64A", "shared", "no-tests", "no-apps", "no-module", "no-asm", "no-makedepend"]
        subprocess.run([commands["perl"], "Configure", *options], cwd=source_dir, check=True)  # noqa: S603 — trusted build tools and verified source
        subprocess.run([commands["nmake"]], cwd=source_dir, check=True)  # noqa: S603 — trusted build tools and verified source
        OUTPUT.mkdir(parents=True, exist_ok=True)
        for name in NAMES:
            shutil.copy2(source_dir / name, OUTPUT / name)
        shutil.copy2(source_dir / "LICENSE.txt", OUTPUT / "LICENSE.txt")
        manifest = {
            "name": "OpenSSL", "version": VERSION, "source": URL,
            "source_sha256": SHA256, "configure": options,
            "files": {name: hashlib.sha256((OUTPUT / name).read_bytes()).hexdigest() for name in NAMES},
        }
        (OUTPUT / "provenance.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"Runtime staged in {OUTPUT}")


if __name__ == "__main__":
    main()
