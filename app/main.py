"""The ACWW online service: accounts, cloud saves, the lobby and the WM relay.

The contract is `docs/kb/hybrid/online-spec.md` and `server/API.md` lists the places this
implementation had to decide something the spec left open.  One process, one port, one
SQLite file, a directory of save images; TLS and the public name are the NAS reverse
proxy's job, which is why nothing here speaks HTTPS.

`uvicorn app.main:app` finds `app` through the module `__getattr__` at the bottom, so
importing this module does NOT build a service or touch `/data` -- the tests construct
their own `Settings` and call `create_app()` several times in one process.
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import logging_ as log
from .config import SERVICE_VERSION, Settings
from .lobby import LobbyState
from .savecheck import SaveRejected, sha256_hex, validate_card_image
from .security import (HASHER, RateLimiter, hash_password, make_token, needs_rehash,
                       read_token, verify_password)
from .store import Store

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,24}$")
MIN_PASSWORD = 8
WS_POLICY_VIOLATION = 1008

# The lobby's own vocabulary. `mode` decides the WM role at match time (lobby.py).
MODES = ("host", "guest")


# --------------------------------------------------------------------- helpers

def _client_key(request: Request, settings: Settings) -> str:
    """The rate limiter's bucket. Behind the NAS proxy every peer is the proxy, so the
    forwarded header is honoured only when the operator says the proxy is really there --
    otherwise a directly exposed server would let anyone spoof their way out of the
    limit by inventing a header."""
    if settings.trust_proxy:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _etag(version: int) -> str:
    return '"%d"' % version


def _parse_if_match(raw: str | None) -> int | None:
    """`"7"`, `W/"7"` and `7` all mean version 7. Anything else means "no expectation"
    rather than an error, except `*`, which means "there must be one" and is handled by
    the caller."""
    if raw is None:
        return None
    token = raw.strip()
    if token.startswith("W/"):
        token = token[2:].strip()
    token = token.strip('"')
    if token == "*":
        return -1
    try:
        return int(token)
    except ValueError:
        return None


def _save_summary(row: Any) -> dict | None:
    if row is None:
        return None
    return {
        "version": int(row["version"]),
        "size": int(row["size"]),
        "sha256": row["sha256"],
        "updated_utc": row["updated_utc"],
    }


# ------------------------------------------------------------------- the app

def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.db_path, settings.saves_dir, settings.save_history)
    limiter = RateLimiter(settings.auth_rate_limit, settings.auth_rate_window)
    lobby = LobbyState()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.event("service.start", version=SERVICE_VERSION, data_dir=str(settings.data_dir),
                  allow_register=settings.allow_register, hasher=HASHER,
                  users=store.user_count(), save_history=settings.save_history,
                  trust_proxy=settings.trust_proxy)
        if settings.secret_was_generated:
            log.event("secret.generated", level="warn",
                      path=str(settings.data_dir / "secret.key"),
                      note="ACWW_SERVER_SECRET was unset; a random HS256 secret was "
                           "generated and persisted. Back this file up with the data "
                           "volume, or set the variable, or every token is invalidated "
                           "the day the file is lost.")
        yield
        store.close()
        log.event("service.stop")

    app = FastAPI(title="ACWW online", version=SERVICE_VERSION, docs_url=None,
                  redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.limiter = limiter
    app.state.lobby = lobby

    # ------------------------------------------------------------------ auth

    def _identify(authorization: str | None) -> Any:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="a bearer token is required")
        claims = read_token(settings.secret, authorization.split(None, 1)[1].strip())
        if claims is None:
            raise HTTPException(status_code=401, detail="the token is invalid or expired")
        row = store.user_by_id(int(claims.get("sub", -1)))
        if row is None:
            raise HTTPException(status_code=401, detail="the account no longer exists")
        return row

    async def _read_credentials(request: Request) -> tuple[str, str]:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="a JSON body is required")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="a JSON object is required")
        username = body.get("username")
        password = body.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise HTTPException(status_code=400,
                                detail="username and password must both be strings")
        return username, password

    def _spend_attempt(request: Request, endpoint: str) -> None:
        key = _client_key(request, settings)
        if not limiter.allow(key):
            log.event("auth.rate_limited", level="warn", endpoint=endpoint, ip=key)
            raise HTTPException(
                status_code=429,
                detail="too many authentication attempts; %d per %d seconds"
                       % (settings.auth_rate_limit, settings.auth_rate_window),
            )

    @app.post("/v1/auth/register")
    async def register(request: Request) -> JSONResponse:
        _spend_attempt(request, "register")
        if not settings.allow_register:
            raise HTTPException(status_code=403, detail="registration is closed on this server")
        username, password = await _read_credentials(request)
        if not USERNAME_RE.match(username):
            raise HTTPException(status_code=400,
                                detail="username must be 3..24 of [A-Za-z0-9_]")
        if len(password) < MIN_PASSWORD:
            raise HTTPException(status_code=400,
                                detail="password must be at least %d characters" % MIN_PASSWORD)
        digest = await run_in_threadpool(hash_password, password)
        user_id = await run_in_threadpool(store.create_user, username, digest)
        if user_id is None:
            log.event("auth.register_conflict", username=username)
            raise HTTPException(status_code=409, detail="that username is taken")
        log.event("auth.register", user_id=user_id, username=username, hasher=HASHER)
        return JSONResponse({"token": make_token(settings.secret, user_id, username,
                                                 settings.token_days),
                             "user_id": user_id})

    @app.post("/v1/auth/login")
    async def login(request: Request) -> JSONResponse:
        _spend_attempt(request, "login")
        username, password = await _read_credentials(request)
        row = await run_in_threadpool(store.user_by_name, username)
        ok = False
        if row is not None:
            ok = await run_in_threadpool(verify_password, row["password_hash"], password)
        if not ok:
            log.event("auth.login_failed", level="warn", username=username,
                      ip=_client_key(request, settings))
            raise HTTPException(status_code=401, detail="wrong username or password")
        if needs_rehash(row["password_hash"]):
            digest = await run_in_threadpool(hash_password, password)
            await run_in_threadpool(store.set_password_hash, int(row["id"]), digest)
            log.event("auth.rehash", user_id=int(row["id"]), hasher=HASHER)
        log.event("auth.login", user_id=int(row["id"]), username=username)
        return JSONResponse({"token": make_token(settings.secret, int(row["id"]),
                                                 row["username"], settings.token_days),
                             "user_id": int(row["id"])})

    @app.get("/v1/me")
    async def me(authorization: str | None = Header(default=None)) -> JSONResponse:
        user = _identify(authorization)
        latest = store.latest_version(int(user["id"]))
        return JSONResponse({"user_id": int(user["id"]), "username": user["username"],
                             "save": _save_summary(latest)})

    # ----------------------------------------------------------------- saves

    @app.get("/v1/save")
    async def get_save(authorization: str | None = Header(default=None)) -> Response:
        user = _identify(authorization)
        latest = store.latest_version(int(user["id"]))
        if latest is None:
            raise HTTPException(status_code=404, detail="this account has no save yet")
        data = await run_in_threadpool(store.read_save, int(user["id"]),
                                       int(latest["version"]))
        if data is None:
            log.event("save.file_missing", level="error", user_id=int(user["id"]),
                      version=int(latest["version"]))
            raise HTTPException(status_code=500, detail="the stored save file is missing")
        log.event("save.get", user_id=int(user["id"]), version=int(latest["version"]),
                  size=len(data), sha256=latest["sha256"])
        return Response(content=data, media_type="application/octet-stream",
                        headers={"ETag": _etag(int(latest["version"])),
                                 "X-Save-Sha256": latest["sha256"]})

    @app.put("/v1/save")
    async def put_save(request: Request,
                       authorization: str | None = Header(default=None),
                       if_match: str | None = Header(default=None)) -> JSONResponse:
        user = _identify(authorization)
        user_id = int(user["id"])
        data = await request.body()
        try:
            validate_card_image(data)
        except SaveRejected as bad:
            # The length is logged; the bytes never are.
            log.event("save.rejected", level="warn", user_id=user_id, size=len(data),
                      reason=bad.reason)
            raise HTTPException(status_code=400, detail=bad.reason)

        expect = _parse_if_match(if_match)
        latest = store.latest_version(user_id)
        current = int(latest["version"]) if latest else 0
        if expect == -1:                       # If-Match: *
            if latest is None:
                raise HTTPException(status_code=412, detail="there is no save to match")
            expect = current
        if expect is not None and expect != current:
            log.event("save.stale", level="warn", user_id=user_id, expected=expect,
                      current=current)
            raise HTTPException(
                status_code=412,
                detail="If-Match is version %d but the server holds version %d; "
                       "re-read /v1/save before writing" % (expect, current))

        digest = sha256_hex(data)
        result = await run_in_threadpool(store.put_save, user_id, data, digest, expect)
        if result is None:                     # lost the race inside the store's lock
            raise HTTPException(status_code=412,
                                detail="another client wrote a newer version; re-read /v1/save")
        version, stamp = result
        log.event("save.put", user_id=user_id, version=version, size=len(data),
                  sha256=digest, if_match=expect)
        return JSONResponse({"version": version, "sha256": digest, "updated_utc": stamp},
                            headers={"ETag": _etag(version)})

    @app.get("/v1/save/history")
    async def save_history(authorization: str | None = Header(default=None)) -> JSONResponse:
        user = _identify(authorization)
        rows = store.history_rows(int(user["id"]))
        return JSONResponse([_save_summary(r) for r in rows])

    @app.get("/v1/save/{version}")
    async def get_save_version(version: int,
                               authorization: str | None = Header(default=None)) -> Response:
        user = _identify(authorization)
        row = store.version_row(int(user["id"]), version)
        if row is None:
            raise HTTPException(status_code=404, detail="no such version")
        data = await run_in_threadpool(store.read_save, int(user["id"]), version)
        if data is None:
            raise HTTPException(status_code=500, detail="the stored save file is missing")
        log.event("save.get_version", user_id=int(user["id"]), version=version,
                  size=len(data))
        return Response(content=data, media_type="application/octet-stream",
                        headers={"ETag": _etag(version), "X-Save-Sha256": row["sha256"]})

    # ----------------------------------------------------------------- lobby

    @app.get("/v1/lobby")
    async def lobby_list(authorization: str | None = Header(default=None)) -> JSONResponse:
        _identify(authorization)
        return JSONResponse(lobby.waiting_list())

    async def _send(socket: Any, payload: dict) -> None:
        """A push to a socket that has already gone is not an error here."""
        if socket is None:
            return
        try:
            await socket.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass

    async def _broadcast_list() -> None:
        payload = {"t": "list", "users": lobby.waiting_list()}
        for uid in lobby.connected_ids():                # a snapshot; sends may fail
            await _send(lobby.socket_of(uid), payload)

    @app.websocket("/v1/lobby/ws")
    async def lobby_ws(websocket: WebSocket, token: str = "") -> None:
        claims = read_token(settings.secret, token)
        user = store.user_by_id(int(claims["sub"])) if claims else None
        if user is None:
            await websocket.close(code=WS_POLICY_VIOLATION)
            return
        user_id, username = int(user["id"]), user["username"]
        await websocket.accept()
        displaced = await lobby.attach(user_id, websocket)
        if displaced is not None:
            await _send(displaced, {"t": "error", "msg": "this account opened another lobby "
                                                         "connection"})
            try:
                await displaced.close(code=WS_POLICY_VIOLATION)
            except Exception:
                pass
        log.event("lobby.open", user_id=user_id, username=username)
        await _send(websocket, {"t": "list", "users": lobby.waiting_list()})

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        raise ValueError
                except Exception:
                    await _send(websocket, {"t": "error", "msg": "a JSON object was expected"})
                    continue
                await _handle_lobby(websocket, user_id, username, msg)
        except WebSocketDisconnect:
            pass
        except Exception as exc:                              # pragma: no cover
            log.event("lobby.error", level="warn", user_id=user_id, error=repr(exc))
        finally:
            changed = await lobby.detach(user_id, websocket)
            log.event("lobby.close", user_id=user_id)
            if changed:
                await _broadcast_list()

    async def _handle_lobby(websocket: WebSocket, user_id: int, username: str,
                            msg: dict) -> None:
        kind = msg.get("t")
        if kind == "ping":
            await _send(websocket, {"t": "pong"})
            return
        if kind == "wait":
            mode = msg.get("mode")
            if mode not in MODES:
                await _send(websocket, {"t": "error", "msg": 'mode must be "host" or "guest"'})
                return
            town = msg.get("town_name") or ""
            if not isinstance(town, str) or len(town) > 32:
                await _send(websocket, {"t": "error", "msg": "town_name must be a short string"})
                return
            await lobby.wait(user_id, username, town, mode, websocket)
            log.event("lobby.wait", user_id=user_id, mode=mode)
            await _broadcast_list()
            return
        if kind == "leave":
            if await lobby.leave(user_id):
                log.event("lobby.leave", user_id=user_id)
                await _broadcast_list()
            return
        if kind == "invite":
            target = msg.get("to")
            if not isinstance(target, int) or target == user_id:
                await _send(websocket, {"t": "error", "msg": "invite needs another user's id"})
                return
            me_w, them_w = lobby.waiter(user_id), lobby.waiter(target)
            if me_w is None:
                await _send(websocket, {"t": "error",
                                        "msg": "say wait before inviting"})
                return
            if them_w is None:
                await _send(websocket, {"t": "error", "msg": "that player is not waiting"})
                return
            log.event("lobby.invite", user_id=user_id, to=target)
            await _send(lobby.socket_of(target), {"t": "invite", "from": me_w.public()})
            return
        if kind == "decline":
            origin = msg.get("from")
            if not isinstance(origin, int):
                await _send(websocket, {"t": "error", "msg": "decline needs the inviter's id"})
                return
            log.event("lobby.decline", user_id=user_id, inviter=origin)
            await _send(lobby.socket_of(origin), {"t": "decline", "from": user_id})
            return
        if kind == "accept":
            origin = msg.get("from")
            if not isinstance(origin, int) or origin == user_id:
                await _send(websocket, {"t": "error", "msg": "accept needs the inviter's id"})
                return
            inviter_w, me_w = lobby.waiter(origin), lobby.waiter(user_id)
            if inviter_w is None or me_w is None:
                await _send(websocket, {"t": "error",
                                        "msg": "that invite is no longer valid"})
                return
            inviter_pub, me_pub = inviter_w.public(), me_w.public()
            inviter_socket, my_socket = lobby.socket_of(origin), websocket
            room = await lobby.match(origin, user_id)
            if room is None:
                await _send(websocket, {"t": "error", "msg": "that invite is no longer valid"})
                return
            by_id = {origin: (inviter_pub, inviter_socket), user_id: (me_pub, my_socket)}
            parent_pub, parent_sock = by_id[room.parent_id]
            child_pub, child_sock = by_id[room.child_id]
            log.event("lobby.matched", room=room.room_id, parent=room.parent_id,
                      child=room.child_id)
            await _send(parent_sock, {"t": "matched", "room": room.room_id,
                                      "peer": child_pub, "role": "parent"})
            await _send(child_sock, {"t": "matched", "room": room.room_id,
                                     "peer": parent_pub, "role": "child"})
            await _broadcast_list()
            return
        await _send(websocket, {"t": "error", "msg": "unknown message type %r" % (kind,)})

    # ----------------------------------------------------------------- relay

    @app.websocket("/v1/relay/{room}")
    async def relay_ws(websocket: WebSocket, room: int, token: str = "") -> None:
        claims = read_token(settings.secret, token)
        user = store.user_by_id(int(claims["sub"])) if claims else None
        if user is None:
            await websocket.close(code=WS_POLICY_VIOLATION)
            return
        user_id = int(user["id"])
        await websocket.accept()
        joined = await lobby.join_room(room, user_id, websocket)
        if joined is None:
            await _send(websocket, {"t": "error", "msg": "no such room, or you are not in it"})
            await websocket.close(code=WS_POLICY_VIOLATION)
            return
        peer_id = joined.peer_of(user_id)
        log.event("relay.open", room=room, user_id=user_id, peer=peer_id)
        frames = 0
        try:
            while True:
                packet = await websocket.receive()
                if packet.get("type") == "websocket.disconnect":
                    raise WebSocketDisconnect(packet.get("code", 1000))
                if packet.get("bytes") is not None:
                    payload = packet["bytes"]
                    peer = joined.sockets.get(peer_id)
                    if peer is not None:
                        try:
                            # Verbatim. The server never reads kind, port or length.
                            await peer.send_bytes(payload)
                            frames += 1
                        except Exception:
                            pass
                    continue
                text = packet.get("text")
                if text is None:
                    continue
                try:
                    msg = json.loads(text)
                except Exception:
                    continue
                if isinstance(msg, dict) and msg.get("t") == "ping":
                    await _send(websocket, {"t": "pong"})
        except WebSocketDisconnect:
            pass
        except Exception as exc:                              # pragma: no cover
            log.event("relay.error", level="warn", room=room, user_id=user_id,
                      error=repr(exc))
        finally:
            survivor = await lobby.leave_room(room, user_id, websocket)
            log.event("relay.close", room=room, user_id=user_id, frames_forwarded=frames)
            if survivor is not None:
                await _send(survivor, {"t": "peer_left"})
                try:
                    await survivor.close(code=1000)
                except Exception:
                    pass

    # ---------------------------------------------------------------- health

    @app.get("/v1/health")
    async def health() -> JSONResponse:
        waiting, rooms = lobby.counts()
        return JSONResponse({"ok": True, "version": SERVICE_VERSION,
                             "users": store.user_count(),
                             "waiting": waiting, "rooms": rooms})

    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    """`uvicorn app.main:app` builds the service here, and only here -- importing this
    module for `create_app` must not create `/data` or open a database."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
