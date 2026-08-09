"""grag — LLM-first graph knowledgebase."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

from grag.config import GragConfig
from grag.core.engine import Engine

try:
    # Single source of truth: pyproject [project] version, via the installed
    # dist metadata (editable installs register it too).
    __version__ = _dist_version("gragdb")
except PackageNotFoundError:  # bare source tree, never installed
    __version__ = "0.0.0.dev0"

__all__ = ["Engine", "GragConfig", "__version__"]
