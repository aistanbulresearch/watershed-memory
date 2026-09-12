"""Bedrock AgentCore entrypoint: one bounded Strands turn, no case-ledger writes."""

import json
import logging
import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from watershed_memory.agentcore_protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    AgentCoreRequest,
    AgentCoreResponse,
    canonical_json,
    validate_wire_size,
)
from watershed_memory.planning import Planner, validate_plan
from watershed_memory.strands_agent import AgentTurnError, bedrock_planner

app = BedrockAgentCoreApp()
logger = logging.getLogger("watershed_memory.runtime")


def handle(payload: bytes | str | dict, planner: Planner | None = None) -> dict:
    raw = canonical_json(payload) if isinstance(payload, dict) else payload
    raw = raw.encode() if isinstance(raw, str) else raw
    if not isinstance(raw, bytes) or len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("Request exceeds bounded size or has an invalid type.")
    request = AgentCoreRequest.model_validate(json.loads(raw))
    selected = planner or bedrock_planner(
        os.environ["WATERSHED_MODEL_ID"], os.environ["WATERSHED_AWS_REGION"])
    audit = {"request_id": request.request_id, "event_id": request.event_id,
             "state_hash": request.state_hash, "released_hash": request.released_hash,
             "revision": request.revision}
    try:
        plan = selected.plan(request.state, request.released)
        validate_plan(plan, request.state, request.released)
    except AgentTurnError as error:
        logger.error(json.dumps({"event": "watershed_turn_failed", **audit,
                                 "usage": error.evidence["usage"],
                                 "model_calls_attempted": error.evidence["model_calls_attempted"]}))
        raise
    response = AgentCoreResponse(**request.model_dump(exclude={"state", "released"}),
                                 plan={"proposals": plan.proposals, "trace": plan.trace})
    validate_wire_size(response, MAX_RESPONSE_BYTES)
    logger.info(json.dumps({"event": "watershed_plan_staged", **audit,
                            "proposal_count": len(plan.proposals), "committed": False}))
    return response.model_dump(mode="json")


@app.entrypoint
def entrypoint(payload: dict) -> dict:
    return handle(payload)


if __name__ == "__main__":
    app.run()

