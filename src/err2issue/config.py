"""Configuration. Environment variables only — no config files, no flags.

Everything is prefixed `E2I_`. See .env.example for a documented template.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GITHUB_COM_API = "https://api.github.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="E2I_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- server ----
    host: str = "0.0.0.0"  # noqa: S104 - containers bind all interfaces by design
    port: int = 4318  # the OTLP/HTTP default, so collectors need no port override
    log_level: str = "INFO"

    # ---- GitHub connection ----
    github_api_url: str = GITHUB_COM_API
    """Override for GitHub Enterprise Server (https://ghes.example.com/api/v3)
    or GHE.com data residency (https://api.SUBDOMAIN.ghe.com)."""

    github_token: str | None = None
    """A classic or fine-grained PAT. Mutually exclusive with App auth."""

    github_app_id: str | None = None
    github_app_private_key: str | None = None
    """PEM contents. Use `github_app_private_key_file` to read from disk instead."""
    github_app_private_key_file: str | None = None
    github_app_installation_id: int | None = None
    """Optional. If unset, the installation is resolved per-repository."""

    # ---- routing ----
    github_repo: str | None = None
    """Default `owner/repo` used when no routing rule matches."""
    route_map: str = ""
    """`service.name=owner/repo` pairs, comma-separated.
    Supports glob patterns: `cart-*=acme/cart,*-worker=acme/workers`."""
    drop_unrouted: bool = True
    """If no rule matches and there is no default repo, drop rather than error."""

    # ---- filing behaviour ----
    sink: str = "github"
    """`github` (direct REST), `workflow` (workflow_dispatch), or `dry-run`."""
    workflow_file: str = "file-error-issue.yml"
    workflow_ref: str = "main"
    issue_labels: str = "err2issue"
    """Extra labels applied to every created issue, comma-separated."""
    reopen_closed: bool = True
    """Reopen a closed issue when its error recurs (regression detection)."""
    max_comment_per_issue_per_hour: int = 4
    """Cap occurrence comments so a long-running error does not spam one issue."""
    repo_unavailable_cooldown_seconds: int = 900
    """How long a repo that answered 404/410 is skipped before being probed again.
    404 is usually permanent, but a rotated token looks identical — without an
    expiry that is indefinite silent data loss on a pod reporting Ready."""

    # ---- noise control ----
    suppress_window_seconds: int = 600
    """At most one filing per fingerprint per window, per replica."""
    max_dispatches_per_minute: int = 30
    """Global ceiling across all fingerprints."""
    max_new_fingerprints_per_day: int = 50
    """Breach protection for a bad deploy. Beyond this, new errors are dropped."""
    trace_buffer_size: int = 500
    """Ring-buffer entries for correlated-log lookup, keyed by trace id."""

    # ---- content limits ----
    max_message_chars: int = 2000
    max_stacktrace_chars: int = 6000
    max_context_log_lines: int = 20

    # ---- redaction ----
    redact: bool = True
    redact_extra_patterns: str = ""
    """Additional regexes, comma-separated. Matches are replaced wholesale."""

    # ---- AI enrichment ----
    ai_enabled: bool = True
    anthropic_api_key: str | None = None
    ai_model: str = "claude-opus-5"
    ai_timeout_seconds: float = 20.0

    # ---- OTLP ingest ----
    require_exception_attributes: bool = False
    """If True, only records carrying `exception.type` are considered errors;
    severity alone is not enough."""

    @field_validator("sink")
    @classmethod
    def _valid_sink(cls, value: str) -> str:
        allowed = {"github", "workflow", "dry-run"}
        if value not in allowed:
            raise ValueError(f"sink must be one of {sorted(allowed)}, got {value!r}")
        return value

    @field_validator("github_api_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    # ---- derived ----

    @property
    def extra_labels(self) -> list[str]:
        return [x.strip() for x in self.issue_labels.split(",") if x.strip()]

    @property
    def redact_patterns(self) -> list[str]:
        return [x.strip() for x in self.redact_extra_patterns.split(",") if x.strip()]

    def resolved_private_key(self) -> str | None:
        if self.github_app_private_key:
            return self.github_app_private_key.replace("\\n", "\n")
        if self.github_app_private_key_file:
            with open(self.github_app_private_key_file) as handle:
                return handle.read()
        return None

    @property
    def auth_mode(self) -> str:
        """`app`, `pat`, or `none`. App wins if both are configured."""
        if self.github_app_id and (self.github_app_private_key or self.github_app_private_key_file):
            return "app"
        if self.github_token:
            return "pat"
        return "none"

    def validation_errors(self) -> list[str]:
        """Every check that can be made without touching the network.

        Fail-fast policy: a misconfigured deployment must refuse to start rather
        than accept telemetry it will silently drop. Anything detectable from
        configuration alone is detected here, at startup, not on first error.
        """
        problems: list[str] = []

        if self.sink != "dry-run" and self.auth_mode == "none":
            problems.append(
                "no GitHub credentials: set E2I_GITHUB_TOKEN, or "
                "E2I_GITHUB_APP_ID + E2I_GITHUB_APP_PRIVATE_KEY"
            )

        if not self.github_repo and not self.route_map and self.sink != "dry-run":
            problems.append("no routing: set E2I_GITHUB_REPO and/or E2I_ROUTE_MAP")

        # Parse the routing table now — a typo here would otherwise surface as a
        # dropped error hours later, under load.
        try:
            from .routing import Router

            Router.from_spec(self.route_map, self.github_repo)
        except Exception as exc:
            problems.append(f"E2I_ROUTE_MAP/E2I_GITHUB_REPO invalid: {exc}")

        # Load and parse the App private key now rather than on first filing.
        if self.auth_mode == "app":
            try:
                key = self.resolved_private_key()
            except OSError as exc:
                problems.append(f"E2I_GITHUB_APP_PRIVATE_KEY_FILE unreadable: {exc}")
            else:
                if not key or "PRIVATE KEY" not in key:
                    problems.append(
                        "E2I_GITHUB_APP_PRIVATE_KEY does not look like a PEM private key"
                    )
                else:
                    try:
                        import jwt

                        jwt.encode({"iat": 0, "exp": 1, "iss": "0"}, key, algorithm="RS256")
                    except Exception as exc:
                        problems.append(f"GitHub App private key cannot sign RS256: {exc}")

        # Compile custom redaction patterns now — a bad regex must not be
        # discovered mid-pipeline, where the failure would look like data loss.
        if self.redact and self.redact_patterns:
            import re

            for pattern in self.redact_patterns:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    problems.append(f"E2I_REDACT_EXTRA_PATTERNS entry {pattern!r} invalid: {exc}")

        if (
            self.workflow_file
            and self.sink == "workflow"
            and not self.workflow_file.endswith((".yml", ".yaml"))
        ):
            problems.append(
                f"E2I_WORKFLOW_FILE should be a workflow filename ending in .yml, "
                f"got {self.workflow_file!r}"
            )

        for name, value in (
            ("E2I_SUPPRESS_WINDOW_SECONDS", self.suppress_window_seconds),
            ("E2I_MAX_DISPATCHES_PER_MINUTE", self.max_dispatches_per_minute),
            ("E2I_MAX_NEW_FINGERPRINTS_PER_DAY", self.max_new_fingerprints_per_day),
            ("E2I_TRACE_BUFFER_SIZE", self.trace_buffer_size),
            ("E2I_REPO_UNAVAILABLE_COOLDOWN_SECONDS", self.repo_unavailable_cooldown_seconds),
            ("E2I_PORT", self.port),
        ):
            if value <= 0:
                problems.append(f"{name} must be positive, got {value}")

        return problems


_settings: Settings | None = None


def get_settings(**overrides) -> Settings:
    """Process-wide settings singleton. Pass overrides in tests."""
    global _settings
    if overrides:
        return Settings(**overrides)
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None
