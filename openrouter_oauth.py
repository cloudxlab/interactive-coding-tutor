#!/usr/bin/env python3
"""Sign in with OpenRouter (OAuth PKCE) and get an API key without copy/pasting.

How it works (OpenRouter's documented PKCE flow):
  1. We make a random secret (code_verifier) and send its SHA-256 hash (code_challenge) to OpenRouter.
  2. Your browser opens openrouter.ai/auth. You log in and click Authorize.
  3. OpenRouter redirects to our callback URL (http://localhost:3000/) with ?code=...
  4. We POST the code + code_verifier to /api/v1/auth/keys and receive an API key.

Usage:
  python openrouter_oauth.py                 # sign in, then save OPENROUTER_API_KEY (+ TUTOR_LLM) to .env
  python openrouter_oauth.py --print-key     # sign in, print only the key to stdout (for scripts)
  python openrouter_oauth.py --manual        # no local server: paste the code from the redirect URL yourself
  python openrouter_oauth.py --no-browser    # print the sign-in URL instead of opening a browser

Notes:
  * OpenRouter only allows https callbacks, plus http://localhost:3000 for development, so the default
    port is 3000. If it is busy, free it (or use --manual).
  * Only the Python standard library is used.
  * Docs: https://openrouter.ai/docs (OAuth PKCE).
"""
import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

AUTH_URL = os.environ.get("OPENROUTER_AUTH_URL", "https://openrouter.ai/auth")
DEFAULT_API_BASE = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
ENV_FILE = Path(__file__).parent / ".env"


class OpenRouterError(RuntimeError):
    """The code exchange failed. (A library error, not SystemExit, so web handlers can catch it.)"""


def log(*a):
    print(*a, file=sys.stderr)


def make_pkce():
    """RFC 7636: verifier is 43-128 URL-safe chars; challenge = base64url(sha256(verifier)), unpadded."""
    verifier = secrets.token_urlsafe(64)  # 86 chars
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_auth_url(callback_url, challenge):
    q = urllib.parse.urlencode({"callback_url": callback_url, "code_challenge": challenge, "code_challenge_method": "S256"})
    return f"{AUTH_URL}?{q}"


def wait_for_code(port, path, timeout):
    """Runs a one-shot local HTTP server and returns the ?code= value from OpenRouter's redirect."""
    result = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path.rstrip("/") != path.rstrip("/"):
                self.send_error(404)
                return
            qs = urllib.parse.parse_qs(u.query)
            result["code"] = (qs.get("code") or [""])[0]
            result["error"] = (qs.get("error") or [""])[0]
            ok = bool(result["code"])
            body = ("<h2>Signed in to OpenRouter.</h2><p>You can close this tab and go back to the terminal.</p>"
                    if ok else "<h2>Sign-in failed.</h2><p>No code was returned. Go back to the terminal.</p>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *args):
            pass

    try:
        server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        raise SystemExit(f"Could not listen on port {port} ({e}). Free the port, or re-run with --manual.")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        if not done.wait(timeout):
            raise SystemExit(f"Timed out after {timeout}s waiting for the browser redirect. Try again, or use --manual.")
    finally:
        server.shutdown()
        server.server_close()
    if not result.get("code"):
        raise SystemExit(f"OpenRouter did not return a code ({result.get('error') or 'no error given'}).")
    return result["code"]


def exchange_code(code, verifier, api_base=DEFAULT_API_BASE):
    """POST /auth/keys -> {"key": "sk-or-..."}. Raises OpenRouterError on any failure."""
    body = json.dumps({"code": code, "code_verifier": verifier, "code_challenge_method": "S256"}).encode()
    req = urllib.request.Request(api_base.rstrip("/") + "/auth/keys", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise OpenRouterError(f"OpenRouter rejected the code (HTTP {e.code}): {e.read().decode('utf-8', 'replace')[:300]}\n"
                              "The code is single-use and short-lived; run the script again.")
    except Exception as e:
        raise OpenRouterError(f"Could not reach OpenRouter: {e}")
    key = data.get("key")
    if not key:
        raise OpenRouterError(f"Unexpected response from OpenRouter: {str(data)[:300]}")
    return key


def save_to_env(key, set_default_llm=True, env_file=ENV_FILE):
    """Updates (or adds) values in .env, preserving every other line. File is created with mode 600."""
    updates = {"OPENROUTER_API_KEY": f"'{key}'"}
    if set_default_llm:
        updates["TUTOR_LLM"] = "openrouter"
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        name = line.split("=", 1)[0]
        if name in updates and "=" in line:
            lines[i] = f"{name}={updates[name]}"
            seen.add(name)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    old_umask = os.umask(0o077)
    try:
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    finally:
        os.umask(old_umask)
    try:
        os.chmod(env_file, 0o600)
    except OSError:
        pass  # e.g. Windows


def main():
    ap = argparse.ArgumentParser(description="Get an OpenRouter API key via OAuth (PKCE).")
    ap.add_argument("--port", type=int, default=3000, help="local callback port (OpenRouter allows localhost:3000 for dev)")
    ap.add_argument("--callback-url", help="override the callback URL (default http://localhost:<port>/)")
    ap.add_argument("--manual", action="store_true", help="don't run a local server; paste the code from the redirect URL")
    ap.add_argument("--no-browser", action="store_true", help="print the sign-in URL instead of opening a browser")
    ap.add_argument("--print-key", action="store_true", help="print only the key to stdout; don't touch .env")
    ap.add_argument("--no-default-llm", action="store_true", help="don't set TUTOR_LLM=openrouter in .env")
    ap.add_argument("--env-file", type=Path, default=ENV_FILE, help="where to save the key (default: .env next to this script)")
    ap.add_argument("--timeout", type=int, default=300, help="seconds to wait for the browser redirect")
    ap.add_argument("--api-base", default=DEFAULT_API_BASE, help=argparse.SUPPRESS)  # for testing against a mock
    args = ap.parse_args()

    callback = args.callback_url or f"http://localhost:{args.port}/"
    cb_path = urllib.parse.urlparse(callback).path or "/"
    verifier, challenge = make_pkce()
    url = build_auth_url(callback, challenge)

    log("Sign in to OpenRouter to authorize the tutor.")
    if args.no_browser or args.manual:
        log("\nOpen this URL in your browser:\n\n  " + url + "\n")
    else:
        log("Opening your browser... (if nothing opens, use this URL)\n\n  " + url + "\n")
        webbrowser.open(url)

    if args.manual:
        log("After you click Authorize, the browser is redirected to a URL like  " + callback + "?code=XXXX")
        log("(the page itself may fail to load; that's fine). Copy the value after 'code='.")
        code = input("Paste the code (or the whole redirect URL): ").strip()
        if "code=" in code:
            code = (urllib.parse.parse_qs(urllib.parse.urlparse(code).query).get("code") or [""])[0]
        if not code:
            raise SystemExit("No code entered.")
    else:
        log(f"Waiting for the redirect on {callback} ...")
        code = wait_for_code(args.port, cb_path, args.timeout)

    try:
        key = exchange_code(code, verifier, args.api_base)
    except OpenRouterError as e:
        raise SystemExit(str(e))
    if args.print_key:
        print(key)
        return
    save_to_env(key, set_default_llm=not args.no_default_llm, env_file=args.env_file)
    log(f"\nSuccess. Saved OPENROUTER_API_KEY to {args.env_file}" + ("" if args.no_default_llm else " and set TUTOR_LLM=openrouter") + ".")
    log("Optional: set TUTOR_MODEL in .env to choose a model (default anthropic/claude-sonnet-4.5).")


if __name__ == "__main__":
    main()
