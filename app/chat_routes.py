"""Chat socket lifecycle: independent reader/writer, bounded waits, no shared I/O lock."""
from __future__ import annotations

import asyncio
import contextlib
import json
import time

from fastapi import WebSocket, WebSocketDisconnect

from .chat import ChatError, ChatHub, ID, NOTICE_FEATURE
from .security import Budget, read_token


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ChatError("invalid_json")
        result[key] = value
    return result


def install_chat(app, settings, store, lobby, client_key):
    hub = ChatHub(max_sessions=min(128, max(1, settings.max_chat_sockets)))
    app.state.chat = hub
    operations = Budget(30, 5)
    handshakes = Budget(10, 1)

    @app.websocket("/v1/chat/ws")
    async def chat_ws(ws: WebSocket):
        if not settings.chat_enabled or not handshakes.spend(client_key(ws, settings)):
            await ws.close(code=1013)
            return
        # New protocol has no query-token compatibility path.
        header = ws.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        claims = read_token(settings.secret, token)
        try:
            user = store.user_by_id(int(claims["sub"])) if claims else None
            expires = float(claims["exp"]) if claims else 0
        except (ValueError, TypeError, KeyError, OverflowError):
            user, expires = None, 0
        if user is None or expires <= time.time():
            await ws.close(code=1008)
            return
        await ws.accept()
        # NOTICE155: a client that can decode `notice` events says so in this header; an
        # older client never sees one (it would treat the unknown type as a protocol error).
        features = {f.strip() for f in ws.headers.get("x-acww-chat-features", "").split(",")}
        try:
            session = hub.attach(int(user["id"]), user["username"],
                                 notices=NOTICE_FEATURE in features)
        except ChatError:
            await ws.close(code=1013)
            return

        async def write():
            while hub.current(session):
                event = await session.events.get()
                await asyncio.wait_for(ws.send_text(json.dumps(event, ensure_ascii=False)), 5)

        async def read():
            while hub.current(session):
                remaining = expires - time.time()
                if remaining <= 0:
                    hub.close(session, "token_expired")
                    return
                try:
                    packet = await asyncio.wait_for(ws.receive(), min(20, remaining))
                except asyncio.TimeoutError:
                    hub.sweep()
                    if expires <= time.time():
                        hub.close(session, "token_expired")
                    else:
                        hub.emit(session, {"v": 1, "t": "ping"})
                    continue
                if packet["type"] == "websocket.disconnect":
                    return
                if expires <= time.time():
                    hub.close(session, "token_expired")
                    return
                raw = packet.get("text")
                if raw is None or len(raw) > 4096 or len(raw.encode("utf-8")) > 4096:
                    hub.close(session, "invalid_envelope")
                    return
                if not operations.spend(str(session.user_id)):
                    hub.close(session, "rate_limited")
                    return
                msg = None
                try:
                    msg = json.loads(raw, object_pairs_hook=unique_object)
                    if not isinstance(msg, dict) or type(msg.get("v")) is not int or msg["v"] != 1:
                        raise ChatError("unsupported_version")
                    hub.touch(session)
                    kind = msg.get("t")
                    if kind == "send":
                        # Identity fields are never read from the envelope.
                        hub.send(session, msg.get("id"), msg.get("text"))
                    elif kind == "presence":
                        towns = await lobby.public_town_snapshot() if msg.get("snapshot") is None else []
                        hub.presence(session, towns, snapshot_id=msg.get("snapshot"), page=msg.get("page", 0))
                    elif kind == "resume":
                        hub.resume(session, msg.get("epoch"), msg.get("seq"))
                    elif kind == "activity":
                        hub.activity(session, msg.get("kind"), msg.get("bells"), msg.get("npc"))
                    elif kind == "ping":
                        hub.emit(session, {"v": 1, "t": "pong"})
                    elif kind == "pong":
                        pass
                    else:
                        raise ChatError("unknown_type")
                except (json.JSONDecodeError, RecursionError, ChatError) as error:
                    response = {"v": 1, "t": "error", "code":
                                error.code if isinstance(error, ChatError) else "invalid_json"}
                    if isinstance(msg, dict) and isinstance(msg.get("id"), str) and ID.fullmatch(msg["id"]):
                        response["id"] = msg["id"]
                    hub.emit(session, response)

        tasks = [asyncio.create_task(write()), asyncio.create_task(read()),
                 asyncio.create_task(session.closed.wait())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                # Consume failures so a broken writer closes this session rather
                # than becoming an unobserved task or affecting another reader.
                task.result()
        except (WebSocketDisconnect, ConnectionError, RuntimeError, asyncio.TimeoutError):
            pass
        finally:
            hub.close(session, session.close_reason or "disconnected")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(code=1008 if session.close_reason == "token_expired" else 1000), 2)

    return hub
