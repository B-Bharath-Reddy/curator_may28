import os
from typing import Optional

from phoenix.otel import register
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry import trace
from openinference.instrumentation.langchain import LangChainInstrumentor

_TRACING_INITIALIZED = False
_TRACER = None


def setup_phoenix(app) -> Optional[object]:
    global _TRACING_INITIALIZED, _TRACER

    if _TRACING_INITIALIZED:
        return _TRACER

    tracer_provider = register(
        project_name=os.getenv("PHOENIX_PROJECT_NAME", "ace-impression-agent"),
        endpoint=os.getenv("PHOENIX_COLLECTOR_ENDPOINT"),
        api_key=os.getenv("PHOENIX_API_KEY"),
        protocol=os.getenv("PHOENIX_PROTOCOL", "http/protobuf"),
        auto_instrument=False,
        batch=True,
    )

    LangChainInstrumentor().instrument()

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        excluded_urls="health",
    )

    _TRACER = trace.get_tracer("ace-impression-agent")
    _TRACING_INITIALIZED = True
    return _TRACER


def get_tracer():
    global _TRACER
    if _TRACER is None:
        _TRACER = trace.get_tracer("ace-impression-agent")
    return _TRACER