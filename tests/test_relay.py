"""The relay: two matched peers, binary frames forwarded verbatim, and peer_left.

The frames here are the spec's `[u8 kind][u8 port][u16 len][payload]` -- built only to
prove that what goes in comes out unchanged.  The server must not parse them, so the test
deliberately sends a frame whose declared `len` disagrees with the bytes that follow: a
server that validated the payload would drop it, and the test would fail.
"""

from __future__ import annotations

import json
import struct

import httpx
import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from test_lobby import Client, account

TIMEOUT = 30

# A relay the server turns away either never completes the handshake (HTTP 403, when
# the token is unreadable and websocket.accept() is never called) or is accepted and
# then closed 1008 with an {"t":"error"} first -- both are a refusal.
REFUSED = (ConnectionClosed, InvalidStatus, OSError)


def frame(kind: int, port: int, payload: bytes, declared_len: int | None = None) -> bytes:
    return struct.pack("<BBH", kind, port,
                       len(payload) if declared_len is None else declared_len) + payload


@pytest.fixture
def matched(server):
    """Two accounts that have been matched: yields the room and both relay URLs."""
    a_token, a_id = account(server, "alpha")
    b_token, b_id = account(server, "bravo")
    a, b = Client(server, a_token), Client(server, b_token)
    a.send(t="wait", mode="host", town_name="Hanabi")
    b.send(t="wait", mode="guest", town_name="Kirie")
    a.expect_list(2)
    # SERVERFIX108 / F4. This fixture used to accept an invitation that was never sent, and
    # that was not a shortcut -- it was the hole: before the fix the server held no record of
    # an invitation, so `accept` could only check that both parties were waiting and anyone
    # in the list could match with anyone else without their consent. The invite is now part
    # of the setup because it is now part of the protocol.
    a.send(t="invite", to=b_id)
    b.expect("invite")
    b.send(t="accept", **{"from": a_id})
    room = a.expect("matched")["room"]
    assert b.expect("matched")["room"] == room
    try:
        yield server, room, (a_token, a_id), (b_token, b_id)
    finally:
        a.close()
        b.close()


def relay(server, room, token):
    return connect("%s/v1/relay/%d?token=%s" % (server.ws_base, room, token),
                   open_timeout=TIMEOUT)


def test_binary_frames_go_both_ways_verbatim(matched):
    server, room, (a_token, _), (b_token, _) = matched
    with relay(server, room, a_token) as parent, relay(server, room, b_token) as child:
        # kind 3, control: an AID assignment the server knows nothing about.
        out = frame(3, 0, b"\x01\x02\x03")
        parent.send(out)
        assert child.recv(timeout=TIMEOUT) == out

        # kind 1, MP data on logical port 12 -- the town driver's port.
        up = frame(1, 12, bytes(range(256)))
        child.send(up)
        assert parent.recv(timeout=TIMEOUT) == up

        # A frame whose header LIES about its length. The server never parses payloads,
        # so it must arrive exactly as sent.
        lying = frame(2, 13, b"\xff" * 8, declared_len=4091)
        parent.send(lying)
        assert child.recv(timeout=TIMEOUT) == lying

        # Many frames in order.
        sent = [frame(1, 12, bytes([i]) * (i + 1)) for i in range(32)]
        for f in sent:
            parent.send(f)
        assert [child.recv(timeout=TIMEOUT) for _ in sent] == sent

        assert httpx.get(server.base + "/v1/health", timeout=TIMEOUT).json()["rooms"] == 1


def test_text_ping_gets_a_pong_and_is_not_forwarded(matched):
    server, room, (a_token, _), (b_token, _) = matched
    with relay(server, room, a_token) as parent, relay(server, room, b_token) as child:
        parent.send(json.dumps({"t": "ping"}))
        assert json.loads(parent.recv(timeout=TIMEOUT)) == {"t": "pong"}
        # The pong went to the sender, not the peer: the peer's next message is the frame.
        out = frame(1, 12, b"after the ping")
        parent.send(out)
        assert child.recv(timeout=TIMEOUT) == out


def test_the_survivor_is_told_and_the_room_is_freed(matched):
    server, room, (a_token, _), (b_token, _) = matched
    child = relay(server, room, b_token)
    parent = relay(server, room, a_token)
    parent.send(frame(1, 12, b"hello"))
    assert child.recv(timeout=TIMEOUT) == frame(1, 12, b"hello")

    parent.close()
    assert json.loads(child.recv(timeout=TIMEOUT)) == {"t": "peer_left"}
    with pytest.raises(ConnectionClosed):
        child.recv(timeout=TIMEOUT)
    child.close()

    assert httpx.get(server.base + "/v1/health", timeout=TIMEOUT).json()["rooms"] == 0

    # The room id is gone: reconnecting to it is refused.
    with pytest.raises(REFUSED):
        ws = relay(server, room, a_token)
        ws.recv(timeout=TIMEOUT)
        ws.recv(timeout=TIMEOUT)


def test_a_third_party_cannot_join_the_room(matched):
    server, room, (a_token, _), _ = matched
    c_token, _ = account(server, "charlie")
    with relay(server, room, a_token):
        with pytest.raises(REFUSED):
            ws = relay(server, room, c_token)
            ws.recv(timeout=TIMEOUT)      # the {"t":"error"} ...
            ws.recv(timeout=TIMEOUT)      # ... then the close


def test_a_bad_token_or_room_is_refused(matched):
    server, room, _, _ = matched
    with pytest.raises(REFUSED):
        ws = relay(server, room, "nonsense")
        ws.recv(timeout=TIMEOUT)
    with pytest.raises(REFUSED):
        ws = relay(server, 99999, "nonsense")
        ws.recv(timeout=TIMEOUT)
        ws.recv(timeout=TIMEOUT)
