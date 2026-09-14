"""Construction of the fixed production Bedrock planner."""

from __future__ import annotations

import re

import boto3
from botocore.config import Config
from strands.models.bedrock import BedrockModel

from .field_strands import CurrentStrandsPlannerV3

_ERROR = "invalid current Bedrock configuration"
_REGION = re.compile(r"[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+\Z")
_NOVA_2_LITE = "us.amazon.nova-2-lite-v1:0"


def _valid_text(value: object, *, allow_none: bool = False) -> bool:
    if allow_none and value is None:
        return True
    return (
        type(value) is str
        and bool(value)
        and len(value) <= 200
        and value.isprintable()
        and value == value.strip()
    )


def bedrock_field_planner(
    model_id: str, region: str, *, aws_profile: str | None = None,
) -> CurrentStrandsPlannerV3:
    """Build the current planner without invoking the provider."""
    if (
        not _valid_text(model_id)
        or any(char.isspace() for char in model_id)
        or type(region) is not str
        or len(region) > 64
        or not _REGION.fullmatch(region)
        or not _valid_text(aws_profile, allow_none=True)
    ):
        raise ValueError(_ERROR) from None
    try:
        session = boto3.Session(profile_name=aws_profile, region_name=region)
        model_config = {
            "model_id": model_id,
            "max_tokens": 1000,
            "temperature": 0,
            "boto_session": session,
            "boto_client_config": Config(
                connect_timeout=5,
                read_timeout=40,
                retries={"max_attempts": 0},
            ),
        }
        if model_id == _NOVA_2_LITE:
            model_config.update(
                max_tokens=5000,
                additional_request_fields={
                    "reasoningConfig": {"type": "enabled", "maxReasoningEffort": "low"}
                },
            )
        model = BedrockModel(**model_config)
        return CurrentStrandsPlannerV3(
            model, model_id=model_id, scripted_test=False, max_calls=12, seconds=120,
        )
    except Exception:
        raise ValueError(_ERROR) from None
