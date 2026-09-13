"""Immutable, redacted evidence for one current AgentCore attempt."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from .remote_types import CurrentRemoteIdentity

_ARN = re.compile(
    r"arn:aws:bedrock-agentcore:([a-z0-9-]+):([0-9]{12}):runtime/"
    r"([A-Za-z][A-Za-z0-9_]{0,47}-[A-Za-z0-9]+)\Z"
)
_QUALIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,47}\Z")
_VERSION = re.compile(r"[1-9][0-9]{0,18}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CODE = re.compile(r"[A-Za-z0-9_]{1,80}\Z")
_SESSION = re.compile(r"wm-[0-9a-f]{32}\Z")


def _text(value: object, maximum: int) -> None:
    if type(value) is not str or not 1 <= len(value) <= maximum or not value.isprintable() or value != value.strip():
        raise ValueError("invalid current AgentCore evidence")


def _optional_text(value: object, maximum: int) -> None:
    if value is not None:
        _text(value, maximum)


def _hash(value: object, optional: bool = False) -> None:
    if optional and value is None:
        return
    if type(value) is not str or not _HASH.fullmatch(value):
        raise ValueError("invalid current AgentCore evidence")


@dataclass(frozen=True, slots=True)
class AgentCoreTarget:
    runtime_arn: str
    qualifier: str
    region: str
    expected_account: str
    expected_version: str

    def __post_init__(self) -> None:
        if (
            type(self.runtime_arn) is not str
            or type(self.region) is not str
            or type(self.expected_account) is not str
            or type(self.qualifier) is not str
            or type(self.expected_version) is not str
            or len(self.runtime_arn) > 256
        ):
            raise ValueError("invalid current AgentCore evidence")
        match = _ARN.fullmatch(self.runtime_arn)
        if not match or match[1] != self.region or match[2] != self.expected_account:
            raise ValueError("invalid current AgentCore evidence")
        if (
            not re.fullmatch(r"[0-9]{12}\Z", self.expected_account)
            or not _QUALIFIER.fullmatch(self.qualifier)
            or self.qualifier == "DEFAULT"
            or not _VERSION.fullmatch(self.expected_version)
            or len(self.endpoint_arn) > 320
        ):
            raise ValueError("invalid current AgentCore evidence")

    @property
    def runtime_id(self) -> str:
        return _ARN.fullmatch(self.runtime_arn)[3]  # type: ignore[index]

    @property
    def endpoint_arn(self) -> str:
        return f"{self.runtime_arn}/runtime-endpoint/{self.qualifier}"


@dataclass(frozen=True, slots=True)
class CurrentAgentCoreEvidence:
    schema_version: int
    identity: CurrentRemoteIdentity
    target: AgentCoreTarget
    runtime_session_id: str
    request_sha256: str
    wrapper_sha256: str
    invocation_attempted: bool
    verified_version: str | None
    endpoint_arn: str | None
    aws_request_id: str | None
    trace_id: str | None
    response_sha256: str | None
    result_wire_sha256: str | None
    outcome: str
    stop_status: str
    error_code: str | None
    stop_error_code: str | None

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int or self.schema_version != 1
            or type(self.identity) is not CurrentRemoteIdentity
            or type(self.target) is not AgentCoreTarget
            or type(self.invocation_attempted) is not bool
            or type(self.runtime_session_id) is not str
            or not _SESSION.fullmatch(self.runtime_session_id)
            or type(self.outcome) is not str
            or self.outcome not in {"VALIDATED_EXECUTION", "VALIDATED_FAILURE", "OUTCOME_UNKNOWN"}
            or type(self.stop_status) is not str
            or self.stop_status not in {"NOT_STARTED", "STOP_REQUEST_ACCEPTED", "STOP_FAILED"}
        ):
            raise ValueError("invalid current AgentCore evidence")
        identity = replace(self.identity)
        target = replace(self.target)
        _hash(self.request_sha256)
        _hash(self.wrapper_sha256)
        _hash(self.response_sha256, optional=True)
        _hash(self.result_wire_sha256, optional=True)
        _optional_text(self.endpoint_arn, 320)
        _optional_text(self.aws_request_id, 128)
        _optional_text(self.trace_id, 256)
        for code in (self.error_code, self.stop_error_code):
            if code is not None and (type(code) is not str or not _CODE.fullmatch(code)):
                raise ValueError("invalid current AgentCore evidence")
        paired = (self.verified_version is None) == (self.endpoint_arn is None)
        if not paired or (
            self.verified_version is not None
            and (type(self.verified_version) is not str or not _VERSION.fullmatch(self.verified_version))
        ):
            raise ValueError("invalid current AgentCore evidence")
        if self.endpoint_arn is not None and self.endpoint_arn != target.endpoint_arn:
            raise ValueError("invalid current AgentCore evidence")
        if self.verified_version is not None and self.verified_version != target.expected_version:
            raise ValueError("invalid current AgentCore evidence")
        if self.stop_status == "STOP_FAILED":
            if self.stop_error_code is None:
                raise ValueError("invalid current AgentCore evidence")
        elif self.stop_error_code is not None:
            raise ValueError("invalid current AgentCore evidence")
        if not self.invocation_attempted:
            if self.verified_version is not None or self.aws_request_id is not None or self.trace_id is not None:
                raise ValueError("invalid current AgentCore evidence")
            if self.response_sha256 is not None or self.result_wire_sha256 is not None or self.stop_status != "NOT_STARTED":
                raise ValueError("invalid current AgentCore evidence")
            if self.outcome != "OUTCOME_UNKNOWN" or self.error_code is None:
                raise ValueError("invalid current AgentCore evidence")
        else:
            if self.verified_version is None or self.stop_status == "NOT_STARTED":
                raise ValueError("invalid current AgentCore evidence")
            if self.outcome in {"VALIDATED_EXECUTION", "VALIDATED_FAILURE"}:
                if self.error_code is not None or self.response_sha256 is None or self.result_wire_sha256 is None:
                    raise ValueError("invalid current AgentCore evidence")
            elif self.error_code is None or self.result_wire_sha256 is not None:
                raise ValueError("invalid current AgentCore evidence")
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "target", target)


class CurrentAgentCoreTransportError(RuntimeError):
    """Neutral transport uncertainty with immutable evidence."""

    def __init__(self, evidence: CurrentAgentCoreEvidence) -> None:
        if type(evidence) is not CurrentAgentCoreEvidence or evidence.outcome != "OUTCOME_UNKNOWN":
            raise ValueError("invalid current AgentCore transport evidence")
        self._evidence = replace(evidence)
        super().__init__("current AgentCore transport outcome is unknown")

    @property
    def evidence(self) -> CurrentAgentCoreEvidence:
        return replace(self._evidence)
