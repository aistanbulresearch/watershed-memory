"""Server-owned configuration for the current production runtime."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from importlib.metadata import version

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from .delivery_types import InvocationProfile
from .field_bedrock import bedrock_field_planner
from .runtime_handler import create_current_app

_ERROR = "invalid current production runtime configuration"
_REGION = re.compile(r"[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+\Z")


def _valid(value: object, maximum: int) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= maximum
        and value.isprintable()
        and not any(char.isspace() for char in value)
    )


def create_production_app(environ: Mapping[str, str] | None = None) -> BedrockAgentCoreApp:
    """Create a fixed STRANDS_CURRENT app from one environment snapshot."""
    try:
        if environ is not None and not isinstance(environ, Mapping):
            raise ValueError
        source = dict(os.environ if environ is None else environ)
        model_id = source.get("WATERSHED_MODEL_ID")
        region = source.get("WATERSHED_AWS_REGION")
        if not _valid(model_id, 200) or not _valid(region, 64) or not _REGION.fullmatch(region):
            raise ValueError
        profile = InvocationProfile(
            "STRANDS_CURRENT", model_id, "watershed-current-v3", version("strands-agents"),
        )

        def planner_factory():
            return bedrock_field_planner(model_id, region)

        return create_current_app(expected_profile=profile, planner_factory=planner_factory)
    except Exception:
        raise ValueError(_ERROR) from None
