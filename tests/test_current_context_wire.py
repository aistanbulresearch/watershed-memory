"""Current cloud context has one closed, lossless, read-only representation."""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_current_context_v3 import load
from test_current_field_store import (
    CANARY,
    NOW,
    OPERATOR,
    attached,
    contents,
    mutate,
    proposed,
    verified,
)
from test_current_field_store import field as field  # noqa: F401
from test_current_field_store import ready as ready  # noqa: F401

from watershed_memory.current import context_wire as wire
from watershed_memory.current.case_records import digest
from watershed_memory.current.context_v3_types import CurrentContextV3
from watershed_memory.current.context_wire import (
    CONTEXT_MAX_BYTES,
    decode_context_v3,
    encode_context_v3,
)
from watershed_memory.current.field_codec import encode as encode_field
from watershed_memory.current.field_types import CorrectFieldResult
from watershed_memory.current.registry import Compatibility, SourceRegistry
from watershed_memory.current.tools import _json


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@pytest.fixture
def context(field):
    verified(field, "PARTIAL")
    return load(field)


@pytest.mark.parametrize("state", ["EMPTY", "PLANNED", "ATTACHED", "VERIFIED", "CORRECTED"])
def test_full_context_round_trip_preserves_source_and_field_memory(field, state):
    if state == "PLANNED":
        proposed(field)
    elif state == "ATTACHED":
        attached(field)
    elif state in {"VERIFIED", "CORRECTED"}:
        result = verified(field).result
        if state == "CORRECTED":
            mutate(field, "correct_result", CorrectFieldResult(
                result.report.report_id, 1, "PARTIAL", "Corrected demonstration access result.",
                NOW, NOW + timedelta(minutes=10), True, CANARY,
            ), who=OPERATOR, now=NOW + timedelta(minutes=19))
    original = load(field)
    before = contents(field[0])
    encoded = encode_context_v3(original)
    decoded = decode_context_v3(encoded)
    assert decoded == original
    assert asdict(decoded) == asdict(original)
    assert _json(decoded) == _json(original)
    assert encode_context_v3(decoded) == encoded
    assert CANARY not in encoded
    assert contents(field[0]) == before
    assert decoded.base.source_health is not None
    assert len(encoded.encode("utf-8")) <= CONTEXT_MAX_BYTES


@pytest.mark.parametrize("path,value", [
    (("base", "case_revision"), True),
    (("base", "case_revision"), "1"),
    (("base", "simulated"), 1),
    (("base", "current", "revision"), 1.0),
    (("base", "current", "series", 0, "sample_count"), True),
    (("base", "current", "series", 0, "latest_value"), 1.5),
    (("base", "current", "series", 0, "latest_value"), "NaN"),
    (("base", "current", "series", 0, "latest_value"), "1e999"),
    (("base", "current", "interval_start"), "2026-09-12"),
    (("base", "source_health"), None),
    (("base", "prior"), {}),
    (("field_work", "latest_results", 0, "result", "report", "simulated"), 1),
    (("field_work", "latest_results", 0, "result", "verification_level"), "REPORTED"),
    (("field_digest",), "0" * 64),
])
def test_wire_rejects_coercion_bad_types_and_forged_field_join(context, path, value):
    raw = json.loads(encode_context_v3(context))
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        decode_context_v3(canonical(raw))


def dictionaries(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dictionaries(child)
    elif isinstance(value, list):
        for child in value:
            yield from dictionaries(child)


def test_every_nested_record_rejects_unknown_and_missing_fields(context):
    raw = json.loads(encode_context_v3(context))
    count = 0
    for record in dictionaries(raw):
        record["unexpected_private_override"] = "ignored fields are forbidden"
        with pytest.raises(ValueError):
            decode_context_v3(canonical(raw))
        del record["unexpected_private_override"]
        key = next(iter(record))
        original = record.pop(key)
        with pytest.raises(ValueError):
            decode_context_v3(canonical(raw))
        record[key] = original
        count += 1
    assert count > 20
    assert decode_context_v3(canonical(raw)) == context


@pytest.mark.parametrize("path,value", [
    (("sample_count",), -1), (("valid_count",), 16001),
    (("latest_at",), None), (("min_value",), None),
    (("max_value",), "-999999"), (("latest_value",), "999999999"),
    (("parameter_code",), "99999"), (("unit",), "invented"),
])
def test_source_summaries_are_validated_before_becoming_remote_context(context, path, value):
    raw = json.loads(encode_context_v3(context))
    raw["base"]["current"]["series"][0][path[0]] = value
    with pytest.raises(ValueError):
        decode_context_v3(canonical(raw))


@pytest.mark.parametrize("key,value", [
    ("interval_coverage", "MISSING"), ("freshness", "INVENTED"),
    ("missing_parameters", ["00060"]), ("null_latest_parameters", ["00060"]),
    ("stale_parameters", ["00060"]), ("policy_id", "unregistered-policy"),
    ("supersedes_event_id", "not-an-event"),
])
def test_derived_source_states_cannot_be_forged(context, key, value):
    raw = json.loads(encode_context_v3(context))
    raw["base"]["current"][key] = value
    with pytest.raises(ValueError):
        decode_context_v3(canonical(raw))


def test_private_source_case_is_not_confused_with_demonstration_work_case(context):
    assert context.base.current.case_id != context.base.case_id
    restored = decode_context_v3(encode_context_v3(context))
    assert restored.base.current.case_id == context.base.current.case_id
    assert restored.base.case_id == context.base.case_id


@pytest.mark.parametrize("invalid", [
    None, {}, b"{}", "[]", "null", "{", '{"a":1,"a":2}',
    '{"value":NaN}', '{"value":Infinity}', '{"value":1e999}',
    '{"bad":"\ud800"}', "[" * 1000 + "]" * 1000,
])
def test_invalid_input_fails_with_value_error(invalid):
    with pytest.raises(ValueError):
        decode_context_v3(invalid)


def test_canonical_and_resource_bounds_are_enforced(context):
    encoded = encode_context_v3(context)
    for bad in (" " + encoded, encoded + "\n", encoded.replace("+00:00", "Z"),
                encoded[:-1] + ',"base":{}}', " " * (CONTEXT_MAX_BYTES + 1)):
        with pytest.raises(ValueError):
            decode_context_v3(bad)


def test_encoder_revalidates_internal_fact_records(context):
    wrong_series = replace(context.base.current.series[0], sample_count=-1)
    wrong_current = replace(context.base.current, series=(wrong_series, *context.base.current.series[1:]))
    wrong_base = replace(context.base, current=wrong_current)
    wrong_context = replace(context, base=wrong_base)
    with pytest.raises(ValueError):
        encode_context_v3(wrong_context)


def test_encoder_accepts_only_exact_context_type(context):
    with pytest.raises(ValueError):
        encode_context_v3(asdict(context))


def test_encoder_preserves_decimal_representation_without_expanding_exponents(context):
    values = dict(latest_value=Decimal("1E+1"), min_value=Decimal("1E+1"), max_value=Decimal("1E+1"))
    item = replace(context.base.current.series[0], **values)
    current = replace(context.base.current, series=(item, *context.base.current.series[1:]))
    original = replace(context, base=replace(context.base, current=current))
    encoded = encode_context_v3(original)
    assert json.loads(encoded)["base"]["current"]["series"][0]["latest_value"] == "1E+1"
    assert _json(decode_context_v3(encoded)) == _json(original)


def test_encoder_revalidates_nested_field_primitive_types(context):
    broken = deepcopy(context)
    object.__setattr__(broken.field_work, "has_more_results", 1)
    with pytest.raises(ValueError):
        encode_context_v3(broken)


def test_encoder_revalidates_nested_field_digest(context):
    broken = deepcopy(context)
    object.__setattr__(broken, "field_digest", "0" * 64)
    with pytest.raises(ValueError):
        encode_context_v3(broken)


def test_encoder_reconstructs_nested_domain_records_even_with_matching_digest(context):
    broken = deepcopy(context)
    object.__setattr__(broken.field_work.latest_results[0].result.report, "outcome", "INVENTED")
    object.__setattr__(broken, "field_digest", digest(encode_field(asdict(broken.field_work))))
    with pytest.raises(ValueError):
        encode_context_v3(broken)


def test_unknown_source_identity_raises_sanitized_value_error(context):
    raw = json.loads(encode_context_v3(context))
    raw["base"]["current"]["source_id"] = "PRIVATE-CANARY"
    with pytest.raises(ValueError) as caught:
        decode_context_v3(canonical(raw))
    assert str(caught.value) == "invalid current context wire payload"
    assert caught.value.__suppress_context__


def test_invalid_utf8_error_does_not_retain_a_public_payload_message():
    with pytest.raises(ValueError) as caught:
        decode_context_v3('{"bad":"PRIVATE-CANARY\ud800"}')
    assert str(caught.value) == "invalid current context wire payload"
    assert caught.value.__suppress_context__


def shape(value, depth=1):
    children = list(value.values()) if type(value) is dict else value if type(value) is list else []
    measured = [shape(child, depth + 1) for child in children]
    return 1 + sum(count for count, _ in measured), max([depth, *(level for _, level in measured)])


def test_resource_limits_count_all_json_values_and_only_json_depth(context, monkeypatch):
    encoded = encode_context_v3(context)
    nodes, depth = shape(json.loads(encoded))
    monkeypatch.setattr(wire, "MAX_NODES", nodes)
    monkeypatch.setattr(wire, "MAX_DEPTH", depth)
    assert decode_context_v3(encoded) == context
    assert encode_context_v3(context) == encoded


def test_resource_limits_apply_to_primitive_nodes_in_both_directions(context, monkeypatch):
    encoded = encode_context_v3(context)
    nodes, _ = shape(json.loads(encoded))
    monkeypatch.setattr(wire, "MAX_NODES", nodes - 1)
    with pytest.raises(ValueError):
        decode_context_v3(encoded)
    with pytest.raises(ValueError):
        encode_context_v3(context)


def test_non_utc_interval_timestamp_alias_is_rejected(context):
    raw = json.loads(encode_context_v3(context))
    at = context.base.current.interval_start
    from datetime import timezone
    raw["base"]["current"]["interval_start"] = at.astimezone(timezone(timedelta(hours=1))).isoformat()
    with pytest.raises(ValueError):
        decode_context_v3(canonical(raw))


@pytest.mark.parametrize("outcome,level,expected", [
    ("COMPLETE", "VERIFIED", "NO_NEW_FIELD_PLAN"),
    ("COMPLETE", "EVIDENCE_ATTACHED", "AWAIT_VERIFICATION"),
    ("PARTIAL", "VERIFIED", "PROPOSE_FIELD_PLAN"),
])
def test_decoded_context_supports_actual_sdk_without_database_writes(field, outcome, level, expected):
    from test_current_field_sdk_fixture import FieldResultScriptedModel
    from test_current_field_strands import planner

    (verified if level == "VERIFIED" else attached)(field, outcome)
    original = load(field)
    before = contents(field[0])
    decoded = decode_context_v3(encode_context_v3(original))
    model = FieldResultScriptedModel()
    execution = planner(model).plan(decoded)
    basis = original.field_work.latest_results[0]
    decision = execution.assessment.field
    assert decision.disposition == expected
    assert decision.basis_report_id == basis.result.report.report_id
    assert decision.basis_report_revision == basis.result.report.revision
    assert decision.basis_verification_level == level
    assert execution.mode == "SCRIPTED_SDK" and execution.model_calls == 6
    assert contents(field[0]) == before


def test_api_built_later_revision_without_parent_retains_existing_fact_contract(context):
    current = replace(context.base.current, revision=2, supersedes_event_id=None)
    compatible = replace(context, base=replace(context.base, current=current))
    assert decode_context_v3(encode_context_v3(compatible)) == compatible


def test_current_correction_requires_its_direct_ancestor_in_selected_history(context):
    current = replace(context.base.current, revision=2, supersedes_event_id="f" * 64)
    wrong = replace(context, base=replace(context.base, current=current))
    with pytest.raises(ValueError):
        encode_context_v3(wrong)


def test_operational_work_cannot_import_another_source_case(field):
    empty = load(field)
    base = replace(empty.base, simulated=False, reviews=tuple(
        replace(review, simulated=False) for review in empty.base.reviews
    ))
    work = replace(empty.field_work, simulated=False, approved_locations=())
    wrong = CurrentContextV3(base, work, digest(encode_field(asdict(work))))
    with pytest.raises(ValueError):
        encode_context_v3(wrong)


def test_wire_registry_cannot_introduce_unapproved_alternate_evidence(context):
    origin = context.base.registry.sources[0]
    alternate = replace(origin, source_id="invented-alternate", station_id="USGS-00001")
    registry = SourceRegistry((origin, alternate), (
        Compatibility(origin.source_id, alternate.source_id, origin.series[0].parameter_code, "fake-authority"),
    ))
    wrong = replace(context, base=replace(context.base, registry=registry))
    with pytest.raises(ValueError):
        encode_context_v3(wrong)
