"""grag — LLM-first graph knowledgebase."""

__version__ = "0.1.0"

from grag.config import GragConfig
from grag.core.engine import Engine

__all__ = ["GragConfig", "Engine", "__version__"]
