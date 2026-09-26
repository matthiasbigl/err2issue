"""OTLP decoding, in both wire encodings, plus error selection.

Accepting only one encoding is a deployment trap: which one a collector sends
depends on the exporter's `encoding` setting, and the failure is silent (the
receiver 415s and the collector retries forever).
"""

from __future__ import annotations

import base64

import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

from err2issue import otlp
from tests.conftest import PY_TRACE, otlp_json


def build_protobuf(service="checkout-api", severity=17, exc_type="TypeError") -> bytes:
    request = ExportLogsServiceRequest()
    resource_logs = request.resource_logs.add()
    attribute = resource_logs.resource.attributes.add()
    attribute.key = "service.name"
    attribute.value.string_value = service

    scope_logs = resource_logs.scope_logs.add()
    record = scope_logs.log_records.add()
    record.severity_number = severity
    record.severity_text = "ERROR" if severity >= 17 else "INFO"
    record.time_unix_nano = 1_785_585_600_000_000_000
    record.body.string_value = "request failed"
    record.trace_id = bytes.fromhex("4bf92f3577b34da6a3ce929d0e0e4736")
    record.span_id = bytes.fromhex("00f067aa0ba902b7")
    if exc_type:
        for key, value in (
            ("exception.type", exc_type),
            ("exception.message", "boom"),
            ("exception.stacktrace", PY_TRACE),
        ):
            kv = record.attributes.add()
            kv.key = key
            kv.value.string_value = value
    return request.SerializeToString()


# -- protobuf --------------------------------------------------------------


def test_protobuf_round_trip():
    decoded = otlp.decode_protobuf(build_protobuf())
    assert len(decoded) == 1
    resource_attrs, record = decoded[0]
    assert resource_attrs["service.name"] == "checkout-api"
    assert record["attributes"]["exception.type"] == "TypeError"
    assert record["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert record["span_id"] == "00f067aa0ba902b7"


def test_protobuf_produces_an_error_event():
    events, _ = otlp.to_events(otlp.decode_protobuf(build_protobuf()))
    assert len(events) == 1
    assert events[0].exception_type == "TypeError"
    assert events[0].service_name == "checkout-api"
    assert events[0].stacktrace == PY_TRACE


def test_invalid_protobuf_raises_decode_error():
    import pytest

    with pytest.raises(otlp.DecodeError):
        otlp.decode_protobuf(b"\xff\xff\xff\xffnot-protobuf-at-all\x00\x01")


def test_empty_protobuf_body_decodes_to_nothing():
    assert otlp.decode_protobuf(b"") == []


# -- JSON ------------------------------------------------------------------


def test_json_round_trip():
    decoded = otlp.decode_json(otlp_json())
    assert len(decoded) == 1
    resource_attrs, record = decoded[0]
    assert resource_attrs["service.name"] == "checkout-api"
    assert record["attributes"]["exception.type"] == "TypeError"


def test_json_accepts_snake_case_field_names():
    """The protobuf JSON mapping permits original proto field names."""
    payload = {
        "resource_logs": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"string_value": "svc"}}]
                },
                "scope_logs": [
                    {
                        "log_records": [
                            {
                                "severity_number": 17,
                                "time_unix_nano": "1785585600000000000",
                                "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
                                "body": {"string_value": "failed"},
                            }
                        ]
                    }
                ],
            }
        ]
    }
    decoded = otlp.decode_json(payload)
    assert decoded[0][0]["service.name"] == "svc"
    assert decoded[0][1]["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_json_and_protobuf_agree():
    from_pb, _ = otlp.to_events(otlp.decode_protobuf(build_protobuf()))
    from_json, _ = otlp.to_events(otlp.decode_json(otlp_json(message="boom")))
    assert from_pb[0].exception_type == from_json[0].exception_type
    assert from_pb[0].service_name == from_json[0].service_name


def test_json_handles_every_anyvalue_kind():
    payload = otlp_json()
    record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    record["attributes"].extend(
        [
            {"key": "b", "value": {"boolValue": True}},
            {"key": "i", "value": {"intValue": "42"}},
            {"key": "d", "value": {"doubleValue": 1.5}},
            {"key": "arr", "value": {"arrayValue": {"values": [{"stringValue": "x"}]}}},
            {
                "key": "kv",
                "value": {"kvlistValue": {"values": [{"key": "n", "value": {"intValue": "1"}}]}},
            },
        ]
    )
    attributes = otlp.decode_json(payload)[0][1]["attributes"]
    assert attributes["b"] == "true"
    assert attributes["i"] == "42"
    assert attributes["arr"] == "[x]"
    assert attributes["kv"] == "{n=1}"


def test_non_object_json_body_is_rejected():
    import pytest

    with pytest.raises(otlp.DecodeError):
        otlp.decode_json([1, 2, 3])


def test_empty_json_decodes_to_nothing():
    assert otlp.decode_json({}) == []


# -- error selection -------------------------------------------------------


def test_severity_error_is_selected():
    decoded = otlp.decode_json(otlp_json(severity_number=17, exception_type=None, body="failed"))
    assert otlp.is_error(decoded[0][1])


def test_severity_fatal_is_selected():
    decoded = otlp.decode_json(otlp_json(severity_number=21, exception_type=None, body="dead"))
    assert otlp.is_error(decoded[0][1])


def test_severity_warn_is_not_selected():
    decoded = otlp.decode_json(otlp_json(severity_number=13, exception_type=None, body="careful"))
    assert not otlp.is_error(decoded[0][1])


def test_exception_attribute_selects_a_record_even_at_info_severity():
    """A handled-and-logged exception is still an error worth filing."""
    decoded = otlp.decode_json(otlp_json(severity_number=9, exception_type="ValueError"))
    assert otlp.is_error(decoded[0][1])


def test_require_exception_mode_ignores_severity_only_records():
    decoded = otlp.decode_json(otlp_json(severity_number=17, exception_type=None, body="failed"))
    assert not otlp.is_error(decoded[0][1], require_exception=True)


def test_severity_only_error_gets_a_synthetic_type_and_keeps_the_body():
    decoded = otlp.decode_json(
        otlp_json(severity_number=17, exception_type=None, body="db connection refused")
    )
    events, _ = otlp.to_events(decoded)
    assert events[0].exception_type == "Error"
    assert events[0].exception_message == "db connection refused"


def test_non_error_records_become_context_lines_not_events():
    payload = otlp_json(severity_number=9, exception_type=None, body="handling request")
    events, lines = otlp.to_events(otlp.decode_json(payload))
    assert events == []
    assert len(lines) == 1
    assert lines[0].text == "handling request"


def test_context_lines_without_a_body_are_dropped():
    payload = otlp_json(severity_number=9, exception_type=None, body="")
    _, lines = otlp.to_events(otlp.decode_json(payload))
    assert lines == []


def test_missing_service_name_falls_back_to_a_placeholder():
    payload = otlp_json()
    payload["resourceLogs"][0]["resource"]["attributes"] = []
    events, _ = otlp.to_events(otlp.decode_json(payload))
    assert events[0].service_name == "unknown-service"


def test_timestamp_is_converted_from_unix_nanos():
    events, _ = otlp.to_events(otlp.decode_json(otlp_json()))
    assert events[0].timestamp.year == 2026


def test_missing_timestamp_defaults_to_now_rather_than_1970():
    payload = otlp_json()
    payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["timeUnixNano"] = "0"
    events, _ = otlp.to_events(otlp.decode_json(payload))
    assert events[0].timestamp.year >= 2026


def test_multiple_resources_and_scopes_are_all_walked():
    payload = otlp_json()
    payload["resourceLogs"].append(otlp_json(service="other-api")["resourceLogs"][0])
    events, _ = otlp.to_events(otlp.decode_json(payload))
    assert {e.service_name for e in events} == {"checkout-api", "other-api"}


def test_exception_record_keeps_its_log_body_alongside_the_message():
    payload = otlp_json(
        exception_type="ConnectionClosedError",
        message="None",
        stacktrace=None,
        body="ConnectionClosedError exception in shielded future",
    )
    events, _ = otlp.to_events(otlp.decode_json(payload))
    assert events[0].exception_message == "None"
    assert events[0].body == "ConnectionClosedError exception in shielded future"


# -- malformed and non-canonical JSON --------------------------------------


def _record(payload: dict) -> dict:
    return payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]


@pytest.mark.parametrize(
    "payload",
    [
        {"resourceLogs": {"not": "a list"}},
        {"resourceLogs": [None, 3, "x"]},
        {"resourceLogs": [{"resource": [], "scopeLogs": {"x": 1}}]},
        {"resourceLogs": [{"resource": {"attributes": [1, None, {"key": "k"}]}}]},
        {"resourceLogs": [{"scopeLogs": [None, {"logRecords": [None, 7]}]}]},
        {"resourceLogs": [{"scopeLogs": [{"logRecords": [{"timeUnixNano": "soon"}]}]}]},
        {"resourceLogs": [{"scopeLogs": [{"logRecords": [{"severityNumber": [17]}]}]}]},
        {"resourceLogs": [{"scopeLogs": [{"logRecords": [{"traceId": 12, "spanId": {}}]}]}]},
        {
            "resourceLogs": [
                {
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {
                                    "severityNumber": 17,
                                    "body": {"kvlistValue": [1, 2]},
                                    "attributes": [{"key": "a", "value": {"arrayValue": 5}}],
                                }
                            ]
                        }
                    ]
                }
            ]
        },
    ],
)
def test_malformed_json_shapes_never_raise(payload):
    otlp.to_events(otlp.decode_json(payload))


def test_severity_enum_names_from_protobuf_json_encoders_are_understood():
    payload = otlp_json(exception_type=None, body="db down")
    _record(payload)["severityNumber"] = "SEVERITY_NUMBER_FATAL2"
    events, _ = otlp.to_events(otlp.decode_json(payload))
    assert len(events) == 1
    assert events[0].severity_number == 22


def test_a_timestamp_far_past_year_9999_falls_back_to_now():
    payload = otlp_json()
    _record(payload)["timeUnixNano"] = "9" * 25
    events, _ = otlp.to_events(otlp.decode_json(payload))
    assert events[0].timestamp.year >= 2026


def test_zero_event_time_falls_back_to_observed_time_in_json():
    payload = otlp_json()
    _record(payload)["timeUnixNano"] = "0"
    _record(payload)["observedTimeUnixNano"] = "1785585600000000000"
    events, _ = otlp.to_events(otlp.decode_json(payload))
    assert (events[0].timestamp.year, events[0].timestamp.month) == (2026, 8)


# -- trace / span id normalization -----------------------------------------

TRACE_HEX = "4bf92f3577b34da6a3ce929d0e0e4736"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (TRACE_HEX, TRACE_HEX),
        (TRACE_HEX.upper(), TRACE_HEX),
        (f"  {TRACE_HEX} ", TRACE_HEX),
        (base64.b64encode(bytes.fromhex(TRACE_HEX)).decode(), TRACE_HEX),
        ("0" * 32, None),
        (base64.b64encode(bytes(16)).decode(), None),
        ("abc", None),
        ("zz" * 16, None),
        (TRACE_HEX[:-2], None),
    ],
)
def test_json_trace_ids_are_normalized_to_lowercase_hex(raw, expected):
    _, record = otlp.decode_json(otlp_json(trace_id=raw))[0]
    assert record["trace_id"] == expected


def test_json_span_id_must_be_eight_bytes():
    payload = otlp_json()
    _record(payload)["spanId"] = "00F067AA0BA902B7"
    assert otlp.decode_json(payload)[0][1]["span_id"] == "00f067aa0ba902b7"
    _record(payload)["spanId"] = TRACE_HEX
    assert otlp.decode_json(payload)[0][1]["span_id"] is None


def test_all_zero_protobuf_ids_mean_no_trace():
    request = ExportLogsServiceRequest()
    request.ParseFromString(build_protobuf())
    record = request.resource_logs[0].scope_logs[0].log_records[0]
    record.trace_id = bytes(16)
    record.span_id = bytes(8)
    _, decoded = otlp.decode_protobuf(request.SerializeToString())[0]
    assert decoded["trace_id"] is None
    assert decoded["span_id"] is None


def test_span_less_lines_are_not_correlated_through_a_zero_trace():
    """Before normalization every span-less INFO line landed in one shared
    trace-000…0 bucket and showed up as "correlated" context on every span-less
    error."""
    from err2issue.context import TraceBuffer

    buffer = TraceBuffer(max_traces=10)
    info = otlp.decode_json(
        otlp_json(severity_number=9, exception_type=None, body="unrelated", trace_id="0" * 32)
    )
    _, lines = otlp.to_events(info)
    for line, (_, record) in zip(lines, info, strict=True):
        buffer.add(record["trace_id"], line)
    error = otlp.decode_json(otlp_json(trace_id="0" * 32))
    events, _ = otlp.to_events(error)
    assert events[0].trace_id is None
    assert buffer.get(events[0].trace_id) == []


# -- severity text fallback and message-only exceptions --------------------


def _unspecified(text: str, **kwargs) -> dict:
    payload = otlp_json(severity_number=0, exception_type=None, body="db pool exhausted", **kwargs)
    _record(payload)["severityText"] = text
    return payload


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("ERROR", 17),
        ("error", 17),
        (" Error ", 17),
        ("ERROR3", 19),
        ("SEVERE", 17),
        ("FATAL", 21),
        ("CRITICAL", 21),
        ("critical", 21),
        ("EMERG", 23),
    ],
)
def test_unspecified_number_falls_back_to_severity_text(text, number):
    events, lines = otlp.to_events(otlp.decode_json(_unspecified(text)))
    assert len(events) == 1 and not lines
    assert events[0].severity_number == number
    assert events[0].exception_type == "Error"
    assert events[0].exception_message == "db pool exhausted"


@pytest.mark.parametrize("text", ["INFO", "WARN", "warning", "", "ERRORS", "debug"])
def test_non_error_severity_text_stays_a_context_line(text):
    events, lines = otlp.to_events(otlp.decode_json(_unspecified(text)))
    assert not events and len(lines) == 1


def test_severity_text_never_overrides_an_explicit_number():
    payload = otlp_json(severity_number=9, exception_type=None, body="handled")
    _record(payload)["severityText"] = "ERROR"
    events, _ = otlp.to_events(otlp.decode_json(payload))
    assert events == []


def test_severity_text_fallback_works_over_protobuf():
    request = ExportLogsServiceRequest()
    request.ParseFromString(build_protobuf(exc_type=None))
    record = request.resource_logs[0].scope_logs[0].log_records[0]
    record.severity_number = 0
    record.severity_text = "fatal"
    events, _ = otlp.to_events(otlp.decode_protobuf(request.SerializeToString()))
    assert [e.severity_number for e in events] == [21]


def test_severity_text_does_not_bypass_require_exception():
    events, _ = otlp.to_events(otlp.decode_json(_unspecified("ERROR")), require_exception=True)
    assert events == []


def test_exception_message_without_type_counts_as_an_exception():
    """Semconv: exception.type OR exception.message. `require_exception` used to
    look at the type only, so message-only SDKs were dropped entirely."""
    payload = otlp_json(severity_number=9, exception_type=None)
    _record(payload)["attributes"] = [
        {"key": "exception.message", "value": {"stringValue": "connection reset"}}
    ]
    events, _ = otlp.to_events(otlp.decode_json(payload), require_exception=True)
    assert len(events) == 1
    assert (events[0].exception_type, events[0].exception_message) == (
        "Error",
        "connection reset",
    )


def test_blank_exception_message_alone_is_not_an_exception():
    payload = otlp_json(severity_number=9, exception_type=None, body="fine")
    _record(payload)["attributes"] = [{"key": "exception.message", "value": {"stringValue": " "}}]
    events, _ = otlp.to_events(otlp.decode_json(payload))
    assert events == []
