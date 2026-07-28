"""Fingerprinting — the versioned identity contract for an error.

The fingerprint decides what "the same bug" means. It must be stable across
releases (line numbers move, build paths change, addresses differ every run)
and distinct across genuinely different bugs.

The rules here are **versioned and frozen**. Changing them changes the identity
of every error in the world, so a change ships as a new VERSION and old issues
stay addressable under the old one. See docs/FINGERPRINT.md.
"""

from __future__ import annotations

import hashlib
import re

VERSION = "v1"
DIGEST_CHARS = 12

# Lines that introduce a stack but are not themselves frames.
_PREAMBLE = re.compile(
    r"""^(
        traceback\s*\(most\ recent\ call\ last\)|
        stack\ trace|
        exception\ in\ thread|
        caused\ by|
        the\ above\ exception|
        during\ handling\ of|
        goroutine\ \d+|
        \.{3}\ \d+\ more
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Markers that identify a line as a genuine stack frame, across ecosystems.
_FRAME_MARKERS = (
    re.compile(r'^\s*File\s+"'),  # Python
    re.compile(r"^\s*at\s+\S"),  # Java / JS / .NET
    re.compile(r"^\s*from\s+\S+:\d+"),  # Ruby "from"
    re.compile(r":\d+:in\s"),  # Ruby
    re.compile(r"^\s*#\d+\s+"),  # PHP / gdb
    re.compile(r"\.go:\d+"),  # Go
    re.compile(r"^\s*\S+\([^)]*\)\s*$"),  # Go func line, C++ symbol
)

_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)
_HEX_ADDR = re.compile(r"\b0[xX][0-9a-fA-F]+\b")
_LINE_COL = re.compile(r":\d+:\d+\b")
_LINE_WORD = re.compile(r"\bline\s+\d+", re.IGNORECASE)
_LINE_NUM = re.compile(r":\d+\b")
_LONG_HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
# Deliberately not anchored on \b at the end: durations and sizes carry unit
# suffixes ("3000ms", "512MB") and a trailing word boundary would never match
# them, letting per-occurrence values leak into the fingerprint.
_LONG_NUM = re.compile(r"\d{3,}")
_PATHISH = re.compile(r"[A-Za-z]:\\[^\s'\"()]+|/[^\s'\"()]*/[^\s'\"()]*|\\[^\s'\"()]+")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_WS = re.compile(r"\s+")


def _shorten_path(match: re.Match[str]) -> str:
    """Keep the last two path segments; drop machine/build-specific prefixes.

    /build/7f3a91/src/app/handler.py -> app/handler.py
    """
    token = match.group(0).replace("\\", "/")
    parts = [p for p in token.split("/") if p]
    return "/".join(parts[-2:]) if parts else token


def normalize_frame(frame: str) -> str:
    """Strip everything that varies between runs, machines, and releases."""
    out = frame.strip()
    out = _PATHISH.sub(_shorten_path, out)
    out = _HEX_ADDR.sub("0xADDR", out)
    out = _UUID.sub("UUID", out)
    out = _LINE_COL.sub(":L:C", out)
    out = _LINE_WORD.sub("line L", out)
    out = _LINE_NUM.sub(":L", out)
    out = _LONG_HEX.sub("HEX", out)
    out = _LONG_NUM.sub("N", out)
    return _WS.sub(" ", out).strip()


def top_frame(stacktrace: str | None) -> str | None:
    """Return the first genuine stack frame line, or None if there is no stack."""
    if not stacktrace:
        return None
    candidates = []
    for raw in stacktrace.splitlines():
        line = raw.strip()
        if not line or _PREAMBLE.match(line):
            continue
        candidates.append(raw)
    for raw in candidates:
        if any(marker.search(raw) for marker in _FRAME_MARKERS):
            return raw.strip()
    # No recognizable frame syntax: fall back to the first substantive line,
    # but only if it is not simply the exception header repeated.
    return candidates[0].strip() if candidates else None


def normalize_message(message: str) -> str:
    """Reduce a message to its invariant shape (used when there is no stack).

    Quoted substrings, ids, and numbers are the parts that differ between two
    occurrences of the same bug, so they are replaced rather than hashed.
    """
    out = _PATHISH.sub(_shorten_path, message.strip())
    out = _QUOTED.sub("'X'", out)
    out = _UUID.sub("UUID", out)
    out = _HEX_ADDR.sub("0xADDR", out)
    out = _LONG_HEX.sub("HEX", out)
    out = _LONG_NUM.sub("N", out)
    return _WS.sub(" ", out).strip()


def fingerprint_parts(event) -> tuple[str, str, str]:
    """The three inputs to the digest, exposed for docs, tests, and debugging."""
    frame = top_frame(event.stacktrace)
    if frame is not None:
        site = "frame:" + normalize_frame(frame)
    else:
        site = "message:" + normalize_message(event.exception_message or event.body or "")
    return (event.service_name or "", event.exception_type or "", site)


def compute(event) -> str:
    """Return the 12-hex-character fingerprint for an ErrorEvent."""
    payload = "\x1f".join(fingerprint_parts(event))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest[:DIGEST_CHARS]


def label_for(fingerprint: str, version: str = VERSION) -> str:
    """The GitHub label that carries this fingerprint.

    Length budget: GitHub caps label names at 50 characters.
    "err2issue-fp-" (13) + "v1-" (3) + 12 = 28. Room for v10+ and a longer digest.
    """
    return f"err2issue-fp-{version}-{fingerprint}"


_LABEL_RE = re.compile(rf"^err2issue-fp-(v\d+)-([0-9a-f]{{{DIGEST_CHARS}}})$")


def parse_label(label: str) -> tuple[str, str] | None:
    """Inverse of `label_for`. Returns (version, fingerprint) or None."""
    match = _LABEL_RE.match(label.strip())
    return (match.group(1), match.group(2)) if match else None
