"""Build the production current planner without making a provider invocation."""

from importlib.metadata import version

import pytest
from test_current_field_sdk_fixture import FieldResultScriptedModel

from watershed_memory.current import field_bedrock
from watershed_memory.current.field_bedrock import bedrock_field_planner
from watershed_memory.current.field_strands import CurrentStrandsPlannerV3


@pytest.fixture
def constructors(monkeypatch):
    calls = []
    session = object()
    model = FieldResultScriptedModel()

    def session_factory(**kwargs):
        calls.append(("session", kwargs))
        return session

    def model_factory(**kwargs):
        calls.append(("model", kwargs))
        return model

    monkeypatch.setattr(field_bedrock.boto3, "Session", session_factory)
    monkeypatch.setattr(field_bedrock, "BedrockModel", model_factory)
    return calls, session, model


@pytest.mark.parametrize("profile", [None, "watershed-memory", "research team"])
def test_fixed_production_profile_and_bounded_provider_configuration(constructors, profile):
    calls, session, model = constructors
    planner = bedrock_field_planner("amazon.nova-pro-v1:0", "us-east-1", aws_profile=profile)
    assert type(planner) is CurrentStrandsPlannerV3
    assert planner.profile.mode == "STRANDS_CURRENT"
    assert planner.profile.model_id == "amazon.nova-pro-v1:0"
    assert planner.profile.instruction_version == "watershed-current-v3"
    assert planner.profile.sdk_version == version("strands-agents")
    assert planner.scripted_test is False and planner.max_calls == 12 and planner.seconds == 120
    assert planner.model is model and model.response_count == 0
    assert calls[0] == ("session", {"profile_name": profile, "region_name": "us-east-1"})
    assert calls[1][0] == "model" and len(calls) == 2
    settings = calls[1][1]
    assert set(settings) == {"model_id", "max_tokens", "temperature", "boto_session", "boto_client_config"}
    assert settings["model_id"] == planner.profile.model_id
    assert settings["boto_session"] is session
    assert settings["max_tokens"] == 1000 and settings["temperature"] == 0
    config = settings["boto_client_config"]
    assert config.connect_timeout == 5 and config.read_timeout == 40
    assert config.retries == {"max_attempts": 0}


def test_nova_2_lite_enables_bounded_low_reasoning_without_changing_turn_guards(constructors):
    calls, session, model = constructors
    planner = bedrock_field_planner(
        "us.amazon.nova-2-lite-v1:0", "us-east-1", aws_profile="watershed-memory"
    )
    assert planner.profile.model_id == "us.amazon.nova-2-lite-v1:0"
    assert planner.scripted_test is False and planner.max_calls == 12 and planner.seconds == 120
    assert planner.model is model and model.response_count == 0
    assert calls[0] == (
        "session", {"profile_name": "watershed-memory", "region_name": "us-east-1"}
    )
    settings = calls[1][1]
    assert set(settings) == {
        "model_id", "max_tokens", "temperature", "boto_session", "boto_client_config",
        "additional_request_fields",
    }
    assert settings["boto_session"] is session
    assert settings["max_tokens"] == 5000 and settings["temperature"] == 0
    assert settings["additional_request_fields"] == {
        "reasoningConfig": {"type": "enabled", "maxReasoningEffort": "low"}
    }


@pytest.mark.parametrize("change", [
    {"model_id": None}, {"model_id": ""}, {"model_id": "PRIVATE-CANARY\n"},
    {"model_id": "model with spaces"}, {"model_id": "a" * 201},
    {"region": None}, {"region": ""}, {"region": "https://example.invalid"},
    {"region": "US-EAST-1"}, {"region": "us-east-1 "},
    {"aws_profile": True}, {"aws_profile": ""}, {"aws_profile": " "},
    {"aws_profile": " name"}, {"aws_profile": "a" * 201},
])
def test_invalid_configuration_refused_before_sdk_construction(constructors, change):
    args = {"model_id": "amazon.nova-pro-v1:0", "region": "us-east-1", "aws_profile": None, **change}
    with pytest.raises(ValueError, match="^invalid current Bedrock configuration$") as caught:
        bedrock_field_planner(**args)
    assert caught.value.__suppress_context__
    assert constructors[0] == [] and constructors[2].response_count == 0


def test_production_constructor_exposes_no_scripted_switch(constructors):
    with pytest.raises(TypeError):
        bedrock_field_planner("amazon.nova-pro-v1:0", "us-east-1", scripted_test=True)
    assert constructors[0] == []
