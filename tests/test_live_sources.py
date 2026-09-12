"""Independent acceptance for the real-source boundary; no network in these tests."""

import copy
import hashlib
import io
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from watershed_memory.watch.observations import SeriesSpec, SourceError
from watershed_memory.watch.usgs import GALLINAS_SERIES, HTTPResponse, USGSClient

UTC = timezone.utc
START = datetime(2026, 9, 12, 11, 15, tzinfo=UTC)
END = START + timedelta(minutes=30)
NOW = END + timedelta(minutes=15)
BASE = "https://api.waterdata.usgs.gov/ogcapi/v1/collections/continuous/items"
SITE = "USGS-08380500"
FLOW = "9857d8ac60994a19bc7a8ec28809066f"
RAIN = "9ff19bc69da442b3b96ef5bdff3732e2"
TURBIDITY = "4fb226f4b9aa49c8a04d181db42095d1"


def feature(**changes):
    properties = {
        "time_series_id": FLOW,
        "monitoring_location_id": SITE,
        "parameter_code": "00060",
        "statistic_id": "00011",
        "time": START.isoformat(),
        "value": "3.640",
        "unit_of_measure": "ft^3/s",
        "approval_status": "Provisional",
        "qualifier": None,
        "last_modified": NOW.isoformat(),
    }
    properties.update(changes)
    return {"type": "Feature", "id": "provider-uuid-1", "properties": properties}


def response(features=(), links=(), *, status=200, headers=None):
    body = json.dumps({"type": "FeatureCollection", "features": features, "links": links}).encode()
    return HTTPResponse(status=status, body=body, headers=headers or {})


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, timeout, max_bytes):
        self.calls.append((url, timeout, max_bytes))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def run(*features, **options):
    transport = Transport(response(features))
    batch = USGSClient(transport=transport, **options).fetch(START, END, retrieved_at=NOW)
    return batch, transport


def next_link(cursor="cursor-2", **changes):
    query = {
        "f": "json",
        "monitoring_location_id": SITE,
        "parameter_code": "00045,00060,63680",
        "datetime": "2026-09-12T11:15:00Z/2026-09-12T11:45:00Z",
        "limit": "1000",
        "cursor": cursor,
    }
    query.update(changes)
    return {"rel": "next", "href": BASE + "?" + urlencode(query)}


def test_real_schema_has_exact_precision_identity_and_provenance():
    batch, transport = run(feature())
    observation = batch.observations[0]
    assert observation.identity == (FLOW, START)
    assert observation.station_id == SITE
    assert observation.parameter_code == "00060"
    assert observation.statistic_id == "00011"
    assert observation.value == Decimal("3.640")
    assert observation.unit == "ft^3/s"
    assert observation.source_modified_at == NOW
    assert observation.approval_status == "Provisional"
    assert observation.qualifier is None
    assert observation.provider_id == "provider-uuid-1"
    assert batch.station_id == SITE
    assert (batch.start, batch.end, batch.retrieved_at) == (START, END, NOW)
    assert batch.pages[0].sha256 == hashlib.sha256(response([feature()]).body).hexdigest()
    assert batch.pages[0].byte_count == len(response([feature()]).body)
    parsed = urlsplit(transport.calls[0][0])
    assert parsed.scheme == "https" and parsed.netloc == "api.waterdata.usgs.gov"
    assert parse_qs(parsed.query)["parameter_code"] == ["00045,00060,63680"]
    with pytest.raises(FrozenInstanceError):
        observation.value = Decimal("0")


def test_all_three_verified_series_and_null_are_preserved():
    batch, _ = run(
        feature(),
        feature(
            time_series_id=RAIN,
            parameter_code="00045",
            statistic_id=None,
            value="0.00",
            unit_of_measure="in",
        ),
        feature(
            time_series_id=TURBIDITY, parameter_code="63680", value=None, unit_of_measure="_FNU"
        ),
    )
    assert {item.series_id for item in batch.observations} == {FLOW, RAIN, TURBIDITY}
    assert next(item for item in batch.observations if item.series_id == TURBIDITY).value is None
    assert {spec.series_id for spec in GALLINAS_SERIES} == {FLOW, RAIN, TURBIDITY}


def test_replication_ids_modification_time_precision_and_timezone_do_not_change_semantics():
    original, _ = run(feature())
    repeated = feature(
        value="3.64",
        time="2026-09-12T05:15:00-06:00",
        last_modified=(NOW + timedelta(seconds=1)).isoformat(),
    )
    repeated["id"] = "replacement-provider-uuid"
    republished, _ = run(repeated)
    assert original.observations[0].identity == republished.observations[0].identity
    assert original.observations[0].semantic_hash == republished.observations[0].semantic_hash


@pytest.mark.parametrize(
    "changes",
    [
        {"value": "3.65"},
        {"value": None},
        {"approval_status": "Approved"},
        {"qualifier": "Ice"},
    ],
)
def test_semantic_corrections_keep_identity_but_change_hash(changes):
    original, _ = run(feature())
    corrected, _ = run(feature(**changes))
    assert original.observations[0].identity == corrected.observations[0].identity
    assert original.observations[0].semantic_hash != corrected.observations[0].semantic_hash


def test_negative_zero_and_precision_equivalent_values_have_same_hash():
    zero, _ = run(feature(value="0.000"))
    negative_zero, _ = run(feature(value="-0"))
    assert zero.observations[0].semantic_hash == negative_zero.observations[0].semantic_hash


@pytest.mark.parametrize(
    "changes",
    [
        {"monitoring_location_id": "USGS-OTHER"},
        {"time_series_id": "unknown"},
        {"parameter_code": "00045"},
        {"unit_of_measure": "m3/s"},
        {"statistic_id": None},
        {"time": "2026-09-12T11:15:00"},
        {"time": "2026-09-12"},
        {"time": (START - timedelta(seconds=1)).isoformat()},
        {"time": (END + timedelta(seconds=1)).isoformat()},
        {"last_modified": "2026-09-12T12:00:00"},
        {"approval_status": "Guaranteed"},
        {"qualifier": []},
        {"qualifier": "x" * 1000},
        {"value": True},
        {"value": 3.64},
        {"value": "NaN"},
        {"value": "Infinity"},
        {"value": "1e999999999"},
        {"value": "1e-999999999"},
        {"value": "x" * 2000},
    ],
)
def test_invalid_or_unrelated_measurement_rejects_whole_batch(changes):
    with pytest.raises(SourceError):
        run(feature(), feature(**changes))


def test_missing_required_fields_and_non_feature_are_rejected():
    bad = feature()
    del bad["properties"]["value"]
    with pytest.raises(SourceError):
        run(bad)
    with pytest.raises(SourceError):
        run({"type": "not-a-feature", "properties": feature()["properties"]})


def test_pagination_finishes_before_batch_return_and_collapses_equal_duplicates():
    second = feature(time=(START + timedelta(minutes=5)).isoformat())
    transport = Transport(response([feature()], [next_link()]), response([feature(), second]))
    batch = USGSClient(transport=transport).fetch(START, END, retrieved_at=NOW)
    assert len(transport.calls) == 2
    assert len(batch.pages) == 2
    assert [item.observed_at for item in batch.observations] == [
        START,
        START + timedelta(minutes=5),
    ]


def test_conflicting_duplicate_identity_across_pages_fails_atomically():
    transport = Transport(response([feature()], [next_link()]), response([feature(value="8")]))
    with pytest.raises(SourceError, match="(?i)conflict"):
        USGSClient(transport=transport).fetch(START, END, retrieved_at=NOW)


def offset_link(offset="1000", **changes):
    link = next_link(**changes)
    query = parse_qs(urlsplit(link["href"]).query)
    query.pop("cursor")
    query["offset"] = [offset]
    return {"rel": "next", "href": BASE + "?" + urlencode(query, doseq=True)}


def test_real_provider_offset_pagination_preserves_complete_batch():
    transport = Transport(
        response([feature()], [offset_link()]),
        response([feature(time=(START + timedelta(minutes=5)).isoformat())], [offset_link("2000")]),
        response([feature(time=(START + timedelta(minutes=10)).isoformat())]),
    )
    batch = USGSClient(transport=transport).fetch(START, END, retrieved_at=NOW)
    assert len(batch.pages) == len(batch.observations) == len(transport.calls) == 3


@pytest.mark.parametrize("offset", ["0", "-1", "100000000", "1.5", "1001", "bad", ""])
def test_offset_cannot_skip_pages_or_expand_source_scope(offset):
    transport = Transport(response([], [offset_link(offset)]))
    with pytest.raises(SourceError):
        USGSClient(transport=transport).fetch(START, END, retrieved_at=NOW)
    assert len(transport.calls) == 1


def test_offset_and_cursor_together_are_ambiguous_and_rejected():
    link = offset_link()
    link["href"] += "&cursor=also-present"
    transport = Transport(response([], [link]))
    with pytest.raises(SourceError):
        USGSClient(transport=transport).fetch(START, END, retrieved_at=NOW)


@pytest.mark.parametrize(
    "link",
    [
        {"rel": "next", "href": "https://untrusted.example/collect"},
        {
            "rel": "next",
            "href": "http://api.waterdata.usgs.gov/ogcapi/v1/collections/continuous/items",
        },
        {"rel": "next", "href": BASE.replace("api.waterdata", "user:password@api.waterdata")},
        next_link(monitoring_location_id="USGS-OTHER"),
        next_link(parameter_code="00095"),
        next_link(datetime="2000-01-01T00:00:00Z/2026-09-12T11:45:00Z"),
        next_link(limit="1000000"),
        next_link(secret="unexpected"),
        {"rel": "next", "href": next_link()["href"] + "#fragment"},
        {"rel": "next", "href": next_link()["href"] + "&limit=1000"},
    ],
)
def test_pagination_cannot_escape_original_request(link):
    transport = Transport(response([feature()], [link]))
    with pytest.raises(SourceError):
        USGSClient(transport=transport).fetch(START, END, retrieved_at=NOW)
    assert len(transport.calls) == 1


def test_multiple_next_links_cycles_and_page_cap_fail():
    for pages, options in [
        ([response([], [next_link(), next_link("another")])], {}),
        ([response([], [next_link()]), response([], [next_link()])], {}),
        ([response([], [next_link()])], {"max_pages": 1}),
    ]:
        transport = Transport(*pages)
        with pytest.raises(SourceError):
            USGSClient(transport=transport, **options).fetch(START, END, retrieved_at=NOW)


@pytest.mark.parametrize("status", [301, 302, 401, 429, 500, 503])
def test_http_errors_never_return_observations_or_retry_invisibly(status):
    transport = Transport(response([feature()], status=status, headers={"Retry-After": "120"}))
    with pytest.raises(SourceError) as caught:
        USGSClient(transport=transport).fetch(START, END, retrieved_at=NOW)
    assert caught.value.code == "HTTP"
    assert len(transport.calls) == 1
    if status == 429:
        assert caught.value.retry_after_seconds == 120


@pytest.mark.parametrize("body", [b"not json", b"[]", b"{}", b'{"features":[]}', b"\xff"])
def test_malformed_response_never_looks_like_no_new_data(body):
    transport = Transport(HTTPResponse(status=200, body=body, headers={}))
    with pytest.raises(SourceError):
        USGSClient(transport=transport).fetch(START, END, retrieved_at=NOW)


def test_oversize_payload_and_timeout_fail_without_partial_result():
    transport = Transport(HTTPResponse(status=200, body=b" " * 1025, headers={}))
    with pytest.raises(SourceError):
        USGSClient(transport=transport, max_bytes=1024).fetch(START, END, retrieved_at=NOW)
    transport = Transport(response([feature()], [next_link()]), TimeoutError())
    with pytest.raises(SourceError) as caught:
        USGSClient(transport=transport).fetch(START, END, retrieved_at=NOW)
    assert caught.value.code == "TRANSPORT"


def test_empty_bounded_response_is_valid_but_has_source_receipt():
    batch, _ = run()
    assert batch.observations == ()
    assert len(batch.pages) == 1


def test_invalid_configuration_or_time_window_performs_no_http():
    transport = Transport()
    for options in ({"page_size": 0}, {"max_pages": 0}, {"timeout": 0}, {"max_bytes": 0}):
        with pytest.raises(ValueError):
            USGSClient(transport=transport, **options)
    for start, end, now in [
        (START.replace(tzinfo=None), END, NOW),
        (END, START, NOW),
        (START, START + timedelta(days=2), NOW),
        (START, END, NOW.replace(tzinfo=None)),
    ]:
        with pytest.raises(ValueError):
            USGSClient(transport=transport).fetch(start, end, retrieved_at=now)
    assert not transport.calls


def test_input_response_is_not_mutated():
    data = feature()
    before = copy.deepcopy(data)
    run(data)
    assert data == before


@pytest.mark.parametrize(
    "options",
    [
        {"max_pages": 64, "max_bytes": 16 * 1024 * 1024},
        {"max_pages": 2, "page_size": 10000},
        {"max_pages": 17},
    ],
)
def test_configuration_cannot_multiply_per_page_bounds_into_unbounded_work(options):
    with pytest.raises(ValueError):
        USGSClient(**options)


@pytest.mark.parametrize(
    "changes",
    [
        {"time_series_id": []},
        {"time_series_id": {}},
        {"parameter_code": 60},
        {"monitoring_location_id": [SITE]},
        {"unit_of_measure": ["ft^3/s"]},
        {"statistic_id": ["00011"]},
        {"approval_status": {}},
        {"time": "2026-09-12 11:15:00+00:00"},
        {"time": "2026-09-12T11:15:00.0000001Z"},
        {"time": "2026-09-12T11:15:00+0000"},
    ],
)
def test_malformed_source_fields_always_raise_typed_schema_error(changes):
    with pytest.raises(SourceError) as caught:
        run(feature(**changes))
    assert caught.value.code == "SCHEMA"


@pytest.mark.parametrize("provider_id", [None, [], {}, 42, "", "x" * 1000])
def test_provider_id_must_be_bounded_string(provider_id):
    data = feature()
    data["id"] = provider_id
    with pytest.raises(SourceError):
        run(data)


def test_missing_provider_id_is_rejected():
    data = feature()
    del data["id"]
    with pytest.raises(SourceError):
        run(data)


@pytest.mark.parametrize(
    "options",
    [
        {"station_id": "USGS-08380500,USGS-OTHER"},
        {"station_id": "USGS-08380500\n"},
        {"transport": "not callable"},
    ],
)
def test_source_configuration_rejects_delimiters_and_non_callable_transport(options):
    with pytest.raises(ValueError):
        USGSClient(**options)


@pytest.mark.parametrize(
    "changes",
    [
        {"series_id": "x" * 1000},
        {"series_id": "two,series"},
        {"parameter_code": "00060,00045"},
        {"parameter_code": "60"},
        {"unit": "x" * 1000},
        {"unit": "ft\n3/s"},
        {"statistic_id": "eleven"},
    ],
)
def test_series_configuration_has_bounded_identifier_grammar(changes):
    values = dict(series_id=FLOW, parameter_code="00060", unit="ft^3/s", statistic_id="00011")
    values.update(changes)
    with pytest.raises(ValueError):
        USGSClient(specs=(SeriesSpec(**values),))


def test_equal_duplicate_provenance_is_independent_of_source_order():
    first, second = feature(), feature()
    first["id"], second["id"] = "provider-a", "provider-b"
    forward, _ = run(first, second)
    backward, _ = run(second, first)
    assert forward.observations == backward.observations


def test_http_headers_are_copied_and_immutable():
    headers = {"Retry-After": "120"}
    record = HTTPResponse(status=429, body=b"{}", headers=headers)
    headers["Retry-After"] = "300"
    assert record.headers["Retry-After"] == "120"
    with pytest.raises(TypeError):
        record.headers["Retry-After"] = "600"


@pytest.mark.parametrize(
    "status,body,headers",
    [
        (True, b"{}", {}),
        (200, "{}", {}),
        (200, b"{}", []),
        (200, b"{}", {"Retry-After": 120}),
    ],
)
def test_http_response_fields_are_typed(status, body, headers):
    with pytest.raises((ValueError, TypeError)):
        HTTPResponse(status=status, body=body, headers=headers)


def test_default_transport_closes_http_error_body(monkeypatch):
    stream = io.BytesIO(b"rate limited")
    error = HTTPError(BASE, 429, "rate limited", {"Retry-After": "120"}, stream)

    class Opener:
        def open(self, *args, **kwargs):
            raise error

    monkeypatch.setattr("urllib.request.build_opener", lambda *a: Opener())
    with pytest.raises(SourceError) as caught:
        USGSClient().fetch(START, END, retrieved_at=NOW)
    assert caught.value.code == "HTTP"
    assert stream.closed


@pytest.mark.parametrize("value", [" 3.64 ", "3_64", "\t3.64", "\u0663.64", "", "+ 3"])
def test_numeric_text_cannot_use_python_only_decimal_syntax(value):
    with pytest.raises(SourceError):
        run(feature(value=value))


@pytest.mark.parametrize(
    "code,retry",
    [
        ("PRIVATE-TEXT", None),
        ("HTTP", True),
        ("HTTP", -1),
        ("HTTP", 3601),
        ("HTTP", "120"),
    ],
)
def test_source_errors_expose_only_typed_bounded_public_metadata(code, retry):
    with pytest.raises(ValueError):
        SourceError("private detail", code=code, retry_after_seconds=retry)


@pytest.mark.parametrize("unit", ["ft\t3/s", "ft\x013/s", "ft\x7f3/s", "x" * 129])
def test_configured_units_reject_all_controls_and_contract_overflow(unit):
    with pytest.raises(ValueError):
        USGSClient(specs=(SeriesSpec(FLOW, "00060", unit, "00011"),))


@pytest.mark.parametrize("qualifier", ["x" * 129, "q\tflag", "q\x01flag", "q\x7fflag"])
def test_qualifier_text_is_printable_and_bounded(qualifier):
    with pytest.raises(SourceError):
        run(feature(qualifier=qualifier))


def test_provider_id_uses_the_same_128_character_limit():
    data = feature()
    data["id"] = "p" * 129
    with pytest.raises(SourceError):
        run(data)
