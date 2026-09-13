"""A public town keeps one parent and admits three independently correlated children."""

from __future__ import annotations

import json

import httpx
import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from test_lobby import Client, TIMEOUT, account


def relay(server, room: int, token: str):
    return connect("%s/v1/relay/%d?token=%s" % (server.ws_base, room, token),
                   open_timeout=TIMEOUT)


def wire(source_aid: int, payload: bytes) -> bytes:
    frame = bytearray(68 + len(payload))
    frame[4] = 3
    frame[63] = source_aid
    frame[68:] = payload
    return bytes(frame)


def admit(host: Client, host_id: int, guest: Client, guest_id: int,
          first: bool) -> dict:
    guest.send(t="wait", mode="guest", town_name="Town-%d" % guest_id)
    guest.expect_list(2)                 # the open host and this waiting guest
    guest.send(t="invite", to=host_id)
    offer = host.expect("invite")
    host.send(t="accept", **{"from": guest_id, "invite_id": offer["invite_id"]})
    if first:
        parent = host.expect("matched")
        assert parent["role"] == "parent" and parent["aid"] == 0
    else:
        joined = host.expect("member_joined")
        assert joined["member"]["user_id"] == guest_id
    child = guest.expect("matched")
    assert child["role"] == "child"
    return child


def test_public_host_invite_survives_the_private_invite_ttl(server_factory):
    server = server_factory("public-four-ttl", invite_ttl=0.0)
    host_token, host_id = account(server, "ttl_host")
    guest_token, guest_id = account(server, "ttl_guest")
    host, guest = Client(server, host_token), Client(server, guest_token)
    try:
        host.send(t="wait", mode="host", town_name="Open", open=True)
        host.expect_list(1)
        guest.send(t="wait", mode="guest", town_name="Away")
        guest.expect_list(2)
        guest.send(t="invite", to=host_id)
        offer = host.expect("invite")
        host.send(t="accept", **{"from": guest_id, "invite_id": offer["invite_id"]})
        assert host.expect("matched")["role"] == "parent"
        assert guest.expect("matched")["role"] == "child"
    finally:
        host.close()
        guest.close()


def test_public_host_replays_the_oldest_waiting_invite_after_each_admission(server):
    credentials = [account(server, name) for name in
                   ("queue_host", "queue_one", "queue_two", "queue_three")]
    clients = [Client(server, token) for token, _user_id in credentials]
    host, first, earlier, later = clients
    host_id = credentials[0][1]
    try:
        host.send(t="wait", mode="host", town_name="Queue", open=True)
        host.expect_list(1)
        admit(host, host_id, first, credentials[1][1], first=True)

        for guest in (earlier, later):
            guest.send(t="wait", mode="guest", town_name="Away")
            guest.send(t="invite", to=host_id)
        earlier_offer = host.expect("invite")
        later_offer = host.expect("invite")
        assert earlier_offer["from"]["user_id"] == credentials[2][1]
        assert later_offer["from"]["user_id"] == credentials[3][1]

        host.send(t="accept", **{"from": credentials[3][1],
                                 "invite_id": later_offer["invite_id"]})
        assert host.expect("member_joined")["member"]["user_id"] == credentials[3][1]
        assert later.expect("matched")["aid"] == 2
        replay = host.expect("invite")
        assert replay["from"]["user_id"] == credentials[2][1]
        assert replay["invite_id"] == earlier_offer["invite_id"]

        host.send(t="accept", **{"from": credentials[2][1],
                                 "invite_id": replay["invite_id"]})
        assert host.expect("member_joined")["member"]["user_id"] == credentials[2][1]
        assert earlier.expect("matched", tries=20)["aid"] == 3
    finally:
        for client in clients:
            client.close()


def test_public_town_admits_three_children_and_relays_in_a_parent_star(server):
    credentials = [account(server, name) for name in
                   ("host", "guest_one", "guest_two", "guest_three")]
    clients = [Client(server, token) for token, _user_id in credentials]
    host = clients[0]
    host_id = credentials[0][1]
    relays = []
    try:
        host.send(t="wait", mode="host", town_name="Hanabi", open=True)
        row = host.expect_list(1)[0]
        assert row["user_id"] == host_id
        assert row["open"] is True and row["players"] == 1 and row["capacity"] == 4

        matches = []
        for index in range(1, 4):
            matches.append(admit(host, host_id, clients[index], credentials[index][1],
                                 first=index == 1))
        room = matches[0]["room"]
        assert [match["room"] for match in matches] == [room, room, room]
        assert [match["aid"] for match in matches] == [1, 2, 3]
        assert [member["aid"] for member in matches[-1]["members"]] == [0, 1, 2, 3]
        health = httpx.get(server.base + "/v1/health", timeout=TIMEOUT).json()
        assert health["rooms"] == 1 and health["waiting"] == 0

        states = []
        for token, _user_id in credentials:
            ws = relay(server, room, token)
            state = json.loads(ws.recv(timeout=TIMEOUT))
            assert state["t"] == "room_state" and state["room"] == room
            states.append(state)
            relays.append(ws)
        assert [state["aid"] for state in states] == [0, 1, 2, 3]

        parent, child1, child2, child3 = relays
        down = wire(0, b"parent broadcasts one opaque frame")
        parent.send(down)
        assert [child.recv(timeout=TIMEOUT) for child in (child1, child2, child3)] \
               == [down, down, down]

        up = wire(2, b"only the parent receives a child frame")
        child2.send(up)
        assert parent.recv(timeout=TIMEOUT) == up
        with pytest.raises(TimeoutError):
            child1.recv(timeout=0.05)
        with pytest.raises(TimeoutError):
            child3.recv(timeout=0.05)

        child2.close()
        for survivor in (parent, child1, child3):
            left = json.loads(survivor.recv(timeout=TIMEOUT))
            assert left == {"t": "member_left", "room": room, "aid": 2}
        assert httpx.get(server.base + "/v1/health", timeout=TIMEOUT).json()["rooms"] == 1

        after = wire(0, b"the room survives one child leaving")
        parent.send(after)
        assert child1.recv(timeout=TIMEOUT) == after
        assert child3.recv(timeout=TIMEOUT) == after

        parent.close()
        for survivor in (child1, child3):
            closed = json.loads(survivor.recv(timeout=TIMEOUT))
            assert closed["t"] == "peer_left" and closed["room_closed"] is True
            with pytest.raises(ConnectionClosed):
                survivor.recv(timeout=TIMEOUT)
    finally:
        for ws in relays:
            try:
                ws.close()
            except Exception:
                pass
        for client in clients:
            client.close()
