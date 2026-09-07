"""Include the owned OpenSSL runtime only in Windows distribution wheels."""

import platform
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if self.target_name != "wheel":
            return
        runtime = Path(self.root) / "src/grag/_runtime"
        if sys.platform != "win32":
            # Never let leftovers from a Windows build enter a universal wheel.
            build_data["force_include"] = {
                k: v for k, v in build_data["force_include"].items()
                if "_runtime" not in v
            }
            return
        if platform.machine().lower() not in {"amd64", "x86_64"}:
            raise RuntimeError("grag's bundled Windows runtime supports x64 only")
        for name in ("libcrypto-3-x64.dll", "libssl-3-x64.dll", "LICENSE.txt", "provenance.json"):
            asset = runtime / name
            if not asset.is_file():
                raise RuntimeError(
                    "Build the Windows runtime first: python scripts/build_windows_runtime.py "
                    "(x64 Visual Studio Developer Command Prompt and Strawberry Perl required)"
                )
            build_data["force_include"][str(asset)] = f"grag/_runtime/{name}"
        build_data["pure_python"] = False
        build_data["tag"] = "py3-none-win_amd64"
