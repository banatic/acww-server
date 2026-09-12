"""SERVERFIX108 / F3: every size is checked BEFORE the allocation it would pay for.

server-audit-1's third finding, in four places:

* an unauthenticated `POST /v1/auth/login` buffered and parsed a body of any size before any
  credential bound existed, and then hashed a password of any length with argon2id -- ten
  attempts a minute of a hash over a megabyte is still ten seconds of the process a minute,
  bought without an account;
* `PUT /v1/save` did `await request.body()` and only then asked whether it was the card's
  262,144 bytes;
* a websocket message had no application ceiling, and uvicorn's own default is 16 MiB;
* the relay had NO server-side frame cap at all -- the audit measured an 8,193-byte message
  arriving unchanged at the paired socket, twice `RELAY_FRAME_MAX`, which the client
  (`port/platform/online.c:1281`) enforces on itself and the server took on trust.

The shape of every test below is the same: the refusal has to be the RIGHT refusal (413 or
1009, not a 400 that happens to also refuse) and nothing may be stored.
"""

from __future__ import annotations

import json
import struct

import httpx
import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from conftest import mutate_save
from test_lobby import Client, account

TIMEOUT = 60
CARD = 262144


def register(server, **body):
    return httpx.post(server.base + "/v1/auth/register", json=body, timeout=TIMEOUT)


def post_raw(server, path, payload: bytes, content_type="application/json"):
    return httpx.post(server.base + path, content=payload,
                      headers={"Content-Type": content_type}, timeout=TIMEOUT)


# ---------------------------------------------------------------- the auth bodies

def test_an_oversized_auth_body_is_refused_before_it_is_parsed(server):
    """413, and a declared Content-Length above the bound is refused without a read."""
    fat = json.dumps({"username": "villager", "password": "x",
                      "padding": "A" * 200000}).encode()
    r = post_raw(server, "/v1/auth/login", fat)
    assert r.status_code == 413, r.text
    assert "bytes" in r.json()["detail"]
    r = post_raw(server, "/v1/auth/register", fat)
    assert r.status_code == 413, r.text
    assert httpx.get(server.base + "/v1/health", timeout=TIMEOUT).json()["users"] == 0


def test_an_oversized_auth_body_is_refused_when_it_lies_about_its_length(server):
    """A chunked body declares no length at all, so the stream itself has to be counted.

    httpx sends `Transfer-Encoding: chunked` for a generator body, which is the case the
    Content-Length gate cannot see and the one an attacker would pick.
    """
    def chunks():
        yield b'{"username":"villager","password":"'
        for _ in range(40):
            yield b"A" * 8192
        yield b'"}'

    r = httpx.post(server.base + "/v1/auth/login", content=chunks(),
                   headers={"Content-Type": "application/json"}, timeout=TIMEOUT)
    assert r.status_code == 413, r.status_code


def test_a_very_long_password_is_refused_before_it_is_hashed(server):
    """The credential bound, which is separate from the body bound: a body can be small and
    still ask for an expensive hash (`{"username":"a","password":"<3 KB>"}`)."""
    r = register(server, username="villager", password="A" * 3000)
    assert r.status_code == 400, r.text
    assert "at most" in r.json()["detail"]
    # And an ordinary password is still accepted, so the bound is a bound and not a wall.
    assert register(server, username="villager", password="a long enough password"
                    ).status_code == 200
    assert httpx.post(server.base + "/v1/auth/login",
                      json={"username": "villager", "password": "A" * 3000},
                      timeout=TIMEOUT).status_code == 400


def test_a_very_long_username_is_refused_before_the_lookup(server):
    """Under the body bound on purpose: 3,000 characters fit in 4,096 bytes of JSON, so the
    credential bound is the gate that answers rather than the body bound -- the two are
    separate checks and a test that let the first one fire would not see the second."""
    r = register(server, username="A" * 3000, password="a long enough password")
    assert r.status_code == 400, r.text
    assert "at most" in r.json()["detail"]


# -------------------------------------------------------------------- the save PUT

@pytest.fixture
def auth(server):
    r = register(server, username="mayor", password="a long enough password")
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_a_save_above_the_card_size_is_refused_while_it_arrives(server, auth):
    """413 rather than 400: the body is abandoned at the limit, not buffered and measured.

    Four megabytes is what the documented `client_max_body_size 4m` would let through, so
    this is the biggest body the supported deployment can actually deliver here.
    """
    headers = dict(auth)
    headers["Content-Type"] = "application/octet-stream"
    r = httpx.put(server.base + "/v1/save", content=b"\x00" * (4 * 1024 * 1024),
                  headers=headers, timeout=TIMEOUT)
    assert r.status_code == 413, r.text
    assert httpx.get(server.base + "/v1/save", headers=auth,
                     timeout=TIMEOUT).status_code == 404

    def chunks():
        for _ in range(64):
            yield b"\x00" * 65536

    r = httpx.put(server.base + "/v1/save", content=chunks(), headers=headers,
                  timeout=TIMEOUT)
    assert r.status_code == 413, r.status_code
    assert httpx.get(server.base + "/v1/save", headers=auth,
                     timeout=TIMEOUT).status_code == 404


def test_the_card_sized_body_still_lands_and_one_byte_over_is_still_400(server, auth,
                                                                        sample_save):
    """M1, the control. A bound that also refused the real thing would pass the test above
    and break every save in the household. 262,145 keeps its OLD answer -- 400 with the
    size in it -- because the client reads that message."""
    headers = dict(auth)
    headers["Content-Type"] = "application/octet-stream"
    assert len(sample_save) == CARD
    assert httpx.put(server.base + "/v1/save", content=sample_save, headers=headers,
                     timeout=TIMEOUT).status_code == 200
    over = httpx.put(server.base + "/v1/save", content=sample_save + b"\x00",
                     headers=headers, timeout=TIMEOUT)
    assert over.status_code == 400, over.text
    assert "262144" in over.json()["detail"]
    assert httpx.put(server.base + "/v1/save", content=mutate_save(sample_save, 44),
                     headers=headers, timeout=TIMEOUT).status_code == 200


# ------------------------------------------------------------------ the websockets

def test_an_oversized_lobby_message_is_closed_1009(server):
    token, _ = account(server, "alpha")
    a = Client(server, token)
    try:
        a.expect("list")
        a.ws.send(json.dumps({"t": "wait", "mode": "host", "town_name": "x",
                              "pad": "A" * 20000}))
        with pytest.raises(ConnectionClosed) as closed:
            for _ in range(4):
                a.recv()
        assert closed.value.rcvd.code == 1009, closed.value.rcvd
    finally:
        a.close()


def test_a_lobby_message_above_the_protocol_ceiling_never_reaches_the_app(server):
    """uvicorn's own `--ws-max-size` is the FIRST gate, and the fixture runs the same 64 KiB
    the Dockerfile passes. A frame this large is refused at the protocol layer, so the
    application never allocates for it -- the close code is 1009 from uvicorn itself."""
    token, _ = account(server, "alpha")
    a = Client(server, token)
    try:
        a.expect("list")
        a.ws.send("A" * (200 * 1024))
        with pytest.raises(ConnectionClosed) as closed:
            for _ in range(4):
                a.recv()
        assert closed.value.rcvd.code == 1009, closed.value.rcvd
    finally:
        a.close()


@pytest.fixture
def matched(server):
    a_token, a_id = account(server, "alpha")
    b_token, b_id = account(server, "bravo")
    a, b = Client(server, a_token), Client(server, b_token)
    a.send(t="wait", mode="host", town_name="Hanabi")
    b.send(t="wait", mode="guest", town_name="Kirie")
    a.expect_list(2)
    a.send(t="invite", to=b_id)
    b.expect("invite")
    b.send(t="accept", **{"from": a_id})
    room = a.expect("matched")["room"]
    assert b.expect("matched")["room"] == room
    try:
        yield server, room, a_token, b_token
    finally:
        a.close()
        b.close()


def relay(server, room, token):
    return connect("%s/v1/relay/%d?token=%s" % (server.ws_base, room, token),
                   open_timeout=TIMEOUT)


def test_a_relay_frame_above_4096_is_refused_and_never_forwarded(matched):
    """The audit's exact measurement, reversed: 8,193 bytes arrived at the peer unchanged.

    The relay stays OPAQUE -- it counts the bytes and reads none of them -- so the frame's
    header is deliberately a valid one. The peer must receive nothing at all.
    """
    server, room, a_token, b_token = matched
    with relay(server, room, b_token) as child:
        parent = relay(server, room, a_token)
        try:
            fat = struct.pack("<BBH", 1, 12, 4096) + b"\xa5" * 8193
            parent.send(fat)
            with pytest.raises(ConnectionClosed) as closed:
                parent.recv(timeout=TIMEOUT)
            assert closed.value.rcvd.code == 1009, closed.value.rcvd
            # The peer never sees the frame. What it DOES see is `peer_left`: the server's
            # 1009 close leaves the room, which is the existing contract for a side going
            # away, so the next message is text and not 8,197 bytes of payload.
            first = child.recv(timeout=TIMEOUT)
            assert isinstance(first, str), "the oversized frame reached the peer"
            assert json.loads(first) == {"t": "peer_left"}
        finally:
            parent.close()


def test_a_frame_at_exactly_4096_still_goes_through(matched):
    """M1's control again. 4,096 is the ceiling online-spec.md names and the client's own
    `RELAY_FRAME_MAX`, so it is the size the game actually sends at its largest."""
    server, room, a_token, b_token = matched
    with relay(server, room, a_token) as parent, relay(server, room, b_token) as child:
        out = struct.pack("<BBH", 1, 12, 4092) + b"\x5a" * 4092
        assert len(out) == 4096
        parent.send(out)
        assert child.recv(timeout=TIMEOUT) == out
