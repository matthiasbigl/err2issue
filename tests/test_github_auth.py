"""Credentials: PAT and GitHub App installation tokens."""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from err2issue.config import Settings
from err2issue.github.auth import (
    AppTokenProvider,
    AuthError,
    StaticTokenProvider,
    build_provider,
)

API = "https://api.github.com"


@pytest.fixture(scope="module")
def rsa_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


# -- PAT -------------------------------------------------------------------


async def test_static_provider_returns_the_same_token_for_every_repo():
    provider = StaticTokenProvider("ghp_test")
    assert await provider.token_for("acme/a") == "ghp_test"
    assert await provider.token_for("acme/b") == "ghp_test"


async def test_static_provider_builds_a_bearer_header():
    headers = await StaticTokenProvider("ghp_test").headers_for("acme/a")
    assert headers["Authorization"] == "Bearer ghp_test"


def test_empty_pat_is_rejected():
    with pytest.raises(AuthError):
        StaticTokenProvider("")


# -- App JWT ---------------------------------------------------------------


def test_app_jwt_is_signed_rs256_with_the_expected_claims(rsa_key):
    provider = AppTokenProvider("12345", rsa_key, API)
    token = provider.app_jwt()
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256"
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == "12345"
    # Backdated to tolerate clock skew, and inside GitHub's 10-minute ceiling.
    assert claims["iat"] < time.time()
    assert 0 < claims["exp"] - time.time() <= 600


def test_app_auth_requires_both_id_and_key(rsa_key):
    with pytest.raises(AuthError):
        AppTokenProvider("", rsa_key, API)
    with pytest.raises(AuthError):
        AppTokenProvider("123", "", API)


def test_malformed_private_key_fails_with_a_clear_message():
    provider = AppTokenProvider("123", "-----BEGIN PRIVATE KEY-----\nnope\n", API)
    with pytest.raises(AuthError, match="sign"):
        provider.app_jwt()


# -- installation tokens ---------------------------------------------------


@respx.mock
async def test_installation_token_is_minted_and_reused(rsa_key):
    respx.get(f"{API}/repos/acme/api/installation").mock(
        return_value=httpx.Response(200, json={"id": 999})
    )
    route = respx.post(f"{API}/app/installations/999/access_tokens").mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_minted", "expires_at": "2099-01-01T00:00:00Z"}
        )
    )
    async with httpx.AsyncClient() as client:
        provider = AppTokenProvider("123", rsa_key, API, client=client)
        assert await provider.token_for("acme/api") == "ghs_minted"
        assert await provider.token_for("acme/api") == "ghs_minted"
    assert route.call_count == 1, "a cached, unexpired token must not be re-minted"


@respx.mock
async def test_expiring_token_is_refreshed(rsa_key):
    respx.get(f"{API}/repos/acme/api/installation").mock(
        return_value=httpx.Response(200, json={"id": 999})
    )
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60))
    route = respx.post(f"{API}/app/installations/999/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_short", "expires_at": soon})
    )
    async with httpx.AsyncClient() as client:
        provider = AppTokenProvider("123", rsa_key, API, client=client)
        await provider.token_for("acme/api")
        await provider.token_for("acme/api")
    # Inside the refresh margin, so the second call re-mints rather than
    # handing back a token that may expire mid-flight.
    assert route.call_count == 2


@respx.mock
async def test_installation_lookup_is_cached_per_repo(rsa_key):
    lookup = respx.get(f"{API}/repos/acme/api/installation").mock(
        return_value=httpx.Response(200, json={"id": 999})
    )
    respx.post(f"{API}/app/installations/999/access_tokens").mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}
        )
    )
    async with httpx.AsyncClient() as client:
        provider = AppTokenProvider("123", rsa_key, API, client=client)
        await provider.token_for("acme/api")
        await provider.token_for("acme/api")
    assert lookup.call_count == 1


@respx.mock
async def test_fixed_installation_id_skips_the_lookup(rsa_key):
    lookup = respx.get(f"{API}/repos/acme/api/installation")
    respx.post(f"{API}/app/installations/42/access_tokens").mock(
        return_value=httpx.Response(
            201, json={"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}
        )
    )
    async with httpx.AsyncClient() as client:
        provider = AppTokenProvider("123", rsa_key, API, installation_id=42, client=client)
        assert await provider.token_for("acme/api") == "ghs_x"
    assert lookup.call_count == 0


@respx.mock
async def test_app_not_installed_gives_an_actionable_error(rsa_key):
    respx.get(f"{API}/repos/acme/api/installation").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        provider = AppTokenProvider("123", rsa_key, API, client=client)
        with pytest.raises(AuthError, match="not installed"):
            await provider.token_for("acme/api")


@respx.mock
async def test_different_repos_can_resolve_to_different_installations(rsa_key):
    """The org-wide case: one App, many installations."""
    respx.get(f"{API}/repos/acme/a/installation").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )
    respx.get(f"{API}/repos/other/b/installation").mock(
        return_value=httpx.Response(200, json={"id": 2})
    )
    respx.post(f"{API}/app/installations/1/access_tokens").mock(
        return_value=httpx.Response(
            201, json={"token": "tok1", "expires_at": "2099-01-01T00:00:00Z"}
        )
    )
    respx.post(f"{API}/app/installations/2/access_tokens").mock(
        return_value=httpx.Response(
            201, json={"token": "tok2", "expires_at": "2099-01-01T00:00:00Z"}
        )
    )
    async with httpx.AsyncClient() as client:
        provider = AppTokenProvider("123", rsa_key, API, client=client)
        assert await provider.token_for("acme/a") == "tok1"
        assert await provider.token_for("other/b") == "tok2"


# -- selection -------------------------------------------------------------


def test_build_provider_prefers_app_over_pat(rsa_key):
    settings = Settings(
        github_token="ghp_x",
        github_app_id="1",
        github_app_private_key=rsa_key,
        github_repo="a/b",
    )
    assert settings.auth_mode == "app"
    assert build_provider(settings).describe().startswith("github-app")


def test_build_provider_falls_back_to_pat():
    settings = Settings(github_token="ghp_x", github_repo="a/b")
    assert settings.auth_mode == "pat"
    assert build_provider(settings).describe() == "pat"


def test_build_provider_returns_none_without_credentials():
    assert build_provider(Settings(github_repo="a/b")) is None


def test_escaped_newlines_in_the_private_key_env_var_are_restored():
    """`E2I_GITHUB_APP_PRIVATE_KEY` from a .env file arrives with literal \\n."""
    settings = Settings(github_app_private_key="line1\\nline2")
    assert settings.resolved_private_key() == "line1\nline2"


def test_private_key_can_be_read_from_a_file(tmp_path, rsa_key):
    path = tmp_path / "key.pem"
    path.write_text(rsa_key)
    settings = Settings(github_app_id="1", github_app_private_key_file=str(path))
    assert settings.resolved_private_key() == rsa_key
    assert settings.auth_mode == "app"
