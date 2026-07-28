"""Route a service to the repository that owns it.

PLAN.md §3 scoped v1 to a single repository. One OTel Collector fans out errors
for every service an org runs, so a single-repo receiver means one deployment
per repo, each with its own credentials and its own private view of the
suppression state (CHALLENGE.md §3). Routing makes one deployment serve an org.

Rules are evaluated most-specific-first:
  1. exact match on service.name
  2. glob match, longest literal prefix wins (so `cart-api` beats `cart-*`)
  3. default repo
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


class RoutingError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    pattern: str
    repo: str

    @property
    def is_glob(self) -> bool:
        return any(ch in self.pattern for ch in "*?[")

    @property
    def specificity(self) -> int:
        """Length of the literal prefix before the first wildcard."""
        for index, char in enumerate(self.pattern):
            if char in "*?[":
                return index
        return len(self.pattern)


class Router:
    def __init__(self, rules: list[Rule] | None = None, default_repo: str | None = None):
        self.rules = rules or []
        self.default_repo = default_repo
        for rule in self.rules:
            _validate_repo(rule.repo)
        if default_repo:
            _validate_repo(default_repo)
        # Exact rules first, then globs by descending specificity.
        self._exact = {r.pattern: r.repo for r in self.rules if not r.is_glob}
        self._globs = sorted(
            (r for r in self.rules if r.is_glob), key=lambda r: r.specificity, reverse=True
        )

    @classmethod
    def from_spec(cls, spec: str, default_repo: str | None = None) -> Router:
        """Parse `svc=owner/repo,other-*=owner/other` into a Router."""
        rules: list[Rule] = []
        for chunk in spec.split(","):
            entry = chunk.strip()
            if not entry:
                continue
            if "=" not in entry:
                raise RoutingError(f"route entry {entry!r} must be 'service-pattern=owner/repo'")
            pattern, repo = entry.split("=", 1)
            pattern, repo = pattern.strip(), repo.strip()
            if not pattern:
                raise RoutingError(f"route entry {entry!r} has an empty service pattern")
            rules.append(Rule(pattern=pattern, repo=repo))
        return cls(rules=rules, default_repo=default_repo)

    def resolve(self, service_name: str) -> str | None:
        """Return `owner/repo` for a service, or None if unroutable."""
        name = (service_name or "").strip()
        if name and name in self._exact:
            return self._exact[name]
        if name:
            for rule in self._globs:
                if fnmatch.fnmatchcase(name, rule.pattern):
                    return rule.repo
        return self.default_repo

    def describe(self) -> dict[str, object]:
        return {
            "exact": dict(self._exact),
            "globs": [{"pattern": r.pattern, "repo": r.repo} for r in self._globs],
            "default": self.default_repo,
        }


def _validate_repo(repo: str) -> None:
    if not _REPO_RE.match(repo):
        raise RoutingError(f"{repo!r} is not a valid 'owner/repo'")
