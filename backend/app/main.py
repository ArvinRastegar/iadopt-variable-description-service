"""Application factory: build the FastAPI app, wire middleware, and mount routers.

This module is intentionally thin. All behavior lives in:

* ``app.core`` — config (``Settings``), shared state/text leaves, auth dependencies
  + middleware, logging.
* ``app.clients`` — OpenRouter / PSNC / HTTP plumbing.
* ``app.services`` — llm, reranker, enrichment, prompts, validation, rdf_ttl,
  nanopub_service, orcid.
* ``app.pipeline`` — decomposition orchestration + startup warmup.
* ``app.routers`` — the HTTP route handlers grouped by domain.

See docs/CONTRACTS.md for the full map and the Phase-2 layering
(routers → services → clients → core/schemas).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .core.dependencies import auth_store, authentication_middleware
from .core.logging import configure_logging
from .pipeline import warmup_assets
from .routers import admin, auth, decompose, nanopub, system

API_DESCRIPTION = """
I-ADOPT variable decomposition, Turtle generation, visualization support, and nanopublication publishing.

There are two decomposition endpoints on purpose:

- `POST /api/decompose/stream` is the endpoint used by the frontend. It streams raw LLM output while the model is
  responding, then emits the final parsed JSON, validation result, enriched JSON, and Turtle payload.
- `POST /api/decompose` runs the same backend pipeline but returns only one final JSON response. It is kept for API
  clients, scripts, tests, and debugging tools that do not want to consume an NDJSON stream.
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize the auth store and warm up cached assets at startup."""
    auth_store.init()
    warmup_assets()
    yield


def create_app() -> FastAPI:
    """Build and configure the FastAPI application.

    Returns:
        The wired ``FastAPI`` app with auth middleware and all routers mounted.
    """
    configure_logging()

    application = FastAPI(
        title="I-ADOPT Variable Decomposition API",
        version="0.1.0",
        description=API_DESCRIPTION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Authentication middleware (defined in core.dependencies, registered here).
    application.middleware("http")(authentication_middleware)

    # Mount routers. Order mirrors the original single-file registration so the
    # generated OpenAPI path order is unchanged.
    application.include_router(auth.router)
    application.include_router(system.router)
    application.include_router(admin.router)
    application.include_router(decompose.router)
    application.include_router(nanopub.router)

    return application


app = create_app()
