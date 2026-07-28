"""Secret redaction, applied before anything reaches GitHub.

PLAN.md §7 leaves redaction to the instrumentation. That is right as policy and
insufficient as a control: everywhere else in a telemetry stack an unredacted
secret lands in a backend behind SSO with a retention limit, whereas here it
lands in a GitHub issue — indexed, emailed to watchers, and on a public repo,
permanently public. See CHALLENGE.md §7.

This is defence in depth. It is deliberately conservative: patterns match token
*shapes* with distinctive prefixes rather than guessing at entropy, so the false
positive rate stays near zero.
"""

from __future__ import annotations

import re

MASK = "[REDACTED]"

# (name, pattern) — order matters only for readability; all are applied.
_BUILTIN: list[tuple[str, str]] = [
    # GitHub tokens: ghp_ (classic PAT), gho_, ghu_, ghs_ (App installation),
    # ghr_ (refresh), github_pat_ (fine-grained).
    ("github_token", r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    ("github_fine_grained", r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    # AWS
    ("aws_access_key", r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
    ("aws_secret", r"(?i)\baws_secret_access_key\b\s*[=:]\s*\S+"),
    # Slack
    ("slack_token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    # Anthropic / OpenAI style
    ("anthropic_key", r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    ("openai_key", r"\bsk-[A-Za-z0-9]{32,}\b"),
    # Google
    ("google_api_key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    # Stripe
    ("stripe_key", r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    # Bearer / Authorization headers
    # The value must run to a delimiter, not to the first space: an
    # "Authorization: Bearer <token>" value contains a space, and stopping at it
    # masks the word "Bearer" while leaking the token that follows it.
    ("bearer", r"(?i)\b(?:authorization|proxy-authorization)\b\s*[=:]\s*[^\n,;}\]\"']+"),
    ("bearer_inline", r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    ("basic_auth", r"(?i)\bbasic\s+[A-Za-z0-9+/]{16,}={0,2}"),
    # JWTs (three dot-separated base64url segments, header starts with eyJ)
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    # Passwords inside connection strings: scheme://user:secret@host.
    # Two groups so the substitution replaces the password *span*. Matching the
    # password as text and replacing it inside the match would mask the first
    # occurrence in the whole match — which is the username whenever the two are
    # equal, leaking the password. `postgres:postgres` and `default:default` are
    # exactly the shapes that show up in a DSN inside a connection error.
    ("conn_string_password", r"(://[^\s:/@]+:)([^\s@/]+)@"),
    # PEM private keys — collapse the whole block
    (
        "private_key_block",
        r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----",
    ),
    # Generic assignments: api_key=..., secret: ..., password = "...", token=...
    (
        "generic_assignment",
        r"(?i)\b(?:api[_-]?key|secret[_-]?key|secret|password|passwd|pwd|access[_-]?token"
        r"|auth[_-]?token|client[_-]?secret|private[_-]?key)\b"
        r"\s*[=:]\s*(?:\"[^\"]{4,}\"|'[^']{4,}'|[^\s,;)}\]]{4,})",
    ),
]

_FLAGS = re.DOTALL

# Rules that keep part of what they match. The value is an `re.sub` template, so
# the replacement is positional — it swaps a span, never a substring looked up by
# its text. Anything not listed here is replaced wholesale with MASK.
_TEMPLATES: dict[str, str] = {
    # Keep the URL shape (scheme, user, host); mask only the password.
    "conn_string_password": rf"\g<1>{MASK}@",
}


class Redactor:
    """Callable that masks secrets in free text.

    `Redactor(enabled=False)` is the identity function, so callers never need
    to branch on configuration.
    """

    def __init__(self, enabled: bool = True, extra_patterns: list[str] | None = None):
        self.enabled = enabled
        self._rules: list[tuple[str, re.Pattern[str]]] = []
        if enabled:
            for name, pattern in _BUILTIN:
                self._rules.append((name, re.compile(pattern, _FLAGS)))
            for index, pattern in enumerate(extra_patterns or []):
                try:
                    self._rules.append((f"custom_{index}", re.compile(pattern, _FLAGS)))
                except re.error as exc:  # a bad custom pattern must not break ingest
                    raise ValueError(
                        f"invalid E2I_REDACT_EXTRA_PATTERNS entry {pattern!r}"
                    ) from exc

    def __call__(self, text: str | None) -> str:
        if text is None:
            return ""
        if not self.enabled or not text:
            return text or ""
        out = text
        for name, rule in self._rules:
            out = rule.sub(_TEMPLATES.get(name, MASK), out)
        return out

    def scan(self, text: str) -> list[str]:
        """Return the names of rules that fire. Used by tests and diagnostics."""
        return [name for name, rule in self._rules if rule.search(text)]
