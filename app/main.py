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

import asyncio
import json
import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import logging_ as log
from .config import SERVICE_VERSION, Settings
from .lobby import LobbyState, Refused, RelayLeaveResult
from .savecheck import FLASH_SIZE as SAVE_BYTES
from .savecheck import SaveRejected, sha256_hex, validate_card_image
from .security import (HASHER, Budget, RateLimiter, hash_password, is_trusted_proxy,
                       make_token, needs_rehash, read_token, verify_password)
from .store import Store

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,24}$")
MIN_PASSWORD = 8
WS_POLICY_VIOLATION = 1008
WS_MESSAGE_TOO_BIG = 1009          # RFC 6455's own code for "that frame was too large"
WS_TRY_AGAIN_LATER = 1013
MAX_REQUEST_ID = 64                 # lobby correlation is bounded before any state change
LOBBY_CAPABILITIES = ("cancel", "invite_id", "command_request_id", "abort_room",
                      "multi_room_v1")
REQUEST_ID_ERROR = ("request_id must be a non-empty string of at most %d characters"
                    % MAX_REQUEST_ID)

# The lobby's own vocabulary. `mode` decides the WM role at match time (lobby.py).
MODES = ("host", "guest")

# F7. `_parse_if_match` has to say three different things and `None` was two of them.
IF_MATCH_ABSENT = "absent"
IF_MATCH_INVALID = "invalid"
IF_MATCH_ANY = "any"


# --------------------------------------------------------------------- helpers

def _peer_host(request_or_ws: Any) -> str:
    client = getattr(request_or_ws, "client", None)
    return client.host if client else "unknown"


def _client_key(request_or_ws: Any, settings: Settings) -> str:
    """The rate limiter's bucket: the address this request really came from.

    F1.  This used to read `X-Forwarded-For`'s FIRST element whenever `ACWW_TRUST_PROXY=1`,
    and the documented nginx line is `proxy_set_header X-Forwarded-For
    $proxy_add_x_forwarded_for`, which PRESERVES whatever the caller sent and appends the
    real client.  So the first element was attacker-chosen: a new invented prefix bought a
    fresh unauthenticated login budget, and repeating somebody else's prefix spent theirs.

    The rule now is the only one that is sound with an appending proxy:

    * the TRANSPORT peer must itself be one of `ACWW_TRUSTED_PROXIES` -- a header from
      anyone else is not evidence about anything and is ignored entirely;
    * then the chain is walked from the RIGHT, because a trusted proxy appends, and the
      right-hand end is therefore the part the proxy wrote rather than the part the caller
      did.  The first hop from the right that is not itself a trusted proxy is the client.

    `ACWW_TRUSTED_PROXIES` is empty by default, so out of the box the header does nothing at
    all.  A single-proxy NAS that would rather not think about chains should make its proxy
    OVERWRITE the header (`proxy_set_header X-Forwarded-For $remote_addr`); that shape is
    correct under this rule too, since the overwritten value is the only untrusted hop.
    """
    peer = _peer_host(request_or_ws)
    specs = settings.trusted_proxies
    if not specs or not is_trusted_proxy(peer, specs):
        return peer
    fwd = request_or_ws.headers.get("x-forwarded-for")
    if not fwd:
        return peer
    hops = [h.strip() for h in fwd.split(",") if h.strip()]
    for hop in reversed(hops):
        if not is_trusted_proxy(hop, specs):
            return hop
    return peer


def _etag(version: int) -> str:
    return '"%d"' % version


def _parse_if_match(raw: str | None) -> int | str:
    """An int version, or one of the three IF_MATCH_* words.

    F7.  `"7"`, `W/"7"` and `7` all mean version 7 and `*` means "there must be one".
    Anything else -- a tag LIST like `"0", "999"`, a garbage tag, an empty header -- used to
    return `None`, which was the same value as "no header at all", so an unsupported
    precondition SILENTLY became an unconditional write and could overwrite a newer save.
    It is now IF_MATCH_INVALID and the caller refuses the request instead of guessing.
    """
    if raw is None:
        return IF_MATCH_ABSENT
    token = raw.strip()
    if not token:
        return IF_MATCH_INVALID
    if token == "*":
        return IF_MATCH_ANY
    if token.startswith("W/"):
        token = token[2:].strip()
    if token.startswith('"'):
        if not token.endswith('"') or len(token) < 2:
            return IF_MATCH_INVALID
        token = token[1:-1]
    elif '"' in token or "," in token:
        return IF_MATCH_INVALID
    if "," in token or '"' in token:
        return IF_MATCH_INVALID
    try:
        return int(token)
    except ValueError:
        return IF_MATCH_INVALID


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
    # F2. Before anything can log a path: a filter on uvicorn's own loggers, which is where
    # a websocket handshake's query string -- and the token in it -- used to be printed.
    log.install_query_redaction()
    store = Store(settings.db_path, settings.saves_dir, settings.save_history)
    limiter = RateLimiter(settings.auth_rate_limit, settings.auth_rate_window,
                          settings.limiter_max_keys)
    lobby = LobbyState(settings.max_lobby_sockets, settings.max_rooms, settings.invite_ttl)
    # F5. Four buckets, because the four kinds of traffic have four different natural rates:
    # a save is a handful of big PUTs a day, a lobby message is a keypress, a relay frame is
    # one WM packet per 1/60 s per side. Keyed by account where there is one and by client
    # address where there is not, and BOTH where there is (an account cannot be made cheaper
    # to abuse by rotating addresses, and an address cannot be made cheaper by rotating
    # accounts).
    budgets = {
        "http_ops": Budget(settings.http_ops_burst, settings.http_ops_per_sec),
        "http_bytes": Budget(settings.http_bytes_burst, settings.http_bytes_per_sec),
        "lobby_ops": Budget(settings.lobby_ops_burst, settings.lobby_ops_per_sec),
        "relay_ops": Budget(settings.relay_ops_burst, settings.relay_ops_per_sec),
        "relay_bytes": Budget(settings.relay_bytes_burst, settings.relay_bytes_per_sec),
    }

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.event("service.start", version=SERVICE_VERSION, data_dir=str(settings.data_dir),
                  allow_register=settings.allow_register, hasher=HASHER,
                  users=store.user_count(), save_history=settings.save_history,
                  trusted_proxies=list(settings.trusted_proxies),
                  ws_max_message=settings.ws_max_message,
                  relay_frame_max=settings.relay_frame_max)
        if settings.trust_proxy and not settings.trusted_proxies:
            log.event("proxy.untrusted", level="warn",
                      note="ACWW_TRUST_PROXY is set but ACWW_TRUSTED_PROXIES is empty, so "
                           "X-Forwarded-For is IGNORED and the rate limiter's key is the "
                           "transport peer -- which behind a proxy is the proxy. Set "
                           "ACWW_TRUSTED_PROXIES to the proxy's address (SERVERFIX108 / F1).")
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
    app.state.budgets = budgets

    # ---------------------------------------------------------------- F3 / F5 helpers

    async def _read_bounded(request: Request, limit: int) -> bytes:
        """The body, or 413 -- and the 413 happens BEFORE the bytes are held.

        F3.  `await request.body()` buffers whatever arrives and only then is anything
        checked, so an unauthenticated caller could make the process hold an arbitrary
        amount of memory before being told no.  Two gates: a declared `Content-Length`
        above the limit is refused without reading at all, and the stream is then counted
        chunk by chunk and abandoned the moment it passes the limit -- because a chunked
        body declares no length, and a body that lies about its length is the interesting
        case rather than the exotic one.
        """
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    raise HTTPException(status_code=413,
                                        detail="at most %d bytes here" % limit)
            except ValueError:
                raise HTTPException(status_code=400, detail="Content-Length is not a number")
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise HTTPException(status_code=413, detail="at most %d bytes here" % limit)
            chunks.append(chunk)
        return b"".join(chunks)

    def _spend(name: str, keys: list[str], cost: float = 1.0) -> bool:
        """Spend `cost` from one budget against every key. All-or-nothing is deliberately
        NOT attempted: a refusal costs nothing (security.Budget), so a caller refused on the
        second key has not been charged for the first in any way that accumulates."""
        bucket = budgets[name]
        return all(bucket.spend(key, cost) for key in keys if key)

    def _http_budget(request: Request, user_id: int | None, cost_bytes: int = 0) -> None:
        keys = ["ip:" + _client_key(request, settings)]
        if user_id is not None:
            keys.append("user:%d" % user_id)
        if not _spend("http_ops", keys):
            log.event("budget.http_ops", level="warn", user_id=user_id,
                      path=request.url.path)
            raise HTTPException(status_code=429,
                                detail="too many requests; slow down and retry")
        if cost_bytes and not _spend("http_bytes", keys, float(cost_bytes)):
            log.event("budget.http_bytes", level="warn", user_id=user_id, size=cost_bytes)
            raise HTTPException(status_code=429,
                                detail="too many bytes uploaded recently; retry shortly")

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
        """The two strings, with every size checked before anything expensive happens (F3).

        The order matters and is the finding: the body is bounded before it is buffered, it
        is parsed only once it is known to be small, and the credential LENGTHS are checked
        before `hash_password` -- argon2id over a megabyte of "password" is a CPU denial of
        service that the auth rate limit alone does not close, because ten attempts a minute
        of a hash that takes a second each is still ten seconds of the process a minute from
        one unauthenticated caller.
        """
        raw = await _read_bounded(request, settings.max_auth_body)
        try:
            body = json.loads(raw)
        except Exception:
            raise HTTPException(status_code=400, detail="a JSON body is required")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="a JSON object is required")
        username = body.get("username")
        password = body.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise HTTPException(status_code=400,
                                detail="username and password must both be strings")
        if len(username.encode("utf-8")) > settings.max_username:
            raise HTTPException(status_code=400,
                                detail="username must be at most %d bytes"
                                       % settings.max_username)
        if len(password.encode("utf-8")) > settings.max_password:
            raise HTTPException(status_code=400,
                                detail="password must be at most %d bytes"
                                       % settings.max_password)
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
    async def me(request: Request,
                 authorization: str | None = Header(default=None)) -> JSONResponse:
        user = _identify(authorization)
        _http_budget(request, int(user["id"]))
        latest = store.latest_version(int(user["id"]))
        return JSONResponse({"user_id": int(user["id"]), "username": user["username"],
                             "save": _save_summary(latest)})

    # ----------------------------------------------------------------- saves

    @app.get("/v1/save")
    async def get_save(request: Request,
                       authorization: str | None = Header(default=None)) -> Response:
        user = _identify(authorization)
        _http_budget(request, int(user["id"]))
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
        # F7, BEFORE the body: a precondition this service cannot evaluate is a refusal, not
        # a licence to write unconditionally. 428 is exactly "your request needs a
        # precondition and the one you sent is not one I can apply".
        expect = _parse_if_match(if_match)
        if expect == IF_MATCH_INVALID:
            log.event("save.if_match_unsupported", level="warn", user_id=user_id)
            raise HTTPException(
                status_code=428,
                detail='If-Match must be one exact version -- "7", W/"7" or 7 -- or *; a tag '
                       "list or any other form is refused rather than ignored, because "
                       "ignoring it would overwrite whatever the server holds")
        # F3. The card is exactly 262,144 bytes, so a body above that is refused while it is
        # arriving rather than buffered and measured afterwards. One byte of slack so that
        # 262,145 is still reported by validate_card_image's own "wrong size" message.
        data = await _read_bounded(request, SAVE_BYTES + 1)
        _http_budget(request, user_id, cost_bytes=len(data))
        try:
            validate_card_image(data)
        except SaveRejected as bad:
            # The length is logged; the bytes never are.
            log.event("save.rejected", level="warn", user_id=user_id, size=len(data),
                      reason=bad.reason)
            raise HTTPException(status_code=400, detail=bad.reason)

        latest = store.latest_version(user_id)
        current = int(latest["version"]) if latest else 0
        if expect == IF_MATCH_ANY:             # If-Match: *
            if latest is None:
                raise HTTPException(status_code=412, detail="there is no save to match")
            expect = current
        if expect == IF_MATCH_ABSENT:
            expect = None
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
    async def save_history(request: Request,
                           authorization: str | None = Header(default=None)) -> JSONResponse:
        user = _identify(authorization)
        _http_budget(request, int(user["id"]))
        rows = store.history_rows(int(user["id"]))
        return JSONResponse([_save_summary(r) for r in rows])

    @app.get("/v1/save/{version}")
    async def get_save_version(version: int, request: Request,
                               authorization: str | None = Header(default=None)) -> Response:
        user = _identify(authorization)
        _http_budget(request, int(user["id"]))
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
    async def lobby_list(request: Request,
                         authorization: str | None = Header(default=None)) -> JSONResponse:
        user = _identify(authorization)
        _http_budget(request, int(user["id"]))
        return JSONResponse(lobby.waiting_list())

    async def _send(socket: Any, payload: dict) -> None:
        """A push to a socket that has already gone is not an error here."""
        if socket is None:
            return
        try:
            await socket.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass

    async def _lobby_send(user_id: int, websocket: WebSocket, payload: dict,
                          generation: int | None = None) -> bool:
        """Queue a lobby response only for the socket generation that issued it."""
        return await lobby.send_current(user_id, websocket, payload, generation)

    async def _drain_lobby(session: Any) -> None:
        """Serialize every queued lobby event for one socket generation."""
        while True:
            payload = await lobby.next_session_event(session)
            if payload is None:
                return
            await _send(session.socket, payload)

    # F5. The last list every connected socket was actually sent, as its serialized form.
    # `wait` with unchanged state used to serialize the whole list and push it to every
    # socket -- and log an event -- on every repetition, which made one account's repeated
    # no-op message into work proportional to the number of players connected.
    last_broadcast: dict[str, str | None] = {"json": None}

    async def _broadcast_list() -> bool:
        """Push the waiting list to every lobby socket. False when nothing was sent.

        The list is serialized ONCE and compared with the last one that went out; an
        identical list is not sent again, because a client that already holds it learns
        nothing and a slow reader would be made to hold the fast ones up (F5).  A socket
        that has never seen a list gets one at connect, outside this path.
        """
        payload = {"t": "list", "users": lobby.waiting_list(),
                   "capabilities": list(LOBBY_CAPABILITIES)}
        blob = json.dumps(payload, ensure_ascii=False)
        if blob == last_broadcast["json"]:
            return False
        last_broadcast["json"] = blob
        await lobby.broadcast_list(payload)
        return True

    def _ws_token(websocket: WebSocket, token: str) -> str:
        """The bearer token for a websocket: the `Authorization` header FIRST (F2).

        The native client now authenticates its two sockets with a request header, which
        WinHTTP can set on the upgrade (`port/platform/online.c`, `ws_open`), because a
        query string is copied verbatim into uvicorn's handshake log record and from there
        into every log export and backup -- a reusable 30-day credential sitting somewhere
        nobody thinks of as secret.  `?token=` is still accepted for one release so a client
        that has not been repacked keeps working; it is the compatibility path on both ends
        and the query string is redacted in the logs either way.
        """
        header = websocket.headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            return header.split(None, 1)[1].strip()
        return token

    @app.websocket("/v1/lobby/ws")
    async def lobby_ws(websocket: WebSocket, token: str = "") -> None:
        claims = read_token(settings.secret, _ws_token(websocket, token))
        user = store.user_by_id(int(claims["sub"])) if claims else None
        if user is None:
            await websocket.close(code=WS_POLICY_VIOLATION)
            return
        user_id, username = int(user["id"]), user["username"]
        ip = _client_key(websocket, settings)
        await websocket.accept()
        sender = None
        try:
            displaced, session = await lobby.attach_session(
                user_id, websocket,
                {"t": "list", "users": [],
                 "capabilities": list(LOBBY_CAPABILITIES)},
            )
            sender = asyncio.create_task(_drain_lobby(session))
        except Refused as refused:               # F5: the lobby socket cap
            log.event("lobby.refused", level="warn", user_id=user_id, reason=refused.reason)
            await _send(websocket, {"t": "error", "msg": refused.reason})
            await websocket.close(code=WS_TRY_AGAIN_LATER)
            return
        if displaced is not None:
            await _send(displaced, {"t": "error", "msg": "this account opened another lobby "
                                                         "connection"})
            try:
                await displaced.close(code=WS_POLICY_VIOLATION)
            except Exception:
                pass
        log.event("lobby.open", user_id=user_id, username=username)

        try:
            while True:
                raw = await websocket.receive_text()
                # The socket map is the lobby session's ownership gate.  The mutator checks
                # below close the remaining TOCTOU window when replacement races a command.
                if not await lobby.is_current_socket(user_id, websocket):
                    await websocket.close(code=WS_POLICY_VIOLATION)
                    return
                # F3. The application's own ceiling, under uvicorn's --ws-max-size: a text
                # frame this large is not a lobby message whatever it contains, and 1009 is
                # the code that says so.
                if len(raw) > settings.max_lobby_message:
                    log.event("lobby.oversize", level="warn", user_id=user_id, size=len(raw))
                    await websocket.close(code=WS_MESSAGE_TOO_BIG)
                    return
                # F5. One bucket per account AND one per address.
                if not _spend("lobby_ops", ["user:%d" % user_id, "ip:" + ip]):
                    log.event("budget.lobby_ops", level="warn", user_id=user_id)
                    await _lobby_send(user_id, websocket, {"t": "error",
                                                            "msg": "too many lobby messages; slow down"},
                                      session.generation)
                    await websocket.close(code=WS_TRY_AGAIN_LATER)
                    return
                try:
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        raise ValueError
                except Exception:
                    await _lobby_send(user_id, websocket,
                                      {"t": "error", "msg": "a JSON object was expected"},
                                      session.generation)
                    continue
                await _handle_lobby(websocket, user_id, username, msg, session.generation)
        except WebSocketDisconnect:
            pass
        except Exception as exc:                              # pragma: no cover
            log.event("lobby.error", level="warn", user_id=user_id, error=repr(exc))
        finally:
            changed, _notices = await lobby.detach_with_notices(user_id, websocket)
            log.event("lobby.close", user_id=user_id)
            if changed:
                await _broadcast_list()
            if sender is not None:
                sender.cancel()
                try:
                    await sender
                except BaseException:
                    pass

    async def _handle_lobby(websocket: WebSocket, user_id: int, username: str,
                            msg: dict, generation: int) -> None:
        kind = msg.get("t")
        has_request_id = "request_id" in msg
        request_id = msg.get("request_id")
        request_id_valid = (not has_request_id or
                            (type(request_id) is str and bool(request_id)
                             and len(request_id) <= MAX_REQUEST_ID))

        async def command_error(message: str) -> None:
            payload = {"t": "error", "msg": message}
            # Never reflect an invalid/unbounded request id.  A valid id is copied only after
            # the complete post-parse command object has passed this bound check.
            if has_request_id and request_id_valid:
                payload["request_id"] = request_id
            await _lobby_send(user_id, websocket, payload, generation)

        if not request_id_valid:
            await _lobby_send(user_id, websocket, {"t": "error", "msg": REQUEST_ID_ERROR},
                              generation)
            return
        if kind == "ping":
            await _lobby_send(user_id, websocket, {"t": "pong"}, generation)
            return
        if kind == "wait":
            mode = msg.get("mode")
            if mode not in MODES:
                await command_error('mode must be "host" or "guest"')
                return
            town = msg.get("town_name") or ""
            if not isinstance(town, str) or len(town) > 32:
                await command_error("town_name must be a short string")
                return
            public_open = msg.get("open") is True
            if "open" in msg and type(msg.get("open")) is not bool:
                await command_error("open must be a boolean")
                return
            if public_open and mode != "host":
                await command_error("only a host can open a public town")
                return
            accepted = await lobby.wait(user_id, username, town, mode, websocket,
                                        generation, public_open)
            if not accepted:
                return
            log.event("lobby.wait", user_id=user_id, mode=mode, public_open=public_open)
            await _broadcast_list()
            return
        if kind == "leave":
            result = await lobby.cancel(user_id, socket=websocket, generation=generation)
            if result.waiting_changed:
                log.event("lobby.leave", user_id=user_id)
                await _broadcast_list()
            return
        if kind == "abort_room":
            room_id = msg.get("room")
            if type(room_id) is not int or room_id <= 0 or not has_request_id:
                await command_error("abort_room requires a positive room and request_id")
                return
            sockets = await lobby.abort_room(room_id, user_id, websocket, generation)
            if sockets is None:
                await command_error("that room is no longer owned by this connection")
                return
            # The room is already revoked under the state lock. Relay I/O is outside it;
            # late joins/forwards cannot restore it while the close handshake completes.
            await _lobby_send(user_id, websocket,
                {"t": "room_abort_ack", "room": room_id, "request_id": request_id},
                generation)
            if isinstance(sockets, RelayLeaveResult):
                for relay_socket in sockets.notify_sockets:
                    try:
                        await _send(relay_socket, {"t": "member_left", "room": room_id,
                                                   "aid": sockets.aid})
                    except Exception:
                        pass
                for relay_socket in sockets.close_sockets:
                    try:
                        if sockets.room_closed:
                            await _send(relay_socket, {"t": "peer_left", "room": room_id,
                                                       "aid": sockets.aid,
                                                       "room_closed": True})
                        await relay_socket.close(code=1000)
                    except Exception:
                        pass
                await _broadcast_list()
            else:
                for relay_socket in sockets:
                    try:
                        await _send(relay_socket, {"t": "peer_left"})
                        await relay_socket.close(code=1000)
                    except Exception:
                        pass
            log.event("lobby.room_abort", user_id=user_id, room=room_id)
            return
        if kind == "cancel":
            has_to, has_from = "to" in msg, "from" in msg
            if has_to and has_from:
                await command_error("cancel accepts either to or from, not both")
                return
            target = msg.get("to") if has_to else msg.get("from") if has_from else None
            if (has_to or has_from) and (type(target) is not int or target == user_id):
                await command_error("cancel target must be another user's id")
                return
            has_invite_id = "invite_id" in msg
            raw_invite_id = msg.get("invite_id")
            if has_invite_id and (not has_to and not has_from):
                await command_error("invite_id requires to or from")
                return
            if has_invite_id and (type(raw_invite_id) is not int
                                  or raw_invite_id <= 0):
                await command_error("invite_id must be a positive integer")
                return
            result = await lobby.cancel(
                user_id,
                target_id=target if (has_to or has_from) else None,
                outgoing=True if has_to else False if has_from else None,
                invite_id=raw_invite_id,
                socket=websocket,
                generation=generation,
            )
            payload = {"t": "cancelled", "status": result.status,
                       "waiting": result.waiting, "removed": result.changed}
            if has_request_id:
                payload["request_id"] = request_id
            if has_to:
                payload["to"] = target
            elif has_from:
                payload["from"] = target
            if has_invite_id:
                payload["invite_id"] = raw_invite_id
            if result.room_id is not None:
                payload["room"] = result.room_id
            await _lobby_send(user_id, websocket, payload)
            if result.waiting_changed:
                await _broadcast_list()
            return
        if kind == "invite":
            target = msg.get("to")
            if type(target) is not int or target == user_id:
                await command_error("invite needs another user's id")
                return
            offer = await lobby.create_invite(user_id, target, websocket, generation)
            if offer is None:
                me_w, them_w = lobby.waiter(user_id), lobby.waiter(target)
                if me_w is None:
                    await command_error("say wait before inviting")
                    return
                if them_w is None:
                    await command_error("that player is not waiting")
                    return
                # The waiters can only disappear while the lock is held.  This branch is kept
                # for a defensive response if a future state implementation refuses an invite.
                await command_error("that invitation could not be created")
                return
            log.event("lobby.invite", user_id=user_id, to=target)
            invited = {"t": "invited", "to": target,
                       "invite_id": offer.invitation.invite_id}
            if has_request_id:
                invited["request_id"] = request_id
            await _lobby_send(user_id, websocket, invited)
            return
        if kind == "decline":
            origin = msg.get("from")
            if type(origin) is not int:
                await command_error("decline needs the inviter's id")
                return
            raw_invite_id = msg.get("invite_id")
            has_invite_id = "invite_id" in msg
            if has_invite_id and (type(raw_invite_id) is not int
                                  or raw_invite_id <= 0):
                await command_error("invite_id must be a positive integer")
                return
            # F4. A refused invitation is GONE: a later accept of it must not work.
            invitation_removed = await lobby.withdraw_invite_info(
                origin, user_id, raw_invite_id, websocket, generation)
            log.event("lobby.decline", user_id=user_id, inviter=origin)
            return
        if kind == "accept":
            origin = msg.get("from")
            if type(origin) is not int or origin == user_id:
                await command_error("accept needs the inviter's id")
                return
            raw_invite_id = msg.get("invite_id")
            has_invite_id = "invite_id" in msg
            if has_invite_id and (type(raw_invite_id) is not int
                                  or raw_invite_id <= 0):
                await command_error("invite_id must be a positive integer")
                return
            # F4. `match` consumes the pending directed invitation inside the same lock it
            # creates the room under, and returns None when there is none to consume -- so a
            # bare accept, a replayed accept and a declined one are all the same refusal,
            # and two accepts of one invitation cannot both win.
            try:
                room = await lobby.match(origin, user_id, raw_invite_id, websocket, generation)
            except Refused as refused:           # F5: the room cap
                log.event("lobby.refused", level="warn", user_id=user_id,
                          reason=refused.reason)
                await command_error(refused.reason)
                return
            if room is None:
                await command_error("that invite is no longer valid")
                return
            # Either endpoint may have sent the invitation.  Log the member whose role is
            # actually child rather than assuming the inviter filled that role.
            child_id = user_id if user_id != room.parent_id else origin
            log.event("lobby.matched", room=room.room_id, parent=room.parent_id,
                      child=child_id, players=len(room.members()))
            await _broadcast_list()
            return
        await command_error("unknown message type %r" % (kind,))

    # ----------------------------------------------------------------- relay

    @app.websocket("/v1/relay/{room}")
    async def relay_ws(websocket: WebSocket, room: int, token: str = "") -> None:
        claims = read_token(settings.secret, _ws_token(websocket, token))
        user = store.user_by_id(int(claims["sub"])) if claims else None
        if user is None:
            await websocket.close(code=WS_POLICY_VIOLATION)
            return
        user_id = int(user["id"])
        ip = _client_key(websocket, settings)
        await websocket.accept()
        joined = await lobby.join_room(room, user_id, websocket)
        if joined is None:
            await _send(websocket, {"t": "error", "msg": "no such room, or you are not in it"})
            await websocket.close(code=WS_POLICY_VIOLATION)
            return
        # F6. `generation` is this CONNECTION's right to write for its side of the room, and
        # `displaced` is the socket it took that right from. The old one is told and closed
        # rather than left running: before this it stayed an authorized writer for ever, so
        # a stale client or a stolen token interleaved frames with its own replacement.
        room_state, generation, displaced = joined
        if displaced is not None and displaced is not websocket:
            log.event("relay.replaced", level="warn", room=room, user_id=user_id)
            await _send(displaced, {"t": "error", "msg": "this account opened another relay "
                                                         "connection for this room"})
            try:
                await displaced.close(code=WS_POLICY_VIOLATION)
            except Exception:
                pass
        aid = room_state.aid_of(user_id)
        if room_state.public_open:
            await _send(websocket, {"t": "room_state", "room": room, "aid": aid,
                                    "members": room_state.member_rows(), "max_players": 4})
        log.event("relay.open", room=room, user_id=user_id, aid=aid,
                  generation=generation, players=len(room_state.members()))
        frames = 0
        try:
            while True:
                packet = await websocket.receive()
                if packet.get("type") == "websocket.disconnect":
                    raise WebSocketDisconnect(packet.get("code", 1000))
                if packet.get("bytes") is not None:
                    payload = packet["bytes"]
                    # F3. online-spec.md's frame ceiling, enforced by the server as well as
                    # by the client: the relay is opaque, and an opaque forwarder can still
                    # count bytes without interpreting one.
                    if len(payload) > settings.relay_frame_max:
                        log.event("relay.oversize", level="warn", room=room, user_id=user_id,
                                  size=len(payload))
                        await websocket.close(code=WS_MESSAGE_TOO_BIG)
                        return
                    # MULTI114. The bridge's v3 header names its source AID in byte 63.
                    # Authenticate that one routing field against the relay membership;
                    # the remaining bytes stay opaque game data. Without this check a child
                    # could impersonate another AID inside the parent's aggregated MP cycle.
                    if (room_state.public_open
                            and (len(payload) < 68 or payload[4] != 3
                                 or payload[63] != aid)):
                        log.event("relay.bad_source_aid", level="warn", room=room,
                                  user_id=user_id, aid=aid, size=len(payload))
                        await websocket.close(code=WS_POLICY_VIOLATION)
                        return
                    # F5. Frames and bytes, per account and per address.
                    keys = ["user:%d" % user_id, "ip:" + ip]
                    if not _spend("relay_ops", keys) \
                            or not _spend("relay_bytes", keys, float(len(payload))):
                        log.event("budget.relay", level="warn", room=room, user_id=user_id)
                        await websocket.close(code=WS_TRY_AGAIN_LATER)
                        return
                    # F6. Asked for every frame, under the lobby's lock: a captured socket
                    # reference is exactly what a superseded connection still holds.
                    targets = await lobby.may_forward_many(room, user_id, generation)
                    if targets is None:
                        log.event("relay.superseded", level="warn", room=room,
                                  user_id=user_id, generation=generation)
                        await websocket.close(code=WS_POLICY_VIOLATION)
                        return
                    for peer in targets:
                        try:
                            # Verbatim. The server chooses recipients from authenticated room
                            # membership but never reads kind, port, length or game payload.
                            await peer.send_bytes(payload)
                            frames += 1
                        except Exception:
                            pass
                    continue
                text = packet.get("text")
                if text is None:
                    continue
                if len(text) > settings.max_lobby_message:
                    log.event("relay.oversize", level="warn", room=room, user_id=user_id,
                              size=len(text))
                    await websocket.close(code=WS_MESSAGE_TOO_BIG)
                    return
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
            departure = await lobby.leave_room(room, user_id, websocket)
            log.event("relay.close", room=room, user_id=user_id, frames_forwarded=frames)
            if departure is not None:
                for survivor in departure.notify_sockets:
                    try:
                        await _send(survivor, {"t": "member_left", "room": room,
                                               "aid": departure.aid})
                    except Exception:
                        pass
                for survivor in departure.close_sockets:
                    payload = ({"t": "peer_left", "room": room, "aid": departure.aid,
                                "room_closed": departure.room_closed}
                               if room_state.public_open else {"t": "peer_left"})
                    await _send(survivor, payload)
                    try:
                        await survivor.close(code=1000)
                    except Exception:
                        pass
                await _broadcast_list()

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
