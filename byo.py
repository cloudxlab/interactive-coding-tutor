"""Bring your own LLM: each signed-in user can connect their own OpenRouter account with one click (OAuth PKCE).

Flow: /openrouter/connect -> openrouter.ai/auth (user approves) -> /openrouter/callback?code=... -> the SERVER
exchanges the code for an API key and stores it encrypted. Nothing is copy-pasted and the key never reaches the browser.

Security notes
* PKCE binds the flow to the browser session that started it: the code_verifier lives only in that user's signed
  session, is single-use and expires after 10 minutes, so a code obtained by an attacker is useless to the victim's
  session (and vice versa).
* Keys are encrypted at rest (Fernet) with a key derived from TUTOR_SECRET_KEY. Rotating that secret makes stored keys
  undecryptable: users simply reconnect.
* A user's key is only ever used for that user's own requests.
"""
import base64
import hashlib
import os
import re
import time

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

import auth
import openrouter_oauth as oro

VERIFIER_TTL = 600
DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"
SUGGESTED_MODELS = [
    "anthropic/claude-sonnet-4.5", "anthropic/claude-haiku-4.5", "openai/gpt-4.1", "openai/gpt-4.1-mini",
    "google/gemini-2.5-flash", "google/gemini-2.5-pro", "deepseek/deepseek-chat-v3.1", "meta-llama/llama-3.3-70b-instruct",
]
MODEL_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-:~]+$")
# True: users MUST connect their own OpenRouter (the operator needs no LLM key of their own).
REQUIRE = os.environ.get("TUTOR_REQUIRE_BYO_LLM", "").lower() in ("1", "true", "yes")

router = APIRouter()
_store = None
_fernet = None
_shared_available = lambda: False   # does the operator provide a shared LLM? (injected by app.py)
_test_call = None                   # fn(key, model) -> raises on failure (injected by app.py)


def init(store, secret, shared_available, test_call):
    global _store, _fernet, _shared_available, _test_call
    _store, _shared_available, _test_call = store, shared_available, test_call
    # Domain-separated derivation, so the session-signing use of the secret and this use never share a key.
    _fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"tutor-byo-llm-v1:" + secret.encode()).digest()))


def credentials(uid):
    """(api_key, model) of the user's own OpenRouter account, or None if not connected / unreadable."""
    row = _store.get_llm(uid)
    if not row:
        return None
    try:
        key = _fernet.decrypt(row["key_enc"].encode()).decode()
    except InvalidToken:
        print(f"[byo] stored key for user {uid} can't be decrypted (was TUTOR_SECRET_KEY changed?); user must reconnect", flush=True)
        return None
    return key, (row["model"] or DEFAULT_MODEL)


def _redirect(query):
    return RedirectResponse("/?" + query, status_code=303)


# ----------------------------------------------------------------------------
# Browser flow
# ----------------------------------------------------------------------------
@router.get("/openrouter/connect")
def connect(request: Request):
    if not auth.session_user(request):
        return RedirectResponse("/", status_code=303)  # not signed in: the app shows the login screen
    verifier, challenge = oro.make_pkce()
    request.session["or_verifier"] = verifier
    request.session["or_started"] = int(time.time())
    callback_url = f"{auth.BASE_URL}/openrouter/callback"
    return RedirectResponse(oro.build_auth_url(callback_url, challenge), status_code=303)


@router.get("/openrouter/callback")
def callback(request: Request, code: str = "", error: str = ""):
    user = auth.session_user(request)
    if not user:
        return _redirect("llm_error=session")
    verifier = request.session.pop("or_verifier", None)  # single use, even if the exchange below fails
    started = request.session.pop("or_started", 0)
    if not verifier or time.time() - started > VERIFIER_TTL:
        return _redirect("llm_error=expired")
    if error or not code:
        return _redirect("llm_error=denied")
    try:
        key = oro.exchange_code(code, verifier)
    except oro.OpenRouterError as e:
        print(f"[byo] OpenRouter code exchange failed for user {user['id']}: {str(e)[:200]}", flush=True)
        return _redirect("llm_error=failed")
    _store.set_llm_key(user["id"], _fernet.encrypt(key.encode()).decode())
    return _redirect("llm=connected")


# ----------------------------------------------------------------------------
# JSON API
# ----------------------------------------------------------------------------
class ModelBody(BaseModel):
    model: str


def status(uid):
    creds = credentials(uid)
    shared = _shared_available()
    return {
        "connected": creds is not None,
        "model": creds[1] if creds else None,
        "default_model": DEFAULT_MODEL,
        "suggested_models": SUGGESTED_MODELS,
        "require_byo": REQUIRE or not shared,   # no shared LLM configured => everyone must bring their own
        "shared_available": shared and not REQUIRE,
    }


@router.get("/api/llm")
def get_llm(request: Request):
    user = auth.require_user(request)
    return status(user["id"])


@router.post("/api/llm/model")
def set_model(body: ModelBody, request: Request):
    user = auth.require_user(request)
    model = body.model.strip()
    if not MODEL_RE.match(model) or len(model) > 100:
        raise HTTPException(400, "A model id looks like provider/model, e.g. anthropic/claude-sonnet-4.5.")
    creds = credentials(user["id"])
    if not creds:
        raise HTTPException(409, "llm_not_connected")
    try:
        _test_call(creds[0], model)  # fail now, not in the middle of a lesson
    except Exception as e:
        raise HTTPException(400, f"That model didn't work with your OpenRouter account: {getattr(e, 'detail', e)}")
    _store.set_llm_model(user["id"], model)
    return status(user["id"])


@router.post("/api/llm/disconnect")
def disconnect(request: Request):
    user = auth.require_user(request)
    _store.delete_llm(user["id"])
    return status(user["id"])
