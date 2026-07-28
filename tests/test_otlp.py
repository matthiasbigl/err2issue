"""OTLP decoding, in both wire encodings, plus error selection.

Accepting only one encoding is a deployment trap: which one a collector sends
depends on the exporter's `encoding` setting, and the failure is silent (the
receiver 415s and the collector retries forever).
"""

from __future__ import annotations

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
                                "trace_id": "abc",
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
    assert decoded[0][1]["trace_id"] == "abc"


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
