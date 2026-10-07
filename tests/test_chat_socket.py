"""Real loopback sockets: auth, cross-town delivery, identity and independent lifetime."""
import json
import time

import httpx
import jwt
import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from conftest import make_server
from test_lobby import account


def open_chat(server, token):
    return connect(server.ws_base + "/v1/chat/ws", open_timeout=5,
                   additional_headers={"Authorization": "Bearer " + token})


def recv(ws):
    return json.loads(ws.recv(timeout=5))


def send(ws, **payload):
    ws.send(json.dumps({"v": 1, **payload}))


def test_two_users_chat_without_lobby_or_room_and_presence_is_private(server):
    ta, aid = account(server, "alpha")
    tb, bid = account(server, "bravo")
    with open_chat(server, ta) as a, open_chat(server, tb) as b:
        assert recv(a)["t"] == recv(b)["t"] == "hello"
        send(a, t="send", id="first", text="안녕하세요", user_id=bid, username="spoof")
        accepted, own, remote = recv(a), recv(a), recv(b)
        assert accepted["t"] == "accepted" and own == remote
        assert (remote["user_id"], remote["username"]) == (aid, "alpha")
        send(b, t="send", id="reply", text="hello")
        assert recv(b)["t"] == "accepted"
        assert recv(a) == recv(b)
        send(a, t="presence")
        presence = recv(a)
        assert presence["t"] == "presence_page" and presence["total"] == 2
        assert {row["username"] for row in presence["rows"]} == {"alpha", "bravo"}
        send(b, t="ping")
        assert recv(b)["t"] == "pong"  # no leaked private presence response
        health = httpx.get(server.base + "/v1/health", timeout=5).json()
        assert health["waiting"] == health["rooms"] == 0
        assert "chat_v1" in health["capabilities"]


def test_duplicate_ack_does_not_broadcast_twice_and_malformed_text_is_refused(server):
    token, _ = account(server, "alpha")
    with open_chat(server, token) as a:
        hello = recv(a)
        send(a, t="send", id="one", text="safe")
        ack, message = recv(a), recv(a)
        send(a, t="send", id="one", text="safe")
        assert recv(a) == ack
        send(a, t="send", id="two", text="bad\x1a")
        assert recv(a) == {"v": 1, "t": "error", "code": "invalid_text", "id": "two"}
        send(a, t="resume", epoch=hello["epoch"], seq=0)
        assert recv(a) == message
        assert recv(a)["t"] == "resumed"


def test_replacing_chat_closes_only_old_chat(server):
    token, _ = account(server, "alpha")
    with connect(server.ws_base + "/v1/lobby/ws",
                 additional_headers={"Authorization": "Bearer " + token}) as lobby:
        assert recv(lobby)["t"] == "list"
        with open_chat(server, token) as old:
            recv(old)
            with open_chat(server, token) as new:
                recv(new)
                with pytest.raises(ConnectionClosed):
                    old.recv(timeout=5)
                send(new, t="ping")
                assert recv(new)["t"] == "pong"
                lobby.send(json.dumps({"t": "ping"}))
                assert recv(lobby)["t"] == "pong"


def test_chat_requires_header_and_rejects_binary_or_oversized_envelopes(server):
    token, _ = account(server, "alpha")
    with pytest.raises((ConnectionClosed, InvalidStatus)):
        with connect(server.ws_base + "/v1/chat/ws?token=" + token) as ws:
            ws.recv(timeout=5)
    for message in (b"binary", "x" * 4097):
        with open_chat(server, token) as ws:
            recv(ws)
            ws.send(message)
            with pytest.raises(ConnectionClosed):
                ws.recv(timeout=5)


def test_operator_disable_does_not_disable_accounts(tmp_path):
    server = make_server(tmp_path, chat_enabled=False)
    try:
        token, _ = account(server, "alpha")
        with pytest.raises((ConnectionClosed, InvalidStatus)):
            with open_chat(server, token) as ws:
                ws.recv(timeout=5)
        assert httpx.get(server.base + "/v1/health", timeout=5).json()["capabilities"] == []
    finally:
        server.stop()


def test_socket_expires_without_waiting_for_client_traffic(server):
    _, uid = account(server, "alpha")
    secret = "test-secret-0123456789abcdef0123456789abcdef"
    token = jwt.encode({"sub": str(uid), "exp": time.time() + 1.5}, secret, algorithm="HS256")
    with open_chat(server, token) as ws:
        assert recv(ws)["t"] == "hello"
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=5)


def test_duplicate_fields_and_deep_json_cannot_break_reader(server):
    token, _ = account(server, "alpha")
    with open_chat(server, token) as ws:
        recv(ws)
        for raw, codes in (('{"v":1,"v":1,"t":"ping"}', {"invalid_json"}),
                           ('[' * 1500 + '0' + ']' * 1500, {"invalid_json", "unsupported_version"})):
            ws.send(raw)
            reply = recv(ws)
            assert reply["v"] == 1 and reply["t"] == "error" and reply["code"] in codes
        send(ws, t="ping")
        assert recv(ws)["t"] == "pong"


def test_notices_follow_the_feature_header(server):
    ta, aid = account(server, "alpha")
    tb, _ = account(server, "bravo")
    tc, _ = account(server, "charlie")
    with connect(server.ws_base + "/v1/chat/ws", open_timeout=5,
                 additional_headers={"Authorization": "Bearer " + ta,
                                     "X-ACWW-Chat-Features": "notice_v1"}) as a, \
            open_chat(server, tb) as old:
        assert "notice_v1" in recv(a)["capabilities"]
        assert recv(old)["t"] == "hello"
        assert recv(a)["username"] == "bravo"               # an old client's arrival is news too
        with open_chat(server, tc) as late:                  # an arrival
            recv(late)
            join = recv(a)
            assert (join["t"], join["kind"], join["username"]) == ("notice", "join", "charlie")
            send(a, t="activity", kind="shop_sell", bells=1200, username="spoof")
            sale = recv(a)
            assert (sale["kind"], sale["bells"], sale["user_id"], sale["username"]) == \
                ("shop_sell", 1200, aid, "alpha")
            send(a, t="activity", kind="shop_sell", bells="1200")
            assert recv(a) == {"v": 1, "t": "error", "code": "invalid_activity"}
            send(old, t="ping")
            assert recv(old)["t"] == "pong"                  # the old client saw no notice
