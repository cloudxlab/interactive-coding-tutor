"""OAuth sign-in (Google, GitHub, Microsoft) and session handling for multi-user mode.

Security notes
* Users are identified by (provider, provider's stable user id), NEVER by email: emails can be unverified or
  re-assigned, and accounts on different providers are deliberately not merged.
* Redirect URIs are built from TUTOR_BASE_URL, not from request headers (no Host-header poisoning).
* OIDC providers: state + nonce + PKCE (S256); the ID token's signature, issuer, audience and nonce are checked.
* Sessions are signed cookies (HttpOnly, SameSite=Lax, Secure on https) that hold only the numeric user id.
* All unsafe requests must be same-origin (see origin_guard).
"""
import os
import urllib.parse

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.responses import PlainTextResponse

BASE_URL = os.environ.get("TUTOR_BASE_URL", "").rstrip("/")
ALLOWED_DOMAINS = {d.strip().lower().lstrip("@") for d in os.environ.get("TUTOR_ALLOWED_DOMAINS", "").split(",") if d.strip()}
ALLOWED_EMAILS = {e.strip().lower() for e in os.environ.get("TUTOR_ALLOWED_EMAILS", "").split(",") if e.strip()}
MS_TENANT = os.environ.get("MICROSOFT_TENANT", "common")
MS_MULTI_TENANT = MS_TENANT.lower() in ("common", "organizations", "consumers")
MS_ISSUER_PREFIX = "https://login.microsoftonline.com/"

oauth = OAuth()
router = APIRouter()
store = None  # set by init()
_labels = {}
_profile_fns = {}
_extra_token_args = {}


def init(user_store):
    """Registers every provider whose credentials are present in the environment."""
    global store
    store = user_store
    env = os.environ.get
    if env("GOOGLE_CLIENT_ID") and env("GOOGLE_CLIENT_SECRET"):
        add_provider(
            "google", "Google", _google_profile,
            client_id=env("GOOGLE_CLIENT_ID"), client_secret=env("GOOGLE_CLIENT_SECRET"),
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"})
    if env("MICROSOFT_CLIENT_ID") and env("MICROSOFT_CLIENT_SECRET"):
        add_provider(
            "microsoft", "Microsoft", _microsoft_profile,
            client_id=env("MICROSOFT_CLIENT_ID"), client_secret=env("MICROSOFT_CLIENT_SECRET"),
            server_metadata_url=f"https://login.microsoftonline.com/{MS_TENANT}/v2.0/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
            # The issuer contains the user's tenant id, so Authlib's fixed-issuer check can't be used;
            # _microsoft_profile verifies the issuer (and tenant) itself.
            token_args={"claims_options": {"iss": {"essential": True}}})
    if env("GITHUB_CLIENT_ID") and env("GITHUB_CLIENT_SECRET"):
        add_provider(
            "github", "GitHub", _github_profile,
            client_id=env("GITHUB_CLIENT_ID"), client_secret=env("GITHUB_CLIENT_SECRET"),
            access_token_url="https://github.com/login/oauth/access_token",
            authorize_url="https://github.com/login/oauth/authorize",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "read:user user:email"})


def add_provider(name, label, profile_fn, token_args=None, **register_kwargs):
    oauth.register(name=name, **register_kwargs)
    _labels[name] = label
    _profile_fns[name] = profile_fn
    _extra_token_args[name] = token_args or {}


def providers():
    return [{"id": n, "label": _labels[n]} for n in _labels]


# ----------------------------------------------------------------------------
# Profile extraction (each returns subject/email/email_verified/name/avatar)
# ----------------------------------------------------------------------------
def _check_aud(claims, client_id):
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if client_id not in auds:
        raise ValueError("ID token was not issued for this application")


async def _google_profile(client, token):
    claims = token.get("userinfo")
    if not claims:
        raise ValueError("no ID token claims returned")
    _check_aud(claims, client.client_id)
    return {"subject": claims["sub"], "email": claims.get("email"), "email_verified": bool(claims.get("email_verified")),
            "name": claims.get("name"), "avatar": claims.get("picture")}


async def _microsoft_profile(client, token):
    claims = token.get("userinfo")
    if not claims:
        raise ValueError("no ID token claims returned")
    _check_aud(claims, client.client_id)
    tid = claims.get("tid")
    if not tid or claims.get("iss") != f"{MS_ISSUER_PREFIX}{tid}/v2.0":
        raise ValueError("unexpected token issuer")
    if not MS_MULTI_TENANT and tid.lower() != MS_TENANT.lower():
        raise ValueError("user is not from the configured tenant")
    # `sub` is only unique per application; combine with the tenant for a stable identity.
    # Microsoft's `email` claim is mutable/unverified, so it is never trusted for access decisions.
    return {"subject": f"{tid}:{claims.get('oid') or claims['sub']}",
            "email": claims.get("email") or claims.get("preferred_username"), "email_verified": False,
            "name": claims.get("name"), "avatar": None}


async def _github_profile(client, token):
    user = (await client.get("user", token=token)).json()
    email, verified = user.get("email"), False
    emails = await client.get("user/emails", token=token)
    if emails.status_code == 200:
        for e in emails.json():
            if e.get("primary") and e.get("verified"):
                email, verified = e["email"], True
                break
    return {"subject": str(user["id"]), "email": email, "email_verified": verified,
            "name": user.get("name") or user.get("login"), "avatar": user.get("avatar_url")}


# ----------------------------------------------------------------------------
# Access policy
# ----------------------------------------------------------------------------
def access_denied_reason(provider, profile):
    """None if the user may sign in, else a short reason. Only VERIFIED emails count for the allow-lists."""
    if not (ALLOWED_DOMAINS or ALLOWED_EMAILS):
        return None
    if provider == "microsoft" and MS_MULTI_TENANT:
        return "multi-tenant Microsoft sign-in cannot be combined with allow-lists (set MICROSOFT_TENANT to your tenant id)"
    if provider == "microsoft":
        return None  # tenant already verified in _microsoft_profile: everyone in the tenant is allowed
    email = (profile.get("email") or "").lower()
    if not email or not profile.get("email_verified"):
        return "no verified email"
    if email in ALLOWED_EMAILS or email.rsplit("@", 1)[-1] in ALLOWED_DOMAINS:
        return None
    return "email not on the allow-list"


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
def _client(provider):
    if provider not in _labels:
        raise HTTPException(404, "Unknown sign-in provider")
    return getattr(oauth, provider)


@router.get("/auth/login/{provider}")
async def login(provider: str, request: Request):
    client = _client(provider)
    redirect_uri = f"{BASE_URL}/auth/callback/{provider}"
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback/{provider}")
async def callback(provider: str, request: Request):
    client = _client(provider)
    try:
        token = await client.authorize_access_token(request, **_extra_token_args[provider])
        profile = await _profile_fns[provider](client, token)
    except (OAuthError, ValueError, KeyError) as e:
        print(f"[auth] {provider} sign-in failed: {type(e).__name__}: {e}", flush=True)
        return RedirectResponse("/?login_error=failed", status_code=303)
    reason = access_denied_reason(provider, profile)
    if reason:
        print(f"[auth] {provider} sign-in denied: {reason}", flush=True)
        return RedirectResponse("/?login_error=denied", status_code=303)
    user = store.upsert_user(provider, profile["subject"], profile.get("email"), profile.get("name"), profile.get("avatar"))
    request.session.clear()  # drop the OAuth state; start a fresh session for the signed-in user
    request.session["uid"] = user["id"]
    return RedirectResponse("/", status_code=303)


@router.post("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


# ----------------------------------------------------------------------------
# Dependencies / middleware
# ----------------------------------------------------------------------------
def session_user(request_or_ws):
    """The signed-in user's row, or None. Works for both HTTP requests and WebSockets."""
    uid = request_or_ws.session.get("uid")
    return store.get_user(uid) if uid is not None else None


def require_user(request: Request):
    user = session_user(request)
    if not user:
        raise HTTPException(401, "Please sign in.")
    return user


def origin_guard(app, base_url):
    """Blocks cross-site state-changing requests (CSRF defence on top of SameSite=Lax cookies)."""
    allowed = urllib.parse.urlparse(base_url)
    allowed_origin = f"{allowed.scheme}://{allowed.netloc}"

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if (origin and origin != allowed_origin) or request.headers.get("sec-fetch-site") == "cross-site":
                return PlainTextResponse("Cross-site request blocked", status_code=403)
        return await call_next(request)


def security_headers(app):
    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if not request.url.path.startswith("/jupyter/"):
            resp.headers.setdefault("X-Frame-Options", "DENY")  # only /jupyter/ may be framed (by our own page)
        return resp
