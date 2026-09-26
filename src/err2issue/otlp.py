"""OTLP/HTTP logs ingest: decode, then select the records that are errors.

Both wire encodings the OTLP spec defines for `/v1/logs` are supported:
`application/x-protobuf` (binary ExportLogsServiceRequest) and
`application/json` (protobuf JSON mapping). Collectors emit either depending on
the exporter's `encoding` setting, so accepting only one is a deployment trap.

Attribute names follow the OTel semantic conventions for exceptions:
https://opentelemetry.io/docs/specs/semconv/exceptions/exceptions-logs/
"""

from __future__ import annotations

import base64
import binascii
import re
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
            scope_name = scope_logs.scope.name or None
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
                            "trace_id": _id_from_bytes(record.trace_id, TRACE_ID_BYTES),
                            "span_id": _id_from_bytes(record.span_id, SPAN_ID_BYTES),
                            "scope_name": scope_name,
                        },
                    )
                )
    return records


# --------------------------------------------------------------------------
# JSON (protobuf JSON mapping — accepts camelCase and snake_case)
#
# JSON arrives from anything that can speak HTTP, not just collectors, so every
# shape is checked rather than assumed: a list where an object belongs, a null
# record, or a non-numeric timestamp skips or zeroes that one field instead of
# raising into /v1/logs and 500ing the whole batch.
# --------------------------------------------------------------------------


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _dicts(value: Any) -> list[dict]:
    return [item for item in _list(value) if isinstance(item, dict)]


def _json_values(container: Any) -> list:
    return _list(container.get("values")) if isinstance(container, dict) else []


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
            values = _json_values(value[key])
            return "[" + ", ".join(_json_any_value(v) for v in values) + "]"
    for key in ("kvlistValue", "kvlist_value"):
        if key in value:
            values = _dicts(_json_values(value[key]))
            inner = ", ".join(
                f"{kv.get('key')}={_json_any_value(kv.get('value'))}" for kv in values
            )
            return "{" + inner + "}"
    return ""


def _json_attributes(attributes: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in _dicts(attributes):
        key = entry.get("key")
        if key:
            out[str(key)] = _json_any_value(entry.get("value"))
    return out


def _pick(mapping: dict, *names, default=None):
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _json_int(value: Any) -> int:
    """int64/uint64 fields are strings in the JSON mapping; tolerate junk as 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == value and abs(value) != float("inf") else 0
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


def _json_severity(value: Any) -> int:
    """OTLP/JSON says enums are integers, but stock protobuf JSON encoders emit
    the enum *name* (`"SEVERITY_NUMBER_ERROR"`), and so does anything built on
    `MessageToJson` defaults. Accept both."""
    if isinstance(value, str) and value.strip().upper().startswith("SEVERITY_NUMBER_"):
        return _SEVERITY_ENUM_NAMES.get(value.strip().upper(), 0)
    return max(_json_int(value), 0)


_SEVERITY_ENUM_NAMES = {
    "SEVERITY_NUMBER_UNSPECIFIED": 0,
    **{
        f"SEVERITY_NUMBER_{name}{suffix}": base + offset
        for name, base in (
            ("TRACE", 1),
            ("DEBUG", 5),
            ("INFO", 9),
            ("WARN", 13),
            ("ERROR", 17),
            ("FATAL", 21),
        )
        for offset, suffix in enumerate(("", "2", "3", "4"))
    },
}


def decode_json(payload: dict) -> list[tuple[dict[str, str], dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise DecodeError("OTLP JSON body must be an object")

    resource_logs = _dicts(_pick(payload, "resourceLogs", "resource_logs", default=[]))
    records: list[tuple[dict[str, str], dict[str, Any]]] = []
    for resource_log in resource_logs:
        resource = resource_log.get("resource")
        resource_attrs = _json_attributes(
            resource.get("attributes") if isinstance(resource, dict) else None
        )
        scope_logs = _dicts(_pick(resource_log, "scopeLogs", "scope_logs", default=[]))
        for scope_log in scope_logs:
            scope = scope_log.get("scope")
            scope_name = scope.get("name") if isinstance(scope, dict) else None
            scope_name = scope_name if isinstance(scope_name, str) and scope_name else None
            log_records = _dicts(_pick(scope_log, "logRecords", "log_records", default=[]))
            for record in log_records:
                time_nano = _json_int(
                    _pick(record, "timeUnixNano", "time_unix_nano", default=0)
                ) or _json_int(
                    _pick(record, "observedTimeUnixNano", "observed_time_unix_nano", default=0)
                )
                severity_text = _pick(record, "severityText", "severity_text", default="")
                records.append(
                    (
                        resource_attrs,
                        {
                            "attributes": _json_attributes(record.get("attributes")),
                            "severity_number": _json_severity(
                                _pick(record, "severityNumber", "severity_number", default=0)
                            ),
                            "severity_text": severity_text
                            if isinstance(severity_text, str)
                            else "",
                            "time_unix_nano": time_nano,
                            "body": _json_any_value(record.get("body")),
                            "trace_id": _id_from_json(
                                _pick(record, "traceId", "trace_id"), TRACE_ID_BYTES
                            ),
                            "span_id": _id_from_json(
                                _pick(record, "spanId", "span_id"), SPAN_ID_BYTES
                            ),
                            "scope_name": scope_name,
                        },
                    )
                )
    return records


# --------------------------------------------------------------------------
# trace / span ids
#
# Ids are the join key for the trace ring buffer and the text of the issue's
# trace link, so they are normalized to one canonical form: lowercase hex of the
# right length. OTLP/JSON specifies hex, but uppercase hex and the base64 that a
# plain protobuf JSON encoder produces both occur in the wild. An all-zero id is
# the spec's "invalid / not set" value and several SDKs send it for records
# outside a span; kept verbatim it became one shared trace that stitched every
# span-less log line into every span-less error's "correlated" context.
# --------------------------------------------------------------------------

TRACE_ID_BYTES = 16
SPAN_ID_BYTES = 8
_HEX_RE = re.compile(r"[0-9a-f]+")


def _id_from_bytes(raw: bytes, size: int) -> str | None:
    if len(raw) != size or not any(raw):
        return None
    return raw.hex()


def _id_from_json(value: Any, size: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    lowered = text.lower()
    if len(lowered) == size * 2 and _HEX_RE.fullmatch(lowered):
        return lowered if lowered.strip("0") else None
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    return _id_from_bytes(raw, size)


# --------------------------------------------------------------------------
# selection + conversion
# --------------------------------------------------------------------------


# severity_text -> number, consulted only when severity_number is 0
# (UNSPECIFIED). Several log bridges and hand-rolled exporters set only the
# text, and syslog/JUL/Python level names leak through unmapped; reading the
# number alone silently dropped every one of those errors.
_SEVERITY_TEXT = {
    **{f"ERROR{n}": SEVERITY_ERROR + i for i, n in enumerate(("", "2", "3", "4"))},
    **{f"FATAL{n}": 21 + i for i, n in enumerate(("", "2", "3", "4"))},
    "ERR": SEVERITY_ERROR,
    "SEVERE": SEVERITY_ERROR,  # java.util.logging
    "CRITICAL": 21,  # Python
    "CRIT": 21,
    "ALERT": 22,
    "EMERG": 23,
    "EMERGENCY": 23,
    "PANIC": 23,
}


def effective_severity(record: dict[str, Any]) -> int:
    """The record's severity number, derived from its text when unspecified."""
    number = record.get("severity_number") or 0
    if number:
        return number
    text = record.get("severity_text")
    if not isinstance(text, str):
        return 0
    return _SEVERITY_TEXT.get(text.strip().upper(), 0)


def has_exception(record: dict[str, Any]) -> bool:
    """Semconv requires exception.type *or* exception.message, not both."""
    attributes = record["attributes"]
    return bool(attributes.get(EXCEPTION_TYPE) or attributes.get(EXCEPTION_MESSAGE, "").strip())


def is_error(record: dict[str, Any], require_exception: bool = False) -> bool:
    """An error is severity >= ERROR (17), or any record carrying exception attributes.

    OTel maps 17-20 to ERROR and 21-24 to FATAL, so `>= 17` covers both. An
    unspecified (0) number falls back to the severity text.
    """
    if has_exception(record):
        return True
    return not require_exception and effective_severity(record) >= SEVERITY_ERROR


def _timestamp(nanos: int) -> datetime:
    if not nanos or nanos < 0:
        return datetime.now(UTC)
    try:
        return datetime.fromtimestamp(nanos / 1_000_000_000, tz=UTC)
    except (OverflowError, OSError, ValueError):  # past year 9999: a unit mix-up
        return datetime.now(UTC)


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
                    severity_number=effective_severity(record) or SEVERITY_ERROR,
                    body=record.get("body") or None,
                    logger_name=record.get("scope_name"),
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
