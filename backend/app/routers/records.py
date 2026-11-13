"""Record routes: start a record from opened Turtle, add steps, and read a record (D-036).

Decomposition records start in the decompose routes and end with the publish route; these
routes cover what only the browser sees in between. Only signed-in accounts are recorded.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from ..core.dependencies import API_PREFIX, record_store, require_current_user
from ..schemas import RecordEventResponse, RecordStartedResponse, RecordSummaryResponse
from ..schemas.requests import RecordEventRequest, StartRecordRequest

router = APIRouter()

RECORD_ID_PATH = Path(..., pattern=r"^[0-9a-f]{16}$", description="The record's id.")


def require_recorded_account(user: Dict[str, Any] = Depends(require_current_user)) -> Dict[str, Any]:
    """FastAPI dependency: the signed-in account, or 409 while authentication is disabled.

    Records belong to accounts, and the auth-disabled development user has none.

    Raises:
        HTTPException: 409 when authentication is disabled.
    """
    if not record_store.applies_to(user):
        raise HTTPException(status_code=409, detail="Records need accounts, and authentication is disabled.")
    return user


@router.post(f"{API_PREFIX}/records", response_model=RecordStartedResponse, status_code=201, tags=["Records"])
def start_record(
    req: StartRecordRequest,
    request: Request,
    user: Dict[str, Any] = Depends(require_recorded_account),
) -> RecordStartedResponse:
    """Start a record from a Turtle the person opened or pasted; it becomes version 0.

    Raises:
        HTTPException: 400 when the text is not valid Turtle.
    """
    try:
        record_id, version = record_store.start_import(
            user=user,
            request=request,
            turtle=req.turtle,
            occurred_at=req.occurred_at,
            client=req.client.model_dump(exclude_none=True) if req.client else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return RecordStartedResponse(record_id=record_id, version=version)


@router.post(
    f"{API_PREFIX}/records/{{record_id}}/events",
    response_model=RecordEventResponse,
    status_code=201,
    tags=["Records"],
)
def add_record_event(
    req: RecordEventRequest,
    request: Request,
    record_id: str = RECORD_ID_PATH,
    user: Dict[str, Any] = Depends(require_recorded_account),
) -> RecordEventResponse:
    """Add one step to the signed-in account's own record.

    Raises:
        HTTPException: 400 when an ``edit`` or ``metadata`` step's Turtle is not valid Turtle.
    """
    try:
        event, version = record_store.append_event(
            user=user,
            request=request,
            record_id=record_id,
            type_=req.type,
            version=req.version,
            occurred_at=req.occurred_at,
            turtle=req.turtle,
            changes=[change.model_dump() for change in req.changes],
            note=req.note,
            unchecked_components=req.unchecked_components,
            client=req.client.model_dump(exclude_none=True) if req.client else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return RecordEventResponse(record_id=record_id, event=event, version=version)


@router.get(f"{API_PREFIX}/records/{{record_id}}", response_model=RecordSummaryResponse, tags=["Records"])
def read_record(
    record_id: str = RECORD_ID_PATH,
    user: Dict[str, Any] = Depends(require_recorded_account),
) -> RecordSummaryResponse:
    """Return a record's versions, best mark and publications; owners and admins only."""
    return RecordSummaryResponse(**record_store.summary(user=user, record_id=record_id))
