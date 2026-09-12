"""SERVERFIX108 / F6: a replaced relay socket stops being an authorized writer.

server-audit-1's sixth finding, measured: the same account opened a SECOND socket for the
same room, `join_room` overwrote the socket map entry, and the first connection was left
running -- its receive loop never asked whether it was still the registered writer, so
frames from BOTH sockets reached the peer as that one account. A stale client or a stolen
token interleaved with a replacement session.

The fix is a generation per side, handed to the connection at join and checked against the
room before every forward, plus closing the socket that was displaced. The audit's
regression asked for two properties and both are here: only the selected socket can
forward, and a stale disconnect cannot destroy its replacement.
"""

from __future__ import annotations

import json
import struct

import httpx
import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from test_lobby import Client, account

TIMEOUT = 30


def frame(tag: bytes) -> bytes:
    return struct.pack("<BBH", 1, 12, len(tag)) + tag


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


def test_the_replaced_socket_can_no_longer_reach_the_peer(matched):
    """THE FINDING. Both sockets' frames used to arrive; now only the survivor's does."""
    server, room, a_token, b_token = matched
    with relay(server, room, b_token) as child:
        first = relay(server, room, a_token)
        first.send(frame(b"from-the-first"))
        assert child.recv(timeout=TIMEOUT) == frame(b"from-the-first")

        second = relay(server, room, a_token)          # the same account, the same room
        try:
            # The displaced socket is told and closed rather than left running.
            told = first.recv(timeout=TIMEOUT)
            assert json.loads(told)["t"] == "error"
            with pytest.raises(ConnectionClosed):
                first.recv(timeout=TIMEOUT)
            with pytest.raises(ConnectionClosed):
                first.send(frame(b"stale"))
                first.recv(timeout=TIMEOUT)

            # The replacement works, and what the peer receives next is ITS frame -- not a
            # stale one, which is the property the finding asked for.
            second.send(frame(b"from-the-second"))
            assert child.recv(timeout=TIMEOUT) == frame(b"from-the-second")
            child.send(frame(b"back"))
            assert second.recv(timeout=TIMEOUT) == frame(b"back")
        finally:
            first.close()
            second.close()


def test_the_stale_disconnect_does_not_destroy_the_replacement(matched):
    """The audit's second property. The displaced socket's close runs its `finally`, and
    that path must not free the room the replacement is sitting in."""
    server, room, a_token, b_token = matched
    with relay(server, room, b_token) as child:
        first = relay(server, room, a_token)
        second = relay(server, room, a_token)
        try:
            first.close()                              # the stale one goes away
            assert httpx.get(server.base + "/v1/health",
                             timeout=TIMEOUT).json()["rooms"] == 1
            second.send(frame(b"still here"))
            assert child.recv(timeout=TIMEOUT) == frame(b"still here")
        finally:
            first.close()
            second.close()


def test_the_survivor_is_still_told_when_the_real_peer_leaves(matched):
    """M1's control: a generation check that refused everything would pass the test above
    and break the game. The ordinary departure still reaches the survivor."""
    server, room, a_token, b_token = matched
    child = relay(server, room, b_token)
    parent = relay(server, room, a_token)
    try:
        parent.send(frame(b"hello"))
        assert child.recv(timeout=TIMEOUT) == frame(b"hello")
        parent.close()
        assert json.loads(child.recv(timeout=TIMEOUT)) == {"t": "peer_left"}
    finally:
        parent.close()
        child.close()
    assert httpx.get(server.base + "/v1/health", timeout=TIMEOUT).json()["rooms"] == 0


def test_the_other_side_is_not_disturbed_by_a_replacement(matched):
    """Replacing one side's socket must not invalidate the other side's."""
    server, room, a_token, b_token = matched
    child = relay(server, room, b_token)
    first = relay(server, room, a_token)
    second = relay(server, room, a_token)
    try:
        child.send(frame(b"child speaks"))
        assert second.recv(timeout=TIMEOUT) == frame(b"child speaks")
        second.send(frame(b"parent answers"))
        assert child.recv(timeout=TIMEOUT) == frame(b"parent answers")
    finally:
        first.close()
        second.close()
        child.close()
