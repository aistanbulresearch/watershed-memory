"""Stateless current-case reasoning through the actual AgentCore HTTP server."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response

from .agentcore_wrapper import decode_sdk_payload
from .delivery_types import InvocationProfile
from .field_delivery_types import FieldDeliveryReservation
from .field_planner import FieldPlannerErrorV3, FieldPlannerV3
from .remote_protocol import decode_request, encode_response, failure_response, success_response
from .runtime_body import CurrentInvocationBodyMiddleware

_SCOPE_KEY = "watershed.current_request_bytes"


def _configured_profile(value: InvocationProfile) -> InvocationProfile:
    if type(value) is not InvocationProfile:
        raise ValueError("invalid current runtime configuration")
    checked = replace(value)
    if checked.instruction_version != "watershed-current-v3":
        raise ValueError("invalid current runtime configuration")
    return checked


def _error(status: int) -> Response:
    body = (b'{"error":"CURRENT_REQUEST_REJECTED"}' if status == 400
            else b'{"error":"CURRENT_RUNTIME_FAILED"}')
    return Response(body, status_code=status, media_type="application/json")


def handle_current(
    payload: object,
    context: RequestContext,
    *,
    expected_profile: InvocationProfile,
    planner_factory: Callable[[], FieldPlannerV3],
) -> Response:
    """Validate an immutable request and return an uncommitted canonical result."""
    try:
        expected = _configured_profile(expected_profile)
        if not callable(planner_factory):
            return _error(500)
        if type(context) is not RequestContext or not isinstance(context.request, Request):
            return _error(500)
        scope = context.request.scope
        if type(scope) is not dict or type(scope.get(_SCOPE_KEY)) is not bytes:
            return _error(500)
        raw = scope[_SCOPE_KEY]
    except Exception:
        return _error(500)

    try:
        if decode_sdk_payload(payload) != raw:
            return _error(400)
        request = decode_request(raw)
        if request.identity.profile != expected:
            return _error(400)
    except Exception:
        return _error(400)

    try:
        planner = planner_factory()
        if not isinstance(planner, FieldPlannerV3) or _configured_profile(planner.profile) != expected:
            return _error(500)
        identity = request.identity
        reservation = FieldDeliveryReservation(
            identity.attempt_id, identity.request_id, identity.profile,
            identity.source_delivery, request.context, identity.reserved_at,
        )
        try:
            execution = planner.plan_reserved(reservation)
        except FieldPlannerErrorV3 as failure:
            response = failure_response(request, failure.failure)
        else:
            response = success_response(request, execution)
        return Response(encode_response(response, request), media_type="application/json")
    except Exception:
        return _error(500)


def create_current_app(
    *,
    expected_profile: InvocationProfile,
    planner_factory: Callable[[], FieldPlannerV3],
) -> BedrockAgentCoreApp:
    """Bind server-owned configuration; callers cannot choose a planner or profile."""
    profile = _configured_profile(expected_profile)
    if not callable(planner_factory):
        raise ValueError("invalid current runtime configuration")
    app = BedrockAgentCoreApp(
        debug=False, middleware=[Middleware(CurrentInvocationBodyMiddleware)],
    )

    @app.entrypoint
    def entrypoint(payload: object, context: RequestContext) -> Response:
        return handle_current(
            payload, context, expected_profile=profile, planner_factory=planner_factory,
        )

    return app
