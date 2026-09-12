"""Strict, bounded wire contract for the AgentCore planner bridge."""

import hashlib
import json
from copy import deepcopy
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .catalog import PACKETS
from .planning import Plan, project_case, validate_plan

SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
Identifier = Annotated[str, Field(min_length=1, max_length=128)]
ShortText = Annotated[str, Field(min_length=1, max_length=200)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Kind = Literal["MONITORING_REVIEW", "EVIDENCE_GAP_REVIEW"]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class CaseRecord(WireModel):
    id: Literal["GALLINAS-HPCC-2022"]
    title: ShortText
    subtitle: ShortText
    status: Literal["OPEN"]
    coverage: Literal["UNKNOWN", "LIMITED", "DEGRADED"]
    water_safety: Literal["NOT_ASSESSED"]


class TaskRecord(WireModel):
    id: Identifier
    kind: Kind
    title: ShortText
    status: Literal["OPEN", "ACKNOWLEDGED", "COMPLETED"]
    reason: str = Field(min_length=8, max_length=700)
    evidence: list[Identifier] = Field(min_length=1, max_length=3)
    created_at: Identifier


class ResponseRecord(WireModel):
    task_id: Identifier
    action: Literal["acknowledge", "complete_review"]
    recorded_at: Identifier
    simulated: Literal[True]

    @field_validator("simulated", mode="before")
    @classmethod
    def require_boolean(cls, value):
        if value is not True:
            raise ValueError("Operator actions must be explicitly labelled as simulated.")
        return value


class ProjectedState(WireModel):
    case: CaseRecord
    tasks: list[TaskRecord] = Field(max_length=6)
    responses: list[ResponseRecord] = Field(max_length=12)


class Proposal(WireModel):
    kind: Kind
    event_id: Identifier
    reason: str = Field(min_length=8, max_length=700)
    existing_task_id: Identifier | None


class TraceEntry(WireModel):
    tool: Literal["get_case_context", "get_observations", "propose_review", "strands_turn"]
    input: dict[str, Any]
    output: dict[str, Any]


class ModelInput(WireModel):
    instruction_version: Identifier
    model_id: Identifier
    sdk_version: Identifier
    scripted_test: bool


class Usage(WireModel):
    inputTokens: int = Field(ge=0, le=160000)
    outputTokens: int = Field(ge=0, le=8000)
    totalTokens: int = Field(ge=0, le=168000)


class ModelOutput(WireModel):
    model_calls: int = Field(ge=1, le=8)
    elapsed_seconds: float = Field(ge=0, le=150)
    usage: Usage
    stop_reason: Identifier


class AgentCoreMetadata(WireModel):
    runtime_arn: str = Field(min_length=30, max_length=256)
    qualifier: str = Field(min_length=1, max_length=48)
    runtime_session_id: str = Field(pattern=r"^wm-[0-9a-f]{32}$")
    request_id: Identifier
    state_hash: Digest
    released_hash: Digest
    invocation_attempted: bool
    stop_status: Literal["NOT_STARTED", "STOP_REQUEST_ACCEPTED", "STOP_FAILED"]
    endpoint_version_verified_before_call: str | None = Field(default=None, pattern=r"^[1-9][0-9]*$")
    endpoint_arn: str | None = Field(default=None, max_length=320)
    aws_request_id: str | None = Field(default=None, max_length=128)
    trace_id: str | None = Field(default=None, max_length=256)
    error_code: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]{1,80}$")
    stop_error_code: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]{1,80}$")


class ObservedModelOutput(ModelOutput):
    agentcore: AgentCoreMetadata


class WirePlan(WireModel):
    proposals: list[Proposal] = Field(max_length=2)
    trace: list[TraceEntry] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def require_model_turn(self):
        markers = [entry for entry in self.trace if entry.tool == "strands_turn"]
        if len(markers) != 1 or self.trace[-1].tool != "strands_turn":
            raise ValueError("Exactly one final SDK execution marker is required.")
        ModelInput.model_validate(markers[0].input)
        ModelOutput.model_validate(markers[0].output)
        return self


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def validate_json_bounds(value: Any, depth: int = 0) -> None:
    if depth > 14:
        raise ValueError("Wire JSON nesting exceeds the bound.")
    if isinstance(value, str) and len(value) > 2000:
        raise ValueError("Wire string exceeds the bound.")
    if isinstance(value, dict):
        if len(value) > 64 or any(not isinstance(k, str) or len(k) > 128 for k in value):
            raise ValueError("Wire object exceeds the bound.")
        for item in value.values():
            validate_json_bounds(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 64:
            raise ValueError("Wire array exceeds the bound.")
        for item in value:
            validate_json_bounds(item, depth + 1)


class Envelope(WireModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    request_id: Identifier
    revision: int = Field(ge=0, le=1000)
    event_id: Identifier
    state_hash: Digest
    released_hash: Digest

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_version_integer(cls, value):
        if type(value) is not int:
            raise ValueError("Schema version must be an integer.")
        return value


class AgentCoreRequest(Envelope):
    state: dict[str, Any]
    released: list[dict[str, Any]] = Field(min_length=1, max_length=3)

    @field_validator("state")
    @classmethod
    def strict_state(cls, value):
        return ProjectedState.model_validate(value).model_dump()

    @model_validator(mode="after")
    def check_hashes_and_packets(self):
        if canonical_hash(self.state) != self.state_hash:
            raise ValueError("State hash mismatch.")
        if canonical_hash(self.released) != self.released_hash:
            raise ValueError("Released evidence hash mismatch.")
        if canonical_hash(self.released) != canonical_hash(PACKETS[:len(self.released)]):
            raise ValueError("Released packets must be an exact catalog prefix.")
        if self.released[-1]["event_id"] != self.event_id:
            raise ValueError("Current event must be the last released packet.")
        previous = {packet["event_id"] for packet in self.released[:-1]}
        tasks = self.state["tasks"]
        ids = {task["id"] for task in tasks}
        if len(ids) != len(tasks) or any(not set(t["evidence"]) <= previous for t in tasks):
            raise ValueError("Saved task identity or historical evidence is inconsistent.")
        if any(r["task_id"] not in ids for r in self.state["responses"]):
            raise ValueError("A response references an unknown saved task.")
        validate_wire_size(self, MAX_REQUEST_BYTES)
        return self


class AgentCoreResponse(Envelope):
    plan: dict[str, Any]

    @field_validator("plan")
    @classmethod
    def strict_plan(cls, value):
        return WirePlan.model_validate(value).model_dump()


def make_request(state: dict, released: list[dict], request_id: str,
                 revision: int) -> AgentCoreRequest:
    if not released:
        raise ValueError("A released observation is required.")
    projected = project_case(state)
    packets = deepcopy(released)
    return AgentCoreRequest(request_id=request_id, revision=revision,
                            event_id=packets[-1]["event_id"], state=projected, released=packets,
                            state_hash=canonical_hash(projected), released_hash=canonical_hash(packets))


def validate_wire_size(value: BaseModel, maximum: int) -> bytes:
    data = value.model_dump(mode="json")
    validate_json_bounds(data)
    encoded = canonical_json(data)
    if len(encoded) > maximum:
        raise ValueError("Wire payload exceeds bounded size.")
    return encoded


def validate_response(response: AgentCoreResponse, request: AgentCoreRequest,
                      state: dict, released: list[dict]) -> Plan:
    for field in Envelope.model_fields:
        if getattr(response, field) != getattr(request, field):
            raise ValueError("Response content or request identity mismatch.")
    validate_wire_size(response, MAX_RESPONSE_BYTES)
    plan = Plan(**deepcopy(response.plan))
    validate_plan(plan, state, released)
    return plan
