"""
Script 3: Authentik token-verifier unit tests.

Unlike test_fetcher.py and test_mcp_server.py, this script needs NO live
infrastructure — no Mealie, no Authentik, no running MCP server. It mints its
own RS256 signing keys locally with joserfc and stubs the JWKS cache, then
drives src/auth.py directly to lock in the security guarantees added in 1.0.19:

  - audience enforcement (accept matching aud, reject wrong/missing aud)
  - RS256 algorithm pinning (reject an HS256-forged token)
  - signing-key rotation (unknown kid forces one JWKS refetch)
  - forced-refetch rate limiting (_JWKS_MIN_REFETCH)
  - exp / nbf / iss claim validation
  - build_token_verifier() fails closed when AUTHENTIK_AUDIENCE is unset

Usage:
    cd mealie-mcp-server
    python tests/test_auth.py
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from joserfc import jwt
from joserfc.jwk import OctKey, RSAKey

import auth

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

results: list[tuple[str, bool, str]] = []

ISSUER = "https://auth.example.com/application/o/mealie-mcp-server"
AUDIENCE = "mealie-mcp-client-id"


def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    results.append((name, bool(condition), detail))


def section(title: str) -> None:
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def mint_key(kid: str) -> RSAKey:
    """Generate an RS256 signing key carrying the given kid."""
    return RSAKey.generate_key(2048, parameters={"kid": kid}, private=True)


def jwks_of(*keys: RSAKey) -> dict:
    """Build a public JWKS document from one or more keys."""
    return {"keys": [k.as_dict(private=False) for k in keys]}


def make_token(key: RSAKey, kid: str, **claim_overrides) -> str:
    """Sign an RS256 JWT with sensible valid defaults, overridable per test."""
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-123",
        "exp": now + 3600,
        "nbf": now - 10,
        "scope": "openid profile",
        "client_id": AUDIENCE,
    }
    claims.update(claim_overrides)
    return jwt.encode({"alg": "RS256", "kid": kid}, claims, key)


def prime_cache(jwks: dict) -> None:
    """Populate the module JWKS cache so verify_token takes the warm path
    (no network) — cache is set fresh so the TTL has not elapsed."""
    auth._jwks_cache = jwks
    auth._jwks_cached_at = time.monotonic()


def verify(token: str, audience: str | None = AUDIENCE):
    verifier = auth.AuthentikTokenVerifier(
        issuer=ISSUER, audience=audience, jwks_uri="https://stub/jwks"
    )
    return asyncio.run(verifier.verify_token(token))


# ---------------------------------------------------------------------------
def run() -> None:
    key = mint_key("key-1")
    prime_cache(jwks_of(key))

    # -----------------------------------------------------------------------
    section("Happy path")
    # -----------------------------------------------------------------------
    tok = make_token(key, "key-1")
    res = verify(tok)
    check("valid RS256 token accepted", res is not None and res.client_id == AUDIENCE)

    # -----------------------------------------------------------------------
    section("Audience enforcement")
    # -----------------------------------------------------------------------
    prime_cache(jwks_of(key))
    check("token with wrong aud rejected", verify(make_token(key, "key-1", aud="some-other-app")) is None)

    prime_cache(jwks_of(key))
    check("token with no aud claim rejected", verify(make_token(key, "key-1", aud=None)) is None)

    prime_cache(jwks_of(key))
    check("aud as list containing audience accepted",
          verify(make_token(key, "key-1", aud=[AUDIENCE, "other"])) is not None)

    # -----------------------------------------------------------------------
    section("Algorithm pinning (alg-confusion)")
    # -----------------------------------------------------------------------
    prime_cache(jwks_of(key))
    hs_token = jwt.encode(
        {"alg": "HS256"},
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "exp": int(time.time()) + 3600},
        OctKey.import_key("shared-secret-not-the-signing-key"),
    )
    check("HS256-forged token rejected (RS256 pinned)", verify(hs_token) is None)

    # -----------------------------------------------------------------------
    section("Claim validation")
    # -----------------------------------------------------------------------
    now = int(time.time())
    prime_cache(jwks_of(key))
    check("expired token (exp in past) rejected", verify(make_token(key, "key-1", exp=now - 5)) is None)

    prime_cache(jwks_of(key))
    check("not-yet-valid token (nbf in future) rejected", verify(make_token(key, "key-1", nbf=now + 600)) is None)

    prime_cache(jwks_of(key))
    check("wrong issuer rejected", verify(make_token(key, "key-1", iss="https://evil.example.com")) is None)

    prime_cache(jwks_of(key))
    check("trailing-slash issuer normalized (accepted)",
          verify(make_token(key, "key-1", iss=ISSUER + "/")) is not None)

    # -----------------------------------------------------------------------
    section("Signing-key rotation (unknown kid → forced refetch)")
    # -----------------------------------------------------------------------
    old_key = mint_key("old-key")
    new_key = mint_key("new-key")
    # Cold cache holds only the OLD key; the token is signed with the NEW key.
    prime_cache(jwks_of(old_key))

    calls = {"n": 0, "forced": 0}
    real_get_jwks = auth._get_jwks

    async def stub_get_jwks(jwks_uri, headers=None, force=False):
        calls["n"] += 1
        if force:
            calls["forced"] += 1
            return jwks_of(old_key, new_key)  # rotation: new key now published
        return jwks_of(old_key)

    auth._get_jwks = stub_get_jwks
    try:
        rotated = verify(make_token(new_key, "new-key"))
    finally:
        auth._get_jwks = real_get_jwks
    check("token with unknown kid validates after forced refetch", rotated is not None)
    check("forced refetch happened exactly once", calls["forced"] == 1, f"forced={calls['forced']} total={calls['n']}")

    # -----------------------------------------------------------------------
    section("Forced-refetch rate limiting (_JWKS_MIN_REFETCH)")
    # -----------------------------------------------------------------------
    http_calls = {"n": 0}

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return jwks_of(key)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            http_calls["n"] += 1
            return _FakeResp()

    real_async_client = auth.httpx.AsyncClient
    auth.httpx.AsyncClient = lambda *a, **k: _FakeClient()
    try:
        # Recent fetch → a forced refetch within the window must be suppressed.
        auth._jwks_cache = jwks_of(key)
        auth._jwks_cached_at = time.monotonic()
        asyncio.run(auth._get_jwks("https://stub/jwks", force=True))
        check("forced refetch suppressed within rate-limit window", http_calls["n"] == 0, f"http_calls={http_calls['n']}")

        # Older than the window → a forced refetch is allowed through.
        auth._jwks_cached_at = time.monotonic() - (auth._JWKS_MIN_REFETCH + 1)
        asyncio.run(auth._get_jwks("https://stub/jwks", force=True))
        check("forced refetch allowed after rate-limit window", http_calls["n"] == 1, f"http_calls={http_calls['n']}")
    finally:
        auth.httpx.AsyncClient = real_async_client

    # -----------------------------------------------------------------------
    section("Startup config (build_token_verifier fails closed)")
    # -----------------------------------------------------------------------
    saved = {k: os.environ.get(k) for k in ("AUTHENTIK_ISSUER", "AUTHENTIK_AUDIENCE", "AUTHENTIK_JWKS_URI", "AUTHENTIK_HOST")}
    try:
        os.environ["AUTHENTIK_ISSUER"] = ISSUER
        os.environ.pop("AUTHENTIK_AUDIENCE", None)
        raised = False
        try:
            auth.build_token_verifier()
        except RuntimeError:
            raised = True
        check("build_token_verifier raises when AUTHENTIK_AUDIENCE unset", raised)

        os.environ["AUTHENTIK_AUDIENCE"] = AUDIENCE
        v = auth.build_token_verifier()
        check("build_token_verifier succeeds when audience set", v is not None)
    finally:
        for k, val in saved.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val

    # -----------------------------------------------------------------------
    section("Summary")
    # -----------------------------------------------------------------------
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    total = len(results)
    print(f"\n  {passed}/{total} passed", end="")
    if failed:
        print(f"  ({failed} failed)\n\nFailed tests:")
        for name, ok, detail in results:
            if not ok:
                print(f"  - {name}" + (f": {detail}" if detail else ""))
    else:
        print(" — all tests passed")
    print()
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    run()
