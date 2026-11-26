"""Application factory: build the FastAPI app, wire middleware, and mount routers.

This module is intentionally thin. All behavior lives in:

* ``app.core`` — config (``Settings``), shared state/text leaves, auth dependencies
  + middleware, logging.
* ``app.storage`` — the database: connections, migrations, backups, space.
* ``app.access`` — what a request may do: one capability per route.
* ``app.usage`` — the daily limit on decompositions of accounts that are not approved, off by default.
* ``app.guards`` — where a request comes from, rate limits, size bounds.
* ``app.oidc`` — signing in with ROHub.
* ``app.clients`` — OpenRouter / PSNC / HTTP plumbing.
* ``app.services`` — llm, enrichment (entity linking), prompts, validation, rdf_ttl,
  nanopub_service, orcid.
* ``app.pipeline`` — decomposition orchestration + startup warmup.
* ``app.routers`` — the HTTP route handlers grouped by domain.

See iadopt-variable-description-service-docs/CONTRACTS.md for the full map and the Phase-2 layering
(routers → services → clients → core/schemas).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import usage
from .access.admission import release_interrupted, store_ends
from .access.dependencies import AccessRefused, access_refused_response
from .core.dependencies import auth_store, authentication_middleware
from .core.logging import configure_logging
from .guards import BodyBoundMiddleware, RequestRefused, request_refused_response
from .oidc import settings as sign_in_settings
from .pipeline import warmup_assets
from .routers import admin, auth, decompose, nanopub, records, system

logger = logging.getLogger(__name__)

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
    """Judge the settings, migrate the database, initialize the auth store, and warm up cached assets at startup.

    With the daily limit, decompositions that a restart interrupted are given back first.
    """
    judged = sign_in_settings.judge(auth_enabled=auth_store.enabled, cookie_secure=auth_store.cookie_secure)
    logger.info("The sign-in mode is %s (%s).", judged.mode, judged.environment)
    limit = usage.daily_limit()
    if limit is not None:
        logger.info("Accounts that are not approved may decompose %s times a day.", limit)
    auth_store.init()
    release_interrupted()
    warmup_assets()
    yield
    store_ends(auth_store.db_path)


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

    # Authentication middleware (defined in core.dependencies, registered here). The size bound is
    # registered after it, so it comes first.
    application.middleware("http")(authentication_middleware)
    application.add_middleware(BodyBoundMiddleware)
    application.add_exception_handler(AccessRefused, access_refused_response)
    application.add_exception_handler(RequestRefused, request_refused_response)

    # Mount routers. Order mirrors the original single-file registration so the
    # generated OpenAPI path order is unchanged.
    application.include_router(auth.router)
    application.include_router(system.router)
    application.include_router(admin.router)
    application.include_router(decompose.router)
    application.include_router(nanopub.router)
    application.include_router(records.router)

    return application


app = create_app()
