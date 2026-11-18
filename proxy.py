"""Authenticated reverse proxy to each user's JupyterLab (HTTP + WebSocket).

The browser only ever talks to this app under /jupyter/. Which Jupyter it reaches is decided SOLELY by the
signed-in session, never by anything in the URL, so a user cannot address someone else's environment. The
per-user Jupyter token is added here, server-side, and never sent to the browser.
"""
import asyncio
import urllib.parse

import httpx
import websockets
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect

import auth
from sandbox import BASE_PATH, SandboxError

router = APIRouter()
SESSION_COOKIE = "tutor_session"
MAX_BODY = 25 * 1024 * 1024
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length"}

manager = None
allowed_origin = ""
forwarded_proto = "https"
_client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(300, connect=10), follow_redirects=False)


def init(container_manager, base_url):
    global manager, allowed_origin, forwarded_proto
    manager = container_manager
    p = urllib.parse.urlparse(base_url)
    allowed_origin = f"{p.scheme}://{p.netloc}"
    forwarded_proto = p.scheme


def _without_session_cookie(cookie_header):
    kept = [c for c in cookie_header.split(";") if c.strip().split("=", 1)[0] != SESSION_COOKIE]
    return ";".join(kept).strip()


async def _upstream_base(user):
    return await run_in_threadpool(manager.ensure, user["id"], user["jupyter_token"])


@router.api_route("/jupyter/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def http_proxy(path: str, request: Request):
    user = auth.session_user(request)
    if not user:
        return JSONResponse({"detail": "Please sign in."}, status_code=401)
    try:
        base = await _upstream_base(user)
    except SandboxError as e:
        return JSONResponse({"detail": str(e)}, status_code=503)

    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > MAX_BODY:
        return JSONResponse({"detail": "Upload too large."}, status_code=413)
    body = await request.body()
    if len(body) > MAX_BODY:
        return JSONResponse({"detail": "Upload too large."}, status_code=413)

    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    if "cookie" in headers:
        headers["cookie"] = _without_session_cookie(headers["cookie"])
    headers["authorization"] = f"token {user['jupyter_token']}"
    headers["x-forwarded-proto"] = forwarded_proto

    url = f"{base}{BASE_PATH}{path}"
    if request.url.query:
        url += "?" + request.url.query
    manager.touch(user["id"])
    try:
        upstream = await _client.send(_client.build_request(request.method, url, headers=headers, content=body), stream=True)
    except httpx.HTTPError:
        return JSONResponse({"detail": "Your notebook environment is not responding."}, status_code=502)

    resp = StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code, background=BackgroundTask(upstream.aclose))
    raw = []
    for k, v in upstream.headers.multi_items():
        if k.lower() in ("connection", "keep-alive", "transfer-encoding", "upgrade"):
            continue
        if k.lower() == "location" and v.startswith(base):
            v = v[len(base):]  # keep redirects on our own origin
        raw.append((k.lower().encode("latin-1"), v.encode("latin-1")))
    resp.raw_headers = raw
    return resp


@router.websocket("/jupyter/{path:path}")
async def ws_proxy(ws: WebSocket, path: str):
    user = auth.session_user(ws)
    origin = ws.headers.get("origin")
    # Reject unauthenticated sockets and cross-site WebSocket hijacking attempts before accepting.
    if not user or origin != allowed_origin:
        await ws.close(code=4403)
        return
    try:
        base = await _upstream_base(user)
    except SandboxError:
        await ws.close(code=1013)
        return

    uri = "ws" + base[len("http"):] + f"{BASE_PATH}{path}"
    if ws.url.query:
        uri += "?" + ws.url.query
    try:
        upstream = await ws_connect(
            uri, additional_headers={"Authorization": f"token {user['jupyter_token']}"},
            subprotocols=ws.scope.get("subprotocols") or None, origin=origin,
            max_size=None, open_timeout=15, proxy=None)
    except Exception:
        await ws.close(code=1011)
        return

    await ws.accept(subprotocol=upstream.subprotocol)
    manager.ws_opened(user["id"])

    async def client_to_upstream():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("text") is not None:
                await upstream.send(msg["text"])
            elif msg.get("bytes") is not None:
                await upstream.send(msg["bytes"])

    async def upstream_to_client():
        async for m in upstream:
            if isinstance(m, str):
                await ws.send_text(m)
            else:
                await ws.send_bytes(m)

    tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        manager.ws_closed(user["id"])
        await upstream.close()
        try:
            await ws.close()
        except (RuntimeError, WebSocketDisconnect):
            pass
