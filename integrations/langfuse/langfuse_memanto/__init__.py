"""
langfuse-memanto — turn Langfuse observability signal into Memanto memories,
live from your application.

Langfuse's Python SDK is built on OpenTelemetry, so this package attaches a
second span processor to the tracer provider Langfuse already set up. Failing
spans are grouped into one memory per error *signature* and written to Memanto
on a background thread::

    from langfuse import Langfuse
    from langfuse_memanto import attach

    Langfuse()
    attach(agent_id="my-agent")

Capture settings are shared with ``memanto migrate langfuse`` — configure them
once with ``memanto migrate langfuse --discover`` and ``--save``.
"""

from langfuse_memanto.config import HandlerSettings
from langfuse_memanto.handler import MemantoLangfuseHandler, attach
from langfuse_memanto.span_mapper import span_to_observation

__version__ = "0.1.0"

__all__ = [
    "HandlerSettings",
    "MemantoLangfuseHandler",
    "attach",
    "span_to_observation",
    "__version__",
]
