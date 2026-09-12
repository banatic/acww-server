"""SERVERFIX108 / F5: budgets, suppressed re-broadcasts, and the socket and room caps.

server-audit-1's fifth finding: nothing bounded how much one authenticated account could
ask the process to do. Repeated `wait` with UNCHANGED state serialized the whole waiting
list and pushed it to every connected socket and logged an event, every time; invite,
decline, ping, relay traffic and save uploads had no rate at all; two accounts could match
and re-match with no active-room quota.

The budgets are token buckets (`app.security.Budget`) and the defaults are deliberately far
above the game: a save is a handful of PUTs a day, a relay carries at most one WM frame per
side per 1/60 s. THE TESTS THEREFORE TURN THEM DOWN. A test that made the default bucket
overflow would have to send tens of thousands of messages, take minutes, and prove nothing
about the mechanism; these set the bucket to a size where the ceiling is a fact rather than
an endurance test, and `test_the_default_budgets_pass_ordinary_play` is the control that the
DEFAULTS do not refuse the traffic the game actually makes.
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


def health(server) -> dict:
    return httpx.get(server.base + "/v1/health", timeout=TIMEOUT).json()


# ------------------------------------------------------------------- the bucket

def test_the_token_bucket_refills_and_refuses():
    # Imported here and not at the top so this module still COLLECTS against the pinned
    # code, where `Budget` does not exist -- every other test in the file then fails for its
    # own reason instead of the whole file failing to import.
    from app.security import Budget
    b = Budget(burst=3, per_sec=0.0)               # no refill: three and then nothing
    assert [b.spend("k") for _ in range(5)] == [True, True, True, False, False]
    assert b.spend("other") is True                # a different key has its own bucket

    costed = Budget(burst=1000, per_sec=0.0)
    assert costed.spend("k", 900.0) is True
    assert costed.spend("k", 200.0) is False       # a refusal costs nothing ...
    assert costed.spend("k", 100.0) is True       # ... so the remaining 100 is still there

    assert Budget(burst=0, per_sec=10.0).spend("k") is False


# ------------------------------------------------------------- the unchanged list

def test_an_unchanged_wait_does_not_rebroadcast_the_list(server):
    """The finding's cheapest half: one account's repeated no-op message used to be work
    proportional to the number of players connected.

    `wait` with the same mode and town is idempotent -- the list it would push is byte for
    byte the one every socket already holds -- so nothing is sent. The FIRST wait is still
    broadcast, and a wait that actually changes something is broadcast again.
    """
    a_token, a_id = account(server, "alpha")
    b_token, _ = account(server, "bravo")
    a, b = Client(server, a_token), Client(server, b_token)
    try:
        b.expect("list")
        a.send(t="wait", mode="host", town_name="Hanabi")
        assert len(b.expect_list(1)) == 1                    # the change is broadcast

        for _ in range(20):
            a.send(t="wait", mode="host", town_name="Hanabi")
        a.send(t="ping")
        # The pong is the marker: it is sent after the twenty repeats, so b's queue holding
        # nothing at all by the time it arrives means no repeat broadcast anything. (a's own
        # queue still has its connect list and the one real change in front of the pong,
        # which is why this walks past them rather than demanding the pong first.)
        assert a.expect("pong")["t"] == "pong"
        with pytest.raises(TimeoutError):
            b.ws.recv(timeout=2.0)

        a.send(t="wait", mode="guest", town_name="Hanabi")   # a REAL change
        listed = b.expect_list(1)
        assert listed[0]["mode"] == "guest"
    finally:
        a.close()
        b.close()


# ----------------------------------------------------------------- lobby budgets

def test_a_flood_of_lobby_messages_is_refused_and_the_socket_is_closed(server_factory):
    s = server_factory("lobbyops", lobby_ops_burst=12, lobby_ops_per_sec=0.0)
    token, _ = account(s, "alpha")
    a = Client(s, token)
    try:
        a.expect("list")
        with pytest.raises(ConnectionClosed) as closed:
            for _ in range(200):
                a.send(t="ping")
                a.recv()
        assert closed.value.rcvd.code == 1013, closed.value.rcvd
    finally:
        a.close()


def test_the_lobby_socket_cap_refuses_the_extra_connection(server_factory):
    """The cap is on SOCKETS this process holds, so the refusal is 1013 (try again later)
    rather than 1008: nothing about the caller is wrong, the server is full."""
    s = server_factory("sockets", max_lobby_sockets=2)
    tokens = [account(s, "player%d" % i)[0] for i in range(3)]
    held = [Client(s, tokens[0]), Client(s, tokens[1])]
    try:
        for c in held:
            c.expect("list")
        third = Client(s, tokens[2])
        try:
            msg = third.recv()
            assert msg["t"] == "error" and "lobby connections" in msg["msg"], msg
            with pytest.raises(ConnectionClosed) as closed:
                third.recv()
            assert closed.value.rcvd.code == 1013, closed.value.rcvd
        finally:
            third.close()
        # The two that were already in are untouched, and a slot freed is a slot reusable.
        held[0].send(t="ping")
        assert held[0].expect("pong")["t"] == "pong"
        held.pop().close()
        again = Client(s, tokens[2])
        try:
            assert again.expect("list")["t"] == "list"
        finally:
            again.close()
    finally:
        for c in held:
            c.close()


def test_an_accounts_own_reconnect_is_not_refused_by_the_socket_cap(server_factory):
    """A replacement socket for an account that already holds one costs no new slot.

    Refusing it would make a reconnect after a dropped connection impossible exactly when
    the lobby is busy, which is when connections drop.
    """
    s = server_factory("reconnect", max_lobby_sockets=1)
    token, _ = account(s, "alpha")
    first = Client(s, token)
    try:
        first.expect("list")
        second = Client(s, token)
        try:
            assert second.expect("list")["t"] == "list"
        finally:
            second.close()
    finally:
        first.close()


# ------------------------------------------------------------------ the room cap

def test_the_room_cap_refuses_a_further_match(server_factory):
    """An active-room quota, which the finding named as absent: two accounts could match,
    open one side of each relay and re-match for ever."""
    s = server_factory("rooms", max_rooms=1)
    people = [account(s, "player%d" % i) for i in range(4)]
    clients = [Client(s, t) for t, _ in people]
    try:
        for c in clients:
            c.send(t="wait", mode="host", town_name="T")
        clients[0].expect_list(4)
        clients[0].send(t="invite", to=people[1][1])
        clients[1].expect("invite")
        clients[1].send(t="accept", **{"from": people[0][1]})
        assert clients[1].expect("matched")["room"]

        clients[2].send(t="invite", to=people[3][1])
        clients[3].expect("invite")
        clients[3].send(t="accept", **{"from": people[2][1]})
        msg = clients[3].expect("error")
        assert "relay rooms" in msg["msg"], msg
        assert health(s)["rooms"] == 1
        # Refused, not consumed: both are still in the list and can try again later.
        assert health(s)["waiting"] == 2
    finally:
        for c in clients:
            c.close()


# ------------------------------------------------------------------ http budgets

def test_the_http_operation_budget_answers_429(server_factory):
    s = server_factory("httpops", http_ops_burst=8, http_ops_per_sec=0.0)
    r = httpx.post(s.base + "/v1/auth/register",
                   json={"username": "mayor", "password": "a long enough password"},
                   timeout=TIMEOUT)
    auth = {"Authorization": "Bearer " + r.json()["token"]}
    codes = [httpx.get(s.base + "/v1/me", headers=auth, timeout=TIMEOUT).status_code
             for _ in range(20)]
    assert codes[:8] == [200] * 8, codes
    assert set(codes[8:]) == {429}, codes
    assert "slow down" in httpx.get(s.base + "/v1/me", headers=auth,
                                    timeout=TIMEOUT).json()["detail"]


def test_the_http_byte_budget_answers_429(server_factory, sample_save):
    """Bytes as well as operations: twenty 256 KB uploads are twenty operations and five
    megabytes, and the second is the one worth bounding on a NAS."""
    s = server_factory("httpbytes", http_bytes_burst=600000, http_bytes_per_sec=0.0)
    r = httpx.post(s.base + "/v1/auth/register",
                   json={"username": "mayor", "password": "a long enough password"},
                   timeout=TIMEOUT)
    auth = {"Authorization": "Bearer " + r.json()["token"],
            "Content-Type": "application/octet-stream"}
    first = httpx.put(s.base + "/v1/save", content=sample_save, headers=auth,
                      timeout=TIMEOUT)
    assert first.status_code == 200, first.text
    second = httpx.put(s.base + "/v1/save", content=mutate_save(sample_save, 51),
                       headers=auth, timeout=TIMEOUT)
    assert second.status_code == 200, second.text
    third = httpx.put(s.base + "/v1/save", content=mutate_save(sample_save, 52),
                      headers=auth, timeout=TIMEOUT)
    assert third.status_code == 429, third.status_code
    assert "bytes" in third.json()["detail"]
    # Nothing was stored by the refusal: the account still holds version 2.
    assert httpx.get(s.base + "/v1/me", headers={"Authorization": auth["Authorization"]},
                     timeout=TIMEOUT).json()["save"]["version"] == 2


# ----------------------------------------------------------------- relay budgets

@pytest.fixture
def matched_on(server_factory):
    def _make(name, **overrides):
        s = server_factory(name, **overrides)
        a_token, a_id = account(s, "alpha")
        b_token, b_id = account(s, "bravo")
        a, b = Client(s, a_token), Client(s, b_token)
        a.send(t="wait", mode="host", town_name="Hanabi")
        b.send(t="wait", mode="guest", town_name="Kirie")
        a.expect_list(2)
        a.send(t="invite", to=b_id)
        b.expect("invite")
        b.send(t="accept", **{"from": a_id})
        room = a.expect("matched")["room"]
        b.expect("matched")
        a.close()
        b.close()
        return s, room, a_token, b_token
    return _make


def relay(server, room, token):
    return connect("%s/v1/relay/%d?token=%s" % (server.ws_base, room, token),
                   open_timeout=TIMEOUT)


def test_the_relay_frame_budget_closes_the_flooding_socket(matched_on):
    s, room, a_token, b_token = matched_on("relayops", relay_ops_burst=20,
                                           relay_ops_per_sec=0.0)
    with relay(s, room, b_token) as child:
        parent = relay(s, room, a_token)
        try:
            frame = struct.pack("<BBH", 1, 12, 4) + b"beep"
            # No recv inside the loop: the relay forwards to the PEER and never echoes, so
            # a receive on the sender would only ever time out. The budget's refusal arrives
            # as the close, which is either a failed send or the next receive.
            with pytest.raises(ConnectionClosed) as closed:
                for _ in range(400):
                    parent.send(frame)
                parent.recv(timeout=TIMEOUT)
            assert closed.value.rcvd.code == 1013, closed.value.rcvd
        finally:
            parent.close()
        # The frames inside the budget WERE forwarded -- the bucket is a ceiling, not a
        # switch -- so the peer's queue holds those and then the close.
        forwarded = 0
        while True:
            got = child.recv(timeout=TIMEOUT)
            if isinstance(got, (bytes, bytearray)):
                forwarded += 1
                continue
            assert json.loads(got) == {"t": "peer_left"}
            break
        assert 0 < forwarded <= 20, forwarded


def test_the_default_budgets_pass_ordinary_play(server, sample_save):
    """M1's control, and the reason every other test in this file turns the knobs down.

    A budget that fires on the game's own traffic is not a security fix, it is an outage.
    This is a whole ordinary session at the DEFAULTS: a save round trip, the history, a
    lobby conversation, a match, and three hundred relay frames each way -- five seconds of
    a real visit at 60 Hz -- and nothing is refused.
    """
    a_token, a_id = account(server, "alpha")
    b_token, b_id = account(server, "bravo")
    auth = {"Authorization": "Bearer " + a_token,
            "Content-Type": "application/octet-stream"}
    assert httpx.put(server.base + "/v1/save", content=sample_save, headers=auth,
                     timeout=TIMEOUT).status_code == 200
    assert httpx.put(server.base + "/v1/save", content=mutate_save(sample_save, 61),
                     headers=auth, timeout=TIMEOUT).status_code == 200
    assert httpx.get(server.base + "/v1/save",
                     headers={"Authorization": "Bearer " + a_token},
                     timeout=TIMEOUT).status_code == 200
    assert httpx.get(server.base + "/v1/save/history",
                     headers={"Authorization": "Bearer " + a_token},
                     timeout=TIMEOUT).status_code == 200

    a, b = Client(server, a_token), Client(server, b_token)
    try:
        a.send(t="wait", mode="host", town_name="Hanabi")
        b.send(t="wait", mode="guest", town_name="Kirie")
        a.expect_list(2)
        for _ in range(10):
            a.send(t="ping")
            assert a.expect("pong")["t"] == "pong"
        a.send(t="invite", to=b_id)
        b.expect("invite")
        b.send(t="accept", **{"from": a_id})
        room = a.expect("matched")["room"]
        b.expect("matched")
    finally:
        a.close()
        b.close()

    with relay(server, room, a_token) as parent, relay(server, room, b_token) as child:
        for i in range(300):
            up = struct.pack("<BBH", 1, 12, 32) + bytes([i & 0xFF]) * 32
            parent.send(up)
            assert child.recv(timeout=TIMEOUT) == up
            child.send(up)
            assert parent.recv(timeout=TIMEOUT) == up
