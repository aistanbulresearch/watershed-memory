"""Production runtime mode and model selection belong to server configuration."""

from collections import UserDict
from dataclasses import replace
from importlib.metadata import version

import pytest
from starlette.testclient import TestClient
from test_current_field_dispatch_runner import sdk_field as sdk_field  # noqa: F401
from test_current_field_store import field as field  # noqa: F401
from test_current_field_store import ready as ready  # noqa: F401
from test_current_remote_types import reserved as reserved  # noqa: F401

from watershed_memory.current import runtime_config as config
from watershed_memory.current.agentcore_wrapper import encode_sdk_wrapper
from watershed_memory.current.delivery_types import InvocationProfile
from watershed_memory.current.remote_protocol import encode_request, make_request

ENV = {"WATERSHED_MODEL_ID": "us.amazon.nova-pro-v1:0", "WATERSHED_AWS_REGION": "us-east-1"}


@pytest.mark.parametrize("environment", [None, {}, [], list(ENV.items()),
    {**ENV, "WATERSHED_MODEL_ID": ""}, {**ENV, "WATERSHED_MODEL_ID": None},
    {**ENV, "WATERSHED_MODEL_ID": "private\ncanary"}, {**ENV, "WATERSHED_MODEL_ID": "a" * 201},
    {**ENV, "WATERSHED_MODEL_ID": "a b"}, {**ENV, "WATERSHED_AWS_REGION": "nonsense"},
    {**ENV, "WATERSHED_AWS_REGION": True}, {**ENV, "WATERSHED_AWS_REGION": "us-east-1 "},
])
def test_invalid_startup_configuration_never_constructs_provider(environment, monkeypatch):
    calls = []
    monkeypatch.setattr(config, "bedrock_field_planner", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.delenv("WATERSHED_MODEL_ID", raising=False)
    monkeypatch.delenv("WATERSHED_AWS_REGION", raising=False)
    with pytest.raises(ValueError) as caught:
        config.create_production_app(environment)
    assert calls == []
    assert str(caught.value) == "invalid current production runtime configuration"
    assert caught.value.__suppress_context__


def test_profile_factory_and_environment_are_frozen_at_startup(monkeypatch):
    calls, captured = [], {}
    marker = object()
    def app_factory(**kw):
        captured.update(kw)
        return marker
    def planner(*args, **kw):
        calls.append((args, kw))
        return "fixture"
    monkeypatch.setattr(config, "create_current_app", app_factory)
    monkeypatch.setattr(config, "bedrock_field_planner", planner)
    environment = UserDict({**ENV, "WATERSHED_ALLOW_SCRIPTED": "true"})
    assert config.create_production_app(environment) is marker
    assert calls == []
    environment["WATERSHED_MODEL_ID"] = "replacement"
    environment["WATERSHED_AWS_REGION"] = "eu-north-1"
    assert captured["expected_profile"] == InvocationProfile(
        "STRANDS_CURRENT", ENV["WATERSHED_MODEL_ID"], "watershed-current-v3", version("strands-agents"))
    assert captured["planner_factory"]() == "fixture"
    assert calls == [((ENV["WATERSHED_MODEL_ID"], ENV["WATERSHED_AWS_REGION"]), {})]


def test_default_environment_is_read_without_provider_on_ping(monkeypatch):
    calls = []
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("WATERSHED_ALLOW_SCRIPTED", "1")
    monkeypatch.setattr(config, "bedrock_field_planner", lambda *a, **kw: calls.append((a, kw)))
    app = config.create_production_app()
    with TestClient(app) as client:
        assert client.get("/ping").status_code == 200
    assert calls == []


def test_scripted_request_is_refused_before_production_factory(reserved, monkeypatch):
    _, _, local, reservation = reserved
    calls = []
    monkeypatch.setattr(config, "bedrock_field_planner", lambda *a, **kw: calls.append((a, kw)))
    app = config.create_production_app({**ENV, "WATERSHED_MODEL_ID": local.model_id,
        "WATERSHED_ALLOW_SCRIPTED": "true"})
    body = encode_sdk_wrapper(encode_request(make_request(reservation)))
    with TestClient(app) as client:
        response = client.post("/invocations", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 400 and calls == [] and local.model.response_count == 0


def test_matching_request_uses_only_frozen_production_factory_arguments(reserved, monkeypatch):
    _, _, local, reservation = reserved
    calls = []
    def factory(*args, **kw):
        calls.append((args, kw))
        raise RuntimeError("PRIVATE-CANARY")
    monkeypatch.setattr(config, "bedrock_field_planner", factory)
    expected = replace(local.profile, mode="STRANDS_CURRENT", model_id=ENV["WATERSHED_MODEL_ID"])
    request = make_request(replace(reservation, profile=expected))
    app = config.create_production_app(ENV)
    with TestClient(app) as client:
        response = client.post("/invocations", content=encode_sdk_wrapper(encode_request(request)),
            headers={"content-type": "application/json"})
    assert calls == [((ENV["WATERSHED_MODEL_ID"], ENV["WATERSHED_AWS_REGION"]), {})]
    assert response.status_code == 500 and "PRIVATE-CANARY" not in response.text
    assert local.model.response_count == 0
