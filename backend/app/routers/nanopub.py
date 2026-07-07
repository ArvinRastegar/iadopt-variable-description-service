"""Nanopub routes: preparation options, publish, and retract.

Publish and retract write IRREVERSIBLY to the public nanopub registry; the heavy
lifting (signing, metadata, key-ownership guard) lives in ``services.nanopub_service``.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from nanopub import Nanopub, NanopubConf
from rdflib import Graph, Literal
from rdflib.namespace import RDFS

from ..core.config import settings
from ..core.dependencies import API_PREFIX, auth_store, require_current_user
from ..schemas import (
    NanopubPreparationOptionsResponse,
    PublishNanopubResponse,
    RetractNanopubResponse,
)
from ..schemas.requests import PublishNanopubRequest, RetractNanopubRequest
from ..services.nanopub_service import (
    add_nanopub_metadata,
    assert_retraction_allowed,
    build_retraction_nanopub,
    extract_assertion_label,
    extract_variable_identifier,
    extract_variable_uri,
    get_nanopub_agent_uri,
    get_nanopub_profile,
    nanopub_created_literal,
    normalize_target_nanopub_uri,
)
from ..services.orcid import normalize_orcid

router = APIRouter()


@router.get(
    f"{API_PREFIX}/nanopub/preparation-options",
    response_model=NanopubPreparationOptionsResponse,
    tags=["Nanopub"],
)
def nanopub_preparation_options() -> NanopubPreparationOptionsResponse:
    """Expose the metadata constants the frontend needs to enrich pasted Turtle for nanopublication.

    These values are the single source of truth shared with the backend's own TTL generator, so
    pasted-Turtle preparation and generated Turtle stay byte-for-byte aligned.
    """
    return NanopubPreparationOptionsResponse(
        default_creator_orcid_id=normalize_orcid(settings.nanopub_orcid_id),
        conforms_to_uri=settings.iadopt_variable_conforms_to,
        created_with_label=settings.iadopt_created_with_label,
    )


@router.post(f"{API_PREFIX}/nanopub/publish", response_model=PublishNanopubResponse, tags=["Nanopub"])
def publish_nanopub(
    req: PublishNanopubRequest,
    request: Request,
    user: Dict[str, Any] = Depends(require_current_user),
) -> PublishNanopubResponse:
    """Publish the exact TTL currently shown in the frontend as a signed nanopublication.

    Raises:
        HTTPException: 400 on an empty/unparseable TTL payload; 500 on a publish
            failure (e.g. missing signing configuration or a registry error).
    """
    start = time.perf_counter()
    ttl = req.ttl.strip()
    if not ttl:
        auth_store.audit_event(
            action="nanopub.publish",
            user=user,
            request=request,
            status_code=400,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            error="TTL payload is empty.",
        )
        raise HTTPException(status_code=400, detail="TTL payload is empty.")

    assertion_graph = Graph()
    try:
        assertion_graph.parse(data=ttl, format="turtle")
    except Exception as e:
        auth_store.audit_event(
            action="nanopub.publish",
            user=user,
            request=request,
            status_code=400,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            error=f"Could not parse Turtle payload: {e}",
        )
        raise HTTPException(status_code=400, detail=f"Could not parse Turtle payload: {e}") from e

    try:
        profile = get_nanopub_profile()
        variable_uri = extract_variable_uri(assertion_graph)
        variable_identifier = extract_variable_identifier(assertion_graph, variable_uri)
        assertion_label = extract_assertion_label(assertion_graph, variable_uri)
        created_at = nanopub_created_literal()
        agent_uri = get_nanopub_agent_uri()

        nanopub_conf = NanopubConf(
            profile=profile,
            use_server=settings.nanopub_publish_server,
            add_prov_generated_time=False,
            add_pubinfo_generated_time=False,
            attribute_assertion_to_profile=False,
            attribute_publication_to_profile=False,
        )
        nanopub = Nanopub(assertion=assertion_graph, conf=nanopub_conf)
        add_nanopub_metadata(
            nanopub,
            variable_uri=variable_uri,
            created_at=created_at,
            agent_uri=agent_uri,
            creator_orcid_id=req.creator_orcid_id,
        )

        if assertion_label:
            # Carry the assertion label into the nanopub pubinfo so the resulting publication is easier to inspect.
            nanopub.pubinfo.add((nanopub.metadata.namespace[""], RDFS.label, Literal(assertion_label)))

        publish_result = nanopub.publish()
        nanopub_url = str(publish_result[0])
        published_to = str(publish_result[1])

        response_payload = PublishNanopubResponse(
            nanopub_url=nanopub_url,
            published_to=published_to,
            variable_identifier=variable_identifier,
            variable_uri=str(variable_uri),
        )
        auth_store.audit_event(
            action="nanopub.publish",
            user=user,
            request=request,
            status_code=200,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            response_payload=response_payload.model_dump(),
            metadata={"variable_identifier": variable_identifier, "variable_uri": str(variable_uri)},
        )
        return response_payload
    except HTTPException:
        raise
    except RuntimeError as e:
        auth_store.audit_event(
            action="nanopub.publish",
            user=user,
            request=request,
            status_code=500,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            error=str(e),
        )
        raise HTTPException(status_code=500, detail=str(e)) from e
    except Exception as e:
        auth_store.audit_event(
            action="nanopub.publish",
            user=user,
            request=request,
            status_code=500,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            error=f"Nanopub publish failed: {e}",
        )
        raise HTTPException(status_code=500, detail=f"Nanopub publish failed: {e}") from e


@router.post(f"{API_PREFIX}/nanopub/retract", response_model=RetractNanopubResponse, tags=["Nanopub"])
def retract_nanopub(
    req: RetractNanopubRequest,
    request: Request,
    user: Dict[str, Any] = Depends(require_current_user),
) -> RetractNanopubResponse:
    """Publish a signed nanopub retraction for a previously published nanopublication.

    Raises:
        HTTPException: 400 on a bad/unresolvable URI or a key-ownership failure;
            500 on a retraction publish failure.
    """
    start = time.perf_counter()
    try:
        target_nanopub_uri = normalize_target_nanopub_uri(req.nanopub_uri)
        profile = get_nanopub_profile()
        assert_retraction_allowed(target_nanopub_uri, profile)
        retraction = build_retraction_nanopub(
            target_nanopub_uri,
            profile,
            creator_orcid_id=req.creator_orcid_id,
        )

        # Publishing the custom retraction nanopub creates a new nanopub whose assertion retracts the target URI.
        publish_result = retraction.publish()
        response_payload = RetractNanopubResponse(
            retraction_url=str(publish_result[0]),
            published_to=str(publish_result[1]),
            retracted_nanopub_url=target_nanopub_uri,
        )
        auth_store.audit_event(
            action="nanopub.retract",
            user=user,
            request=request,
            status_code=200,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            response_payload=response_payload.model_dump(),
            metadata={"target_nanopub_uri": target_nanopub_uri},
        )
        return response_payload
    except RuntimeError as e:
        auth_store.audit_event(
            action="nanopub.retract",
            user=user,
            request=request,
            status_code=400,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            error=str(e),
        )
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        auth_store.audit_event(
            action="nanopub.retract",
            user=user,
            request=request,
            status_code=500,
            latency_ms=round((time.perf_counter() - start) * 1000),
            request_payload=req.model_dump(),
            error=f"Nanopub retract failed: {e}",
        )
        raise HTTPException(status_code=500, detail=f"Nanopub retract failed: {e}") from e
