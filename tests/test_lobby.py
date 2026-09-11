"""The lobby: wait, the pushed list, invite, decline, accept, matched to BOTH sides."""

from __future__ import annotations

import json

import httpx
import pytest
from websockets.sync.client import connect

TIMEOUT = 30


def account(server, name):
    r = httpx.post(server.base + "/v1/auth/register",
                   json={"username": name, "password": "a long enough password"},
                   timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()["token"], r.json()["user_id"]


class Client:
    """One player's lobby socket, with a blocking `expect` for a message type."""

    def __init__(self, server, token):
        self.ws = connect(server.ws_base + "/v1/lobby/ws?token=" + token,
                          open_timeout=TIMEOUT)

    def send(self, **msg):
        self.ws.send(json.dumps(msg))

    def recv(self):
        return json.loads(self.ws.recv(timeout=TIMEOUT))

    def expect(self, kind, tries=8):
        """The next message of this type. The server pushes a list on every change, so a
        test that wants `matched` must be allowed to walk past the lists."""
        for _ in range(tries):
            msg = self.recv()
            if msg.get("t") == kind:
                return msg
        raise AssertionError("no %r message arrived" % kind)

    def expect_list(self, n, tries=8):
        """Walk to the pushed list that holds exactly n waiters.

        Every socket is sent a list the moment it connects, and another on every change,
        so "the next list" is ambiguous in a test with two clients -- the count is the
        thing being asserted, so wait for it rather than for the first list to arrive.
        """
        for _ in range(tries):
            msg = self.recv()
            if msg.get("t") == "list" and len(msg["users"]) == n:
                return msg["users"]
        raise AssertionError("no list of %d waiters arrived" % n)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


@pytest.fixture
def two(server):
    a_token, a_id = account(server, "alpha")
    b_token, b_id = account(server, "bravo")
    a, b = Client(server, a_token), Client(server, b_token)
    try:
        yield server, (a, a_id, a_token), (b, b_id, b_token)
    finally:
        a.close()
        b.close()


def test_a_bad_token_is_refused_at_the_handshake(server):
    from websockets.exceptions import ConnectionClosed, InvalidStatus
    with pytest.raises((ConnectionClosed, InvalidStatus, OSError)):
        ws = connect(server.ws_base + "/v1/lobby/ws?token=nonsense", open_timeout=TIMEOUT)
        ws.recv(timeout=TIMEOUT)          # the close frame arrives here if the accept did


def test_the_first_push_is_the_empty_list(two):
    server, (a, _, _), _ = two
    assert a.expect("list")["users"] == []


def test_wait_shows_up_on_both_the_socket_and_the_rest_list(two):
    server, (a, a_id, a_token), (b, b_id, _) = two
    a.send(t="wait", mode="host", town_name="Hanabi")

    for who in (a, b):
        listed = who.expect_list(1)
        assert listed[0]["user_id"] == a_id
        assert listed[0]["username"] == "alpha"
        assert listed[0]["town_name"] == "Hanabi"
        assert listed[0]["mode"] == "host"
        assert listed[0]["since_utc"]

    rest = httpx.get(server.base + "/v1/lobby",
                     headers={"Authorization": "Bearer " + a_token}, timeout=TIMEOUT).json()
    assert [u["user_id"] for u in rest] == [a_id]
    assert httpx.get(server.base + "/v1/health", timeout=TIMEOUT).json()["waiting"] == 1

    a.send(t="leave")
    assert b.expect_list(0) == []
    assert httpx.get(server.base + "/v1/health", timeout=TIMEOUT).json()["waiting"] == 0


def test_leaving_the_socket_drops_the_wait(server):
    a_token, a_id = account(server, "alpha")
    b_token, _ = account(server, "bravo")
    b = Client(server, b_token)
    a = Client(server, a_token)
    a.send(t="wait", mode="guest", town_name="Kirie")
    assert len(b.expect_list(1)) == 1
    a.close()
    assert b.expect_list(0) == []
    b.close()


def test_invite_decline_and_then_accept(two):
    server, (a, a_id, _), (b, b_id, _) = two
    a.send(t="wait", mode="host", town_name="Hanabi")
    b.send(t="wait", mode="guest", town_name="Kirie")
    a.expect_list(2)
    b.expect_list(2)

    a.send(t="invite", to=b_id)
    invite = b.expect("invite")
    assert invite["from"]["user_id"] == a_id
    assert invite["from"]["town_name"] == "Hanabi"
    assert invite["from"]["mode"] == "host"

    b.send(t="decline", **{"from": a_id})
    declined = a.expect("decline")
    assert declined["from"] == b_id
    assert httpx.get(server.base + "/v1/health", timeout=TIMEOUT).json()["waiting"] == 2

    a.send(t="invite", to=b_id)
    b.expect("invite")
    b.send(t="accept", **{"from": a_id})

    ma, mb = a.expect("matched"), b.expect("matched")
    assert ma["room"] == mb["room"]
    assert ma["role"] == "parent" and mb["role"] == "child"       # host is the WM parent
    assert ma["peer"]["user_id"] == b_id and ma["peer"]["town_name"] == "Kirie"
    assert mb["peer"]["user_id"] == a_id and mb["peer"]["town_name"] == "Hanabi"

    health = httpx.get(server.base + "/v1/health", timeout=TIMEOUT).json()
    assert health["rooms"] == 1 and health["waiting"] == 0         # both left the list


def test_the_inviter_is_the_parent_when_both_say_the_same_mode(two):
    _server, (a, a_id, _), (b, b_id, _) = two
    a.send(t="wait", mode="guest", town_name="Hanabi")
    b.send(t="wait", mode="guest", town_name="Kirie")
    a.expect_list(2)
    b.send(t="accept", **{"from": a_id})
    assert a.expect("matched")["role"] == "parent"
    assert b.expect("matched")["role"] == "child"


def test_the_errors(two):
    _server, (a, a_id, _), (b, b_id, _) = two
    a.expect("list")
    a.send(t="wait", mode="sideways")
    assert "host" in a.expect("error")["msg"]

    a.send(t="invite", to=b_id)                 # not waiting yet
    assert "wait" in a.expect("error")["msg"]

    a.send(t="wait", mode="host", town_name="Hanabi")
    a.expect_list(1)
    a.send(t="invite", to=b_id)                 # b is not waiting
    assert "not waiting" in a.expect("error")["msg"]

    a.send(t="accept", **{"from": b_id})
    assert "no longer valid" in a.expect("error")["msg"]

    a.send(t="somethingelse")
    assert "unknown" in a.expect("error")["msg"]

    a.ws.send("not json at all")
    assert "JSON" in a.expect("error")["msg"]

    a.send(t="ping")
    assert a.expect("pong")["t"] == "pong"
