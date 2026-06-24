"""FastAPI routers grouped by domain.

Each module exposes an ``APIRouter`` that the app factory (``app.main``) includes.
Routers depend on services/pipeline and on ``core.dependencies`` for auth; they
never import the app factory, keeping the dependency direction one-way.
"""
