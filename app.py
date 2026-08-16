"""
oauth-proxy — spec-compliant MCP OAuth 2.1 front door for an unmodified,
loopback-only MCP backend (e.g. grafana/mcp-grafana on Cloud Run).

Design (see internal wiki: "MCP OAuth on Cloud Run" -> Concrete Design:
OAuth Proxy Sidecar):

  - Downstream (to the MCP client, e.g. Claude): this process IS the OAuth
    2.1 authorization server + resource-server guard. It serves discovery
    metadata, /register, /authorize, /token, and gates /mcp.
  - Upstream (to Google): this process is an ordinary pre-registered OAuth
    client (client_id/secret created once in Google Cloud Console).

Statelessness (required for Cloud Run scale-to-zero / multi-instance):
  Nothing is kept in memory. The "database" is a symmetric signing key.
  - client_id issued by /register is itself a signed JWT carrying the
    agreed redirect_uris (forging one requires SIGNING_KEY).
  - The `state` sent to Google on the upstream leg is a signed JWT carrying
    everything needed to resume the flow on ANY instance that receives the
    callback: the downstream client's PKCE challenge, redirect_uri,
    client_id, resource, and this proxy's own upstream PKCE verifier.
  - The authorization code handed back to the client is a signed JWT too
    (short TTL, single logical use enforced by tight expiry + PKCE binding).
  - Access/refresh tokens are signed JWTs. No revocation before expiry
    (accepted risk from the wiki doc) -> keep access TTL short (1h).

Run:
    uvicorn app:app --host 0.0.0.0 --port 8080
"""

import base64
import hashlib
import logging
import os
import time
import urllib.parse
from typing import Optional

import httpx
import jwt as pyjwt
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

logger = logging.getLogger("oauth-proxy")
logging.basicConfig(level=logging.INFO)

# --------------------------------------------------------------------------
# Config (all via env vars / Secret Manager — nothing hardcoded)
# --------------------------------------------------------------------------

PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")          # e.g. https://grafana-mcp.pattaravut.info
UPSTREAM_URL = os.environ["UPSTREAM_URL"].rstrip("/")      # e.g. http://127.0.0.1:8000
RESOURCE = os.environ.get("RESOURCE", f"{PUBLIC_URL}/mcp") # canonical MCP resource URI (path included!)

SIGNING_KEY = os.environ["SIGNING_KEY"]                    # long random secret, from Secret Manager
JWT_ALG = "HS256"

GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

ALLOWED_EMAILS = {e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()}
ALLOWED_DOMAINS = {d.strip().lower() for d in os.environ.get("ALLOWED_DOMAINS", "").split(",") if d.strip()}

ACCESS_TOKEN_TTL = int(os.environ.get("ACCESS_TOKEN_TTL", "3600"))       # 1h
REFRESH_TOKEN_TTL = int(os.environ.get("REFRESH_TOKEN_TTL", "1209600"))  # 14d
AUTH_CODE_TTL = 120        # seconds — narrow window between /authorize and /token
UPSTREAM_STATE_TTL = 600   # seconds — time allowed to complete the Google login screen

if not (ALLOWED_EMAILS or ALLOWED_DOMAINS):
    # Fail closed: refuse to boot rather than accept-all.
    raise RuntimeError("Refusing to start: set ALLOWED_EMAILS and/or ALLOWED_DOMAINS")


# --------------------------------------------------------------------------
# JWT helpers — this is the entire "database"
# --------------------------------------------------------------------------

def sign(payload: dict) -> str:
    return pyjwt.encode({**payload, "iat": int(time.time())}, SIGNING_KEY, algorithm=JWT_ALG)


def verify(token: str, expected_typ: Optional[str] = None) -> dict:
    # verify_aud=False: PyJWT refuses to decode a token carrying an "aud"
    # claim unless you pass audience=<expected value> to decode() -- it
    # raises InvalidAudienceError otherwise, even before returning the
    # payload for inspection. Access/refresh tokens always carry "aud"
    # (see _issue_tokens), and mcp_proxy does its own explicit
    # claims.get("aud") != RESOURCE check right after this returns, so
    # PyJWT's own check is both redundant and, without an audience= this
    # generic across token types, was rejecting every access token
    # outright.
    payload = pyjwt.decode(token, SIGNING_KEY, algorithms=[JWT_ALG], options={"verify_aud": False})
    if expected_typ and payload.get("typ") != expected_typ:
        raise pyjwt.InvalidTokenError(f"expected typ={expected_typ}")
    return payload


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pkce_challenge_from_verifier(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode()).digest())


def new_code_verifier() -> str:
    return b64url(os.urandom(40))


def www_authenticate_header() -> str:
    resource_metadata = f"{PUBLIC_URL}/.well-known/oauth-protected-resource"
    return f'Bearer resource_metadata="{resource_metadata}"'


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

async def protected_resource_metadata(request: Request):
    return JSONResponse({
        "resource": RESOURCE,
        "authorization_servers": [PUBLIC_URL],
    })


async def authorization_server_metadata(request: Request):
    return JSONResponse({
        "issuer": PUBLIC_URL,
        "authorization_endpoint": f"{PUBLIC_URL}/authorize",
        "token_endpoint": f"{PUBLIC_URL}/token",
        "registration_endpoint": f"{PUBLIC_URL}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["tools:read", "tools:write"],
        # CIMD not implemented here — see README for how to add it if the
        # client requires client_id_metadata_document_supported.
        "client_id_metadata_document_supported": False,
    })


# --------------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591) — stateless
# --------------------------------------------------------------------------

async def register(request: Request):
    body = await request.json()
    redirect_uris = body.get("redirect_uris") or []
    if not redirect_uris or not isinstance(redirect_uris, list):
        return JSONResponse({"error": "invalid_client_metadata", "error_description": "redirect_uris required"}, 400)

    client_id = sign({
        "typ": "client",
        "redirect_uris": redirect_uris,
        "client_name": body.get("client_name", "mcp-client"),
    })
    return JSONResponse({
        "client_id": client_id,
        "client_name": body.get("client_name", "mcp-client"),
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }, 201)


def load_client(client_id: str) -> dict:
    return verify(client_id, expected_typ="client")


# --------------------------------------------------------------------------
# /authorize — downstream leg. Validate the client's request, then start
# our OWN independent PKCE + OAuth round trip against Google.
# --------------------------------------------------------------------------

async def authorize(request: Request):
    q = request.query_params
    client_id = q.get("client_id", "")
    redirect_uri = q.get("redirect_uri", "")
    code_challenge = q.get("code_challenge", "")
    code_challenge_method = q.get("code_challenge_method", "")
    downstream_state = q.get("state", "")
    resource = q.get("resource", RESOURCE)

    if code_challenge_method != "S256" or not code_challenge:
        return JSONResponse({"error": "invalid_request", "error_description": "PKCE S256 required"}, 400)

    try:
        client = load_client(client_id)
    except pyjwt.InvalidTokenError:
        return JSONResponse({"error": "invalid_client"}, 400)

    # Redirect URI must be exactly one the client registered — validated
    # BEFORE any redirect is issued, so an unregistered URI can never
    # become an open redirect.
    if redirect_uri not in client["redirect_uris"]:
        return JSONResponse({"error": "invalid_request", "error_description": "unregistered redirect_uri"}, 400)

    if resource != RESOURCE:
        return JSONResponse({"error": "invalid_target", "error_description": "unknown resource"}, 400)

    # Our own, independent PKCE pair for the upstream (Google) leg — we
    # never relay the client's challenge upstream.
    upstream_verifier = new_code_verifier()
    upstream_challenge = pkce_challenge_from_verifier(upstream_verifier)

    upstream_state = sign({
        "typ": "upstream_state",
        "exp": int(time.time()) + UPSTREAM_STATE_TTL,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "downstream_state": downstream_state,
        "downstream_code_challenge": code_challenge,
        "resource": resource,
        "upstream_verifier": upstream_verifier,
    })

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": f"{PUBLIC_URL}/oauth2/callback",
        "response_type": "code",
        "scope": "openid email",
        "code_challenge": upstream_challenge,
        "code_challenge_method": "S256",
        "state": upstream_state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return RedirectResponse(f"{GOOGLE_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}")


# --------------------------------------------------------------------------
# /oauth2/callback — Google redirects here. Exchange code, check the
# allowlist, discard Google's token, mint OUR OWN authorization code.
# --------------------------------------------------------------------------

async def google_callback(request: Request):
    q = request.query_params
    error = q.get("error")
    if error:
        return JSONResponse({"error": "access_denied", "error_description": error}, 400)

    google_code = q.get("code", "")
    upstream_state_token = q.get("state", "")

    try:
        state = verify(upstream_state_token, expected_typ="upstream_state")
    except pyjwt.InvalidTokenError:
        return JSONResponse({"error": "invalid_request", "error_description": "bad or expired state"}, 400)

    async with httpx.AsyncClient(timeout=10) as client:
        token_resp = await client.post(GOOGLE_TOKEN_ENDPOINT, data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "code": google_code,
            "redirect_uri": f"{PUBLIC_URL}/oauth2/callback",
            "grant_type": "authorization_code",
            "code_verifier": state["upstream_verifier"],
        })
        token_resp.raise_for_status()
        google_tokens = token_resp.json()

        userinfo_resp = await client.get(
            GOOGLE_USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {google_tokens['access_token']}"},
        )
        userinfo_resp.raise_for_status()
        userinfo = userinfo_resp.json()
    # Google's token is used once, for userinfo, then dropped on the floor —
    # it is NEVER forwarded to the client or to the backend (no confused
    # deputy / no token passthrough).

    email = (userinfo.get("email") or "").lower()
    email_verified = userinfo.get("email_verified", False)
    domain = email.split("@")[-1] if "@" in email else ""

    if not (email_verified and (email in ALLOWED_EMAILS or domain in ALLOWED_DOMAINS)):
        return JSONResponse({"error": "access_denied", "error_description": "not on allowlist"}, 403)

    proxy_code = sign({
        "typ": "auth_code",
        "exp": int(time.time()) + AUTH_CODE_TTL,
        "sub": email,
        "client_id": state["client_id"],
        "redirect_uri": state["redirect_uri"],
        "code_challenge": state["downstream_code_challenge"],
        "resource": state["resource"],
    })

    redirect_params = {"code": proxy_code}
    if state.get("downstream_state"):
        redirect_params["state"] = state["downstream_state"]
    return RedirectResponse(f"{state['redirect_uri']}?{urllib.parse.urlencode(redirect_params)}")


# --------------------------------------------------------------------------
# /token — authorization_code and refresh_token grants
# --------------------------------------------------------------------------

async def token(request: Request):
    form = await request.form()
    grant_type = form.get("grant_type")

    if grant_type == "authorization_code":
        code = form.get("code", "")
        code_verifier = form.get("code_verifier", "")
        redirect_uri = form.get("redirect_uri", "")
        client_id = form.get("client_id", "")

        try:
            claims = verify(code, expected_typ="auth_code")
        except pyjwt.InvalidTokenError:
            return JSONResponse({"error": "invalid_grant"}, 400)

        if claims["client_id"] != client_id or claims["redirect_uri"] != redirect_uri:
            return JSONResponse({"error": "invalid_grant", "error_description": "client/redirect mismatch"}, 400)

        if pkce_challenge_from_verifier(code_verifier) != claims["code_challenge"]:
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, 400)

        return _issue_tokens(sub=claims["sub"], resource=claims["resource"], client_id=client_id)

    elif grant_type == "refresh_token":
        refresh_token = form.get("refresh_token", "")
        try:
            claims = verify(refresh_token, expected_typ="refresh_token")
        except pyjwt.InvalidTokenError:
            return JSONResponse({"error": "invalid_grant"}, 400)

        # Re-check the allowlist on every refresh — a revoked user loses
        # access within one access-token TTL even though tokens themselves
        # can't be revoked before expiry.
        email = claims["sub"]
        domain = email.split("@")[-1] if "@" in email else ""
        if not (email in ALLOWED_EMAILS or domain in ALLOWED_DOMAINS):
            return JSONResponse({"error": "invalid_grant", "error_description": "no longer allowlisted"}, 400)

        return _issue_tokens(sub=email, resource=claims["resource"], client_id=claims["client_id"])

    return JSONResponse({"error": "unsupported_grant_type"}, 400)


def _issue_tokens(sub: str, resource: str, client_id: str) -> JSONResponse:
    now = int(time.time())
    access_token = sign({
        "typ": "access",
        "sub": sub,
        "aud": resource,
        "client_id": client_id,
        "scope": "tools:read tools:write",
        "exp": now + ACCESS_TOKEN_TTL,
    })
    refresh_token = sign({
        "typ": "refresh_token",
        "sub": sub,
        "resource": resource,
        "client_id": client_id,
        "exp": now + REFRESH_TOKEN_TTL,
    })
    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL,
        "refresh_token": refresh_token,
        "scope": "tools:read tools:write",
    })


# --------------------------------------------------------------------------
# /mcp (and anything else) — the resource-server guard + byte-forwarder.
# No MCP parsing at all: once the bearer validates, bytes stream through
# untouched in both directions (SSE-safe, no read timeout).
# --------------------------------------------------------------------------

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}


async def mcp_proxy(request: Request):
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        logger.warning(
            "mcp_proxy 401: no Bearer auth header (header present: %s, value prefix: %r)",
            "authorization" in {k.lower() for k in request.headers.keys()},
            auth_header[:16],
        )
        return Response(status_code=401, headers={"WWW-Authenticate": www_authenticate_header()})

    raw_token = auth_header[len("Bearer "):]
    try:
        claims = verify(raw_token, expected_typ="access")
    except pyjwt.ExpiredSignatureError:
        logger.warning("mcp_proxy 401: token expired (token length %d)", len(raw_token))
        return Response(status_code=401, headers={"WWW-Authenticate": www_authenticate_header() + ', error="invalid_token"'})
    except pyjwt.InvalidTokenError as e:
        logger.warning("mcp_proxy 401: invalid token (%s: %s, token length %d)", type(e).__name__, e, len(raw_token))
        return Response(status_code=401, headers={"WWW-Authenticate": www_authenticate_header() + ', error="invalid_token"'})

    if claims.get("aud") != RESOURCE:
        logger.warning("mcp_proxy 401: aud mismatch (token aud=%r, expected RESOURCE=%r)", claims.get("aud"), RESOURCE)
        return Response(status_code=401, headers={"WWW-Authenticate": www_authenticate_header() + ', error="invalid_token"'})

    # Strip the inbound Authorization header and ANY client-forged
    # X-Auth-* headers before injecting our own — identity spoofing guard.
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP and k.lower() != "authorization" and not k.lower().startswith("x-auth-")
    }
    fwd_headers["x-auth-subject"] = claims["sub"]
    fwd_headers["x-auth-email"] = claims["sub"]
    fwd_headers["x-auth-scope"] = claims.get("scope", "")

    body = await request.body()
    upstream_path = request.url.path
    upstream_url = f"{UPSTREAM_URL}{upstream_path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    client = httpx.AsyncClient(timeout=None)  # no read timeout — MCP holds SSE open
    req = client.build_request(request.method, upstream_url, headers=fwd_headers, content=body)
    upstream_resp = await client.send(req, stream=True)

    async def body_stream():
        async for chunk in upstream_resp.aiter_raw():
            yield chunk
        await upstream_resp.aclose()
        await client.aclose()

    resp_headers = {
        k: v for k, v in upstream_resp.headers.items() if k.lower() not in HOP_BY_HOP
    }
    return StreamingResponse(body_stream(), status_code=upstream_resp.status_code, headers=resp_headers)


async def healthz(request: Request):
    return JSONResponse({"ok": True})


app = Starlette(routes=[
    Route("/healthz", healthz),
    Route("/.well-known/oauth-protected-resource", protected_resource_metadata),
    Route("/.well-known/oauth-authorization-server", authorization_server_metadata),
    Route("/register", register, methods=["POST"]),
    Route("/authorize", authorize),
    Route("/oauth2/callback", google_callback),
    Route("/token", token, methods=["POST"]),
    Route("/{path:path}", mcp_proxy, methods=["GET", "POST", "DELETE", "PUT"]),
])
