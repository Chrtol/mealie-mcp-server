import asyncio
import base64
import json
import logging
import os
import time

import httpx
from joserfc import jwt
from joserfc.jwk import KeySet
from joserfc.errors import JoseError

from mcp.server.auth.provider import AccessToken

logger = logging.getLogger("mealie-mcp.auth")

_discovery_cache: dict[str, str] = {}  # issuer -> jwks_uri
_jwks_cache: dict = {}
_jwks_cached_at: float = 0.0
_JWKS_TTL = 3600  # seconds
# L4(c): a token whose `kid` isn't in the cached JWKS means Authentik likely
# rotated its signing key. We force one immediate refetch instead of waiting out
# _JWKS_TTL (otherwise every token fails for up to an hour). To stop a flood of
# bogus-kid tokens from hammering Authentik, a forced refetch is rate-limited to
# at most once per _JWKS_MIN_REFETCH seconds.
_JWKS_MIN_REFETCH = 60  # seconds

# Explicit signature-algorithm allowlist passed to jwt.decode(). Without it,
# joserfc validates against whatever its default registry accepts, which leaves
# the door open to algorithm-confusion (e.g. a token forged with alg:HS256 using
# the RSA public key bytes as the HMAC secret). Authentik signs OIDC tokens
# asymmetrically with RS256 when a Signing Key (certificate) is configured — its
# default setup — so pin RS256. If Authentik is ever switched to an EC signing
# certificate (ES256) this must be updated to match the JWKS.
_ALLOWED_ALGORITHMS = ["RS256"]

# Gate the cold-cache upstream fetches so a burst of concurrent token
# verifications doesn't stampede Authentik with N simultaneous discovery/JWKS
# requests. Warm-cache reads return BEFORE taking the lock (no contention on
# the hot path); on a miss, the lock holder fetches once and everyone else
# re-checks the now-populated cache. asyncio.Lock() at module scope is safe on
# Python 3.10+ (loop-agnostic until first await).
_discovery_lock = asyncio.Lock()
_jwks_lock = asyncio.Lock()


async def _get_jwks_uri(issuer: str, headers: dict | None = None) -> str:
    if issuer in _discovery_cache:
        return _discovery_cache[issuer]
    async with _discovery_lock:
        # Re-check: a concurrent caller may have populated it while we waited.
        if issuer in _discovery_cache:
            return _discovery_cache[issuer]
        discovery_url = f"{issuer}/.well-known/openid-configuration"
        logger.info("Fetching OIDC discovery document from %s", discovery_url)
        async with httpx.AsyncClient() as client:
            resp = await client.get(discovery_url, timeout=10, headers=headers or {})
            resp.raise_for_status()
            data = resp.json()
            jwks_uri = data["jwks_uri"]
            _discovery_cache[issuer] = jwks_uri
            logger.info("Discovered JWKS URI: %s", jwks_uri)
            return jwks_uri


async def _get_jwks(jwks_uri: str, headers: dict | None = None, force: bool = False) -> dict:
    global _jwks_cache, _jwks_cached_at
    now = time.monotonic()
    # Warm-cache fast path (skipped on a forced refetch so a rotated key is picked
    # up immediately rather than waiting out the TTL).
    if not force and _jwks_cache and (now - _jwks_cached_at) < _JWKS_TTL:
        logger.debug("JWKS cache hit (age=%.0fs)", now - _jwks_cached_at)
        return _jwks_cache
    async with _jwks_lock:
        # Re-check under the lock: a concurrent caller may have just fetched.
        now = time.monotonic()
        if not force and _jwks_cache and (now - _jwks_cached_at) < _JWKS_TTL:
            logger.debug("JWKS cache hit after lock (age=%.0fs)", now - _jwks_cached_at)
            return _jwks_cache
        # Rate-limit forced refetches: if we (re)fetched very recently, serve the
        # current cache rather than letting bogus-kid tokens stampede Authentik.
        if force and _jwks_cache and (now - _jwks_cached_at) < _JWKS_MIN_REFETCH:
            logger.debug("Forced JWKS refetch suppressed (last fetch %.0fs ago)",
                         now - _jwks_cached_at)
            return _jwks_cache
        logger.info("Fetching JWKS from %s%s", jwks_uri, " (forced)" if force else "")
        async with httpx.AsyncClient() as client:
            resp = await client.get(jwks_uri, timeout=10, headers=headers or {})
            resp.raise_for_status()
            _jwks_cache = resp.json()
            _jwks_cached_at = now
            key_count = len(_jwks_cache.get("keys", []))
            logger.info("JWKS fetched successfully (%d key(s))", key_count)
            return _jwks_cache


def _token_kid(token: str) -> str | None:
    """Best-effort parse of the `kid` from a JWT header WITHOUT verifying — used
    only to decide whether a forced JWKS refetch is warranted."""
    try:
        header_b64 = token.split(".", 1)[0]
        padding = "=" * (-len(header_b64) % 4)
        header = json.loads(base64.urlsafe_b64decode(header_b64 + padding))
        kid = header.get("kid")
        return kid if isinstance(kid, str) else None
    except Exception:
        return None


def _jwks_kids(jwks_data: dict) -> set:
    return {k.get("kid") for k in jwks_data.get("keys", []) if k.get("kid")}


class AuthentikTokenVerifier:
    """Validates Authentik-issued JWTs for the MCP bearer auth middleware."""

    def __init__(self, issuer: str, audience: str | None = None, jwks_uri: str | None = None, host_header: str | None = None):
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._jwks_uri_override = jwks_uri
        self._headers = {"Host": host_header} if host_header else {}
        logger.info(
            "AuthentikTokenVerifier initialized (issuer=%s, audience=%s, jwks_uri=%s, host_header=%s)",
            self._issuer,
            audience or "not set",
            jwks_uri or "via OIDC discovery",
            host_header or "not set",
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        logger.debug("Verifying bearer token")
        try:
            if self._jwks_uri_override:
                jwks_uri = self._jwks_uri_override
            else:
                jwks_uri = await _get_jwks_uri(self._issuer, self._headers)
            jwks_data = await _get_jwks(jwks_uri, self._headers)
            # L4(c): if the token's signing key isn't in our cached JWKS, Authentik
            # has likely rotated keys — force one (rate-limited) refetch before
            # giving up, so rotation doesn't cause up to an hour of auth outage.
            kid = _token_kid(token)
            if kid is not None and kid not in _jwks_kids(jwks_data):
                logger.info("Token kid=%s absent from cached JWKS — forcing refetch", kid)
                jwks_data = await _get_jwks(jwks_uri, self._headers, force=True)
            key_set = KeySet.import_key_set(jwks_data)
            token_obj = jwt.decode(token, key_set, algorithms=_ALLOWED_ALGORITHMS)
            claims = token_obj.claims
            now = int(time.time())
            exp = claims.get("exp")
            if exp is None or now > int(exp):
                logger.warning("Token expired or missing exp claim")
                return None
            # Validate nbf (not before) if present
            nbf = claims.get("nbf")
            if nbf is not None and now < int(nbf):
                logger.warning("Token not yet valid (nbf=%s, now=%s)", nbf, now)
                return None
            token_iss = (claims.get("iss") or "").rstrip("/")
            logger.debug("Token iss=%r (expected=%r)", token_iss, self._issuer)
            if token_iss != self._issuer:
                logger.warning("ISS mismatch: token=%r expected=%r", token_iss, self._issuer)
                return None
            if self._audience:
                aud = claims.get("aud")
                if isinstance(aud, str):
                    aud = [aud]
                if not aud or self._audience not in aud:
                    logger.warning("Audience mismatch: %r", aud)
                    return None
            scopes_raw = claims.get("scope", "")
            scopes = scopes_raw.split() if isinstance(scopes_raw, str) else list(scopes_raw)
            client_id = claims.get("client_id") or claims.get("azp") or claims.get("sub", "")
            logger.debug("Token valid (client_id=%s, scopes=%s)", client_id, scopes)
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=scopes,
                expires_at=int(exp) if exp is not None else None,
            )
        except JoseError as e:
            logger.warning("JWT validation failed: %s", e)
            return None
        except httpx.HTTPError as e:
            logger.error("Failed to fetch OIDC discovery or JWKS: %s", e)
            return None
        except Exception as e:
            logger.error("Unexpected error during token verification: %s", e, exc_info=True)
            return None


def build_token_verifier() -> AuthentikTokenVerifier:
    issuer = os.environ["AUTHENTIK_ISSUER"]
    audience = os.environ.get("AUTHENTIK_AUDIENCE")
    # L4(a): require audience whenever auth is enabled (this function is only
    # called when AUTHENTIK_ISSUER is set). Without it the `aud` check is skipped
    # and ANY valid token from the issuer is accepted — including one minted for a
    # different app sharing the same Authentik (confused-deputy). Fail closed at
    # startup with a clear message rather than silently accepting cross-app tokens.
    if not audience:
        raise RuntimeError(
            "AUTHENTIK_AUDIENCE is required when auth is enabled (AUTHENTIK_ISSUER "
            "is set). Set it to this server's audience/client id so tokens minted "
            "for other Authentik apps are rejected."
        )
    jwks_uri = os.environ.get("AUTHENTIK_JWKS_URI")
    host_header = os.environ.get("AUTHENTIK_HOST")
    return AuthentikTokenVerifier(issuer=issuer, audience=audience, jwks_uri=jwks_uri, host_header=host_header)
