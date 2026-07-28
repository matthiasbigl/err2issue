"""GitHub credentials: PAT or GitHub App installation tokens.

App auth is what makes org-wide deployment work (CHALLENGE.md §3). One App
installed on an org can file into every repository it is granted, and its rate
limit scales with the installation (5,000/hr floor, up to 12,500/hr) instead of
being a fixed per-user PAT budget.

Installation tokens expire after one hour, so they are cached per installation
and refreshed early.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx
import jwt

# Refresh this long before actual expiry, so an in-flight request never races
# the boundary.
REFRESH_MARGIN_SECONDS = 300
JWT_LIFETIME_SECONDS = 540  # GitHub rejects App JWTs with exp > 10 minutes out.


class AuthError(RuntimeError):
    pass


class TokenProvider(ABC):
    """Supplies an Authorization header value for a given repository."""

    @abstractmethod
    async def token_for(self, repo: str) -> str: ...

    @property
    def scheme(self) -> str:
        return "Bearer"

    async def headers_for(self, repo: str) -> dict[str, str]:
        return {"Authorization": f"{self.scheme} {await self.token_for(repo)}"}

    async def aclose(self) -> None:  # pragma: no cover - trivial
        return None

    def describe(self) -> str:
        return self.__class__.__name__


class StaticTokenProvider(TokenProvider):
    """Personal access token — classic or fine-grained. Same token everywhere."""

    def __init__(self, token: str):
        if not token:
            raise AuthError("empty GitHub token")
        self._token = token

    async def token_for(self, repo: str) -> str:
        return self._token

    def describe(self) -> str:
        return "pat"


@dataclass
class _CachedToken:
    value: str
    expires_at: float

    @property
    def usable(self) -> bool:
        return time.time() < (self.expires_at - REFRESH_MARGIN_SECONDS)


class AppTokenProvider(TokenProvider):
    """GitHub App: sign a JWT, exchange it for a per-installation token."""

    def __init__(
        self,
        app_id: str,
        private_key: str,
        api_url: str,
        installation_id: int | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        if not app_id or not private_key:
            raise AuthError("GitHub App auth needs both an app id and a private key")
        self.app_id = str(app_id)
        self.private_key = private_key
        self.api_url = api_url.rstrip("/")
        self.fixed_installation_id = installation_id
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None
        self._tokens: dict[int, _CachedToken] = {}
        self._installations: dict[str, int] = {}

    # -- JWT ---------------------------------------------------------------

    def app_jwt(self) -> str:
        now = int(time.time())
        payload = {
            # Backdate by 60s to tolerate clock skew between us and GitHub.
            "iat": now - 60,
            "exp": now + JWT_LIFETIME_SECONDS,
            "iss": self.app_id,
        }
        try:
            return jwt.encode(payload, self.private_key, algorithm="RS256")
        except Exception as exc:
            raise AuthError(f"could not sign GitHub App JWT: {exc}") from exc

    # -- installation resolution ------------------------------------------

    async def _resolve_installation(self, repo: str) -> int:
        if self.fixed_installation_id:
            return self.fixed_installation_id
        if repo in self._installations:
            return self._installations[repo]

        owner, _, name = repo.partition("/")
        response = await self._client.get(
            f"{self.api_url}/repos/{owner}/{name}/installation",
            headers={
                "Authorization": f"Bearer {self.app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if response.status_code == 404:
            raise AuthError(
                f"the GitHub App is not installed on {repo!r} "
                "(or lacks access to it) — install it and grant Issues: write"
            )
        if response.status_code >= 400:
            raise AuthError(
                f"could not resolve installation for {repo!r}: "
                f"{response.status_code} {response.text[:200]}"
            )
        installation_id = int(response.json()["id"])
        self._installations[repo] = installation_id
        return installation_id

    # -- token minting -----------------------------------------------------

    async def token_for(self, repo: str) -> str:
        installation_id = await self._resolve_installation(repo)
        cached = self._tokens.get(installation_id)
        if cached and cached.usable:
            return cached.value

        response = await self._client.post(
            f"{self.api_url}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {self.app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if response.status_code >= 400:
            raise AuthError(
                f"could not mint installation token for {installation_id}: "
                f"{response.status_code} {response.text[:200]}"
            )
        payload = response.json()
        # "2026-07-28T12:00:00Z" -> epoch seconds; fall back to one hour.
        expires_at = time.time() + 3600.0
        raw_expiry = payload.get("expires_at")
        if raw_expiry:
            from datetime import datetime

            try:
                expires_at = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        token = payload["token"]
        self._tokens[installation_id] = _CachedToken(value=token, expires_at=expires_at)
        return token

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def describe(self) -> str:
        return f"github-app:{self.app_id}"


def build_provider(settings, client: httpx.AsyncClient | None = None) -> TokenProvider | None:
    """Construct the provider implied by settings, or None when unauthenticated."""
    mode = settings.auth_mode
    if mode == "app":
        return AppTokenProvider(
            app_id=settings.github_app_id,
            private_key=settings.resolved_private_key(),
            api_url=settings.github_api_url,
            installation_id=settings.github_app_installation_id,
            client=client,
        )
    if mode == "pat":
        return StaticTokenProvider(settings.github_token)
    return None
