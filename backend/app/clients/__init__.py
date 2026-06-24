"""HTTP client plumbing for external services (OpenRouter, PSNC, shared session).

Clients sit below services in the layering: ``routers → services → clients →
core/schemas``. They own connection construction and per-provider request shapes,
not business logic.
"""
