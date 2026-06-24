"""Domain service modules — one clear responsibility each.

Layering: ``routers → services → clients → core/schemas``. Services hold the
business logic (LLM calls, enrichment, RDF/TTL generation, validation, nanopub
publishing, ORCID resolution); they never import routers or the app factory.
"""
