"""OTLP/HTTP logs ingest: decode, then select the records that are errors.

Both wire encodings the OTLP spec defines for `/v1/logs` are supported:
`application/x-protobuf` (binary ExportLogsServiceRequest) and
`application/json` (protobuf JSON mapping). Collectors emit either depending on
the exporter's `encoding` setting, so accepting only one is a deployment trap.

Attribute names follow the OTel semantic conventions for exceptions:
https://opentelemetry.io/docs/specs/semconv/exceptions/exceptions-logs/
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

from .models import SEVERITY_ERROR, ErrorEvent, LogLine

EXCEPTION_TYPE = "exception.type"
EXCEPTION_MESSAGE = "exception.message"
EXCEPTION_STACKTRACE = "exception.stacktrace"
SERVICE_NAME = "service.name"
SERVICE_VERSION = "service.version"

PROTOBUF_CONTENT_TYPES = {"application/x-protobuf", "application/protobuf"}
JSON_CONTENT_TYPES = {"application/json"}


class DecodeError(ValueError):
    """The request body could not be parsed as OTLP."""


# --------------------------------------------------------------------------
# protobuf
# --------------------------------------------------------------------------


def _pb_any_value(value: Any) -> str:
    kind = value.WhichOneof("value")
    if kind is None:
        return ""
    if kind == "string_value":
        return value.string_value
    if kind == "bool_value":
        return "true" if value.bool_value else "false"
    if kind == "int_value":
        return str(value.int_value)
    if kind == "double_value":
        return repr(value.double_value)
    if kind == "bytes_value":
        return value.bytes_value.hex()
    if kind == "array_value":
        return "[" + ", ".join(_pb_any_value(v) for v in value.array_value.values) + "]"
    if kind == "kvlist_value":
        inner = ", ".join(f"{kv.key}={_pb_any_value(kv.value)}" for kv in value.kvlist_value.values)
        return "{" + inner + "}"
    return ""


def _pb_attributes(attributes) -> dict[str, str]:
    return {kv.key: _pb_any_value(kv.value) for kv in attributes}


def decode_protobuf(body: bytes) -> list[tuple[dict[str, str], dict[str, Any]]]:
    request = ExportLogsServiceRequest()
    try:
        request.ParseFromString(body)
    except Exception as exc:  # protobuf raises a variety of types
        raise DecodeError(f"invalid OTLP protobuf body: {exc}") from exc

    records: list[tuple[dict[str, str], dict[str, Any]]] = []
    for resource_logs in request.resource_logs:
        resource_attrs = _pb_attributes(resource_logs.resource.attributes)
        for scope_logs in resource_logs.scope_logs:
            for record in scope_logs.log_records:
                records.append(
                    (
                        resource_attrs,
                        {
                            "attributes": _pb_attributes(record.attributes),
                            "severity_number": int(record.severity_number),
                            "severity_text": record.severity_text,
                            "time_unix_nano": int(record.time_unix_nano)
                            or int(record.observed_time_unix_nano),
                            "body": _pb_any_value(record.body) if record.HasField("body") else "",
                            "trace_id": record.trace_id.hex() or None,
                            "span_id": record.span_id.hex() or None,
                        },
                    )
                )
    return records


# --------------------------------------------------------------------------
# JSON (protobuf JSON mapping — accepts camelCase and snake_case)
# --------------------------------------------------------------------------


def _json_any_value(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, dict):
        return str(value)
    for key in ("stringValue", "string_value"):
        if key in value:
            return str(value[key])
    for key in ("boolValue", "bool_value"):
        if key in value:
            return "true" if value[key] else "false"
    for key in ("intValue", "int_value", "doubleValue", "double_value"):
        if key in value:
            return str(value[key])
    for key in ("bytesValue", "bytes_value"):
        if key in value:
            return str(value[key])
    for key in ("arrayValue", "array_value"):
        if key in value:
            values = (value[key] or {}).get("values", []) or []
            return "[" + ", ".join(_json_any_value(v) for v in values) + "]"
    for key in ("kvlistValue", "kvlist_value"):
        if key in value:
            values = (value[key] or {}).get("values", []) or []
            inner = ", ".join(
                f"{kv.get('key')}={_json_any_value(kv.get('value'))}" for kv in values
            )
            return "{" + inner + "}"
    return ""


def _json_attributes(attributes: list[dict] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in attributes or []:
        key = entry.get("key")
        if key:
            out[str(key)] = _json_any_value(entry.get("value"))
    return out


def _pick(mapping: dict, *names, default=None):
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def decode_json(payload: dict) -> list[tuple[dict[str, str], dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise DecodeError("OTLP JSON body must be an object")

    resource_logs = _pick(payload, "resourceLogs", "resource_logs", default=[]) or []
    records: list[tuple[dict[str, str], dict[str, Any]]] = []
    for resource_log in resource_logs:
        resource = resource_log.get("resource") or {}
        resource_attrs = _json_attributes(resource.get("attributes"))
        scope_logs = _pick(resource_log, "scopeLogs", "scope_logs", default=[]) or []
        for scope_log in scope_logs:
            log_records = _pick(scope_log, "logRecords", "log_records", default=[]) or []
            for record in log_records:
                time_nano = _pick(
                    record,
                    "timeUnixNano",
                    "time_unix_nano",
                    "observedTimeUnixNano",
                    "observed_time_unix_nano",
                    default=0,
                )
                records.append(
                    (
                        resource_attrs,
                        {
                            "attributes": _json_attributes(record.get("attributes")),
                            "severity_number": int(
                                _pick(record, "severityNumber", "severity_number", default=0) or 0
                            ),
                            "severity_text": _pick(
                                record, "severityText", "severity_text", default=""
                            ),
                            "time_unix_nano": int(time_nano or 0),
                            "body": _json_any_value(record.get("body")),
                            "trace_id": _pick(record, "traceId", "trace_id") or None,
                            "span_id": _pick(record, "spanId", "span_id") or None,
                        },
                    )
                )
    return records


# --------------------------------------------------------------------------
# selection + conversion
# --------------------------------------------------------------------------


def is_error(record: dict[str, Any], require_exception: bool = False) -> bool:
    """An error is severity >= ERROR (17), or any record carrying exception.type.

    OTel maps 17-20 to ERROR and 21-24 to FATAL, so `>= 17` covers both.
    """
    has_exception = bool(record["attributes"].get(EXCEPTION_TYPE))
    if require_exception:
        return has_exception
    return has_exception or record["severity_number"] >= SEVERITY_ERROR


def _timestamp(nanos: int) -> datetime:
    if not nanos:
        return datetime.now(UTC)
    return datetime.fromtimestamp(nanos / 1_000_000_000, tz=UTC)


def _derive_type_and_message(record: dict[str, Any]) -> tuple[str, str]:
    attributes = record["attributes"]
    exc_type = attributes.get(EXCEPTION_TYPE, "").strip()
    exc_message = attributes.get(EXCEPTION_MESSAGE, "").strip()
    body = (record.get("body") or "").strip()

    if exc_type:
        return exc_type, exc_message or body
    # A severity-only error with no exception attributes: synthesize a stable
    # type so fingerprinting and the issue title still have something to work
    # with. `Error` is deliberately generic — the message carries the identity.
    return "Error", exc_message or body or "(no message)"


def to_events(
    decoded: list[tuple[dict[str, str], dict[str, Any]]],
    require_exception: bool = False,
) -> tuple[list[ErrorEvent], list[LogLine]]:
    """Split decoded records into error events and correlated context lines.

    Non-error records are not discarded: they are returned as `LogLine`s so the
    context package can show what the service was doing before it failed.
    """
    events: list[ErrorEvent] = []
    lines: list[LogLine] = []

    for resource_attrs, record in decoded:
        service = resource_attrs.get(SERVICE_NAME, "unknown-service")
        timestamp = _timestamp(record["time_unix_nano"])

        if is_error(record, require_exception=require_exception):
            exc_type, exc_message = _derive_type_and_message(record)
            events.append(
                ErrorEvent(
                    service_name=service,
                    service_version=resource_attrs.get(SERVICE_VERSION),
                    exception_type=exc_type,
                    exception_message=exc_message,
                    stacktrace=record["attributes"].get(EXCEPTION_STACKTRACE) or None,
                    trace_id=record["trace_id"],
                    span_id=record["span_id"],
                    timestamp=timestamp,
                    severity_number=record["severity_number"] or SEVERITY_ERROR,
                    body=record.get("body") or None,
                    attributes=dict(record["attributes"]),
                    resource_attributes=dict(resource_attrs),
                )
            )
        else:
            text = (record.get("body") or "").strip()
            if text:
                lines.append(
                    LogLine(
                        timestamp=timestamp,
                        severity=record.get("severity_text")
                        or _severity_label(record["severity_number"]),
                        text=text,
                    )
                )
    return events, lines


def _severity_label(number: int) -> str:
    from .models import severity_name

    return severity_name(number)


def trace_of(decoded_record: dict[str, Any]) -> str | None:
    return decoded_record.get("trace_id")
