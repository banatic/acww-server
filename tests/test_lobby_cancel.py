"""Cancellable lobby waiting and invitation correlation.

The server's cancellation operation is deliberately tested with the real websocket endpoint,
then its two lock orders are exercised directly.  The endpoint tests use only fixture accounts
and the temporary database supplied by ``conftest.py``.
"""

from __future__ import annotations

import asyncio
import gc
import weakref

import httpx
import pytest

from app.lobby import LobbyState
from test_lobby import Client, account


TIMEOUT = 30


@pytest.fixture
def pair(server):
    a_token, a_id = account(server, "alpha")
    b_token, b_id = account(server, "bravo")
    a, b = Client(server, a_token), Client(server, b_token)
    a.send(t="wait", mode="host", town_name="Hanabi")
    b.send(t="wait", mode="guest", town_name="Kirie")
    a.expect_list(2)
    b.expect_list(2)
    try:
        yield server, (a, a_id), (b, b_id)
    finally:
        a.close()
        b.close()


def health(server):
    return httpx.get(server.base + "/v1/health", timeout=TIMEOUT).json()


def test_initial_list_advertises_cancellation_capability(server):
    token, _ = account(server, "alpha")
    client = Client(server, token)
    try:
        first = client.expect("list")
        assert set(first["capabilities"]) >= {"cancel", "invite_id", "command_request_id"}
    finally:
        client.close()


def test_cancel_clears_wait_and_notifies_outgoing_peer_once(pair):
    server, (a, a_id), (b, b_id) = pair
    a.send(t="invite", to=b_id, request_id="invite-1")
    invite_id = b.expect("invite")["invite_id"]
    assert a.expect("invited") == {"t": "invited", "to": b_id,
                                    "invite_id": invite_id, "request_id": "invite-1"}

    a.send(t="cancel", request_id="close-1")
    ack = a.expect("cancelled")
    assert ack == {"t": "cancelled", "status": "cancelled", "waiting": False,
                   "removed": True, "request_id": "close-1"}
    assert b.expect("cancelled") == {"t": "cancelled", "from": a_id,
                                      "invite_id": invite_id}
    # The list message above is the only state broadcast; assert its count without relying on
    # its row order (the fixture's socket may have seen the initial list first).
    assert len(b.expect_list(1)) == 1
    assert health(server)["waiting"] == 1

    # A retry is a successful, idempotent no-op and does not notify the peer again.
    a.send(t="cancel", request_id="close-2")
    assert a.expect("cancelled") == {"t": "cancelled", "status": "cancelled",
                                      "waiting": False, "removed": False,
                                      "request_id": "close-2"}
    with pytest.raises(TimeoutError):
        b.ws.recv(timeout=0.5)

    # The old acceptance cannot consume anything after cancellation.
    b.send(t="accept", **{"from": a_id}, invite_id=invite_id)
    assert "no longer valid" in b.expect("error")["msg"]


def test_targeted_cancel_keeps_waiting_and_old_id_cannot_accept_fresh_invite(pair):
    server, (a, a_id), (b, b_id) = pair
    a.send(t="invite", to=b_id)
    old_id = b.expect("invite")["invite_id"]
    assert a.expect("invited")["invite_id"] == old_id

    # Incoming cancellation removes exactly this invitation and leaves b in the waiting list.
    b.send(t="cancel", request_id="decline-1", **{"from": a_id}, invite_id=old_id)
    assert b.expect("cancelled") == {"t": "cancelled", "status": "cancelled",
                                      "waiting": True, "removed": True,
                                      "request_id": "decline-1", "from": a_id,
                                      "invite_id": old_id}
    assert a.expect("cancelled") == {"t": "cancelled", "from": b_id,
                                      "invite_id": old_id}
    assert health(server)["waiting"] == 2

    # A fresh invitation receives a different correlation id.  A delayed accept for the old
    # invitation is refused and cannot consume the fresh one.
    a.send(t="invite", to=b_id)
    new_id = b.expect("invite")["invite_id"]
    assert a.expect("invited")["invite_id"] == new_id
    assert new_id != old_id
    b.send(t="accept", **{"from": a_id}, invite_id=old_id)
    assert "no longer valid" in b.expect("error")["msg"]
    assert health(server)["waiting"] == 2
    b.send(t="accept", **{"from": a_id}, invite_id=new_id)
    assert a.expect("matched")["invite_id"] == new_id
    assert b.expect("matched")["invite_id"] == new_id


def test_bare_cancel_clears_both_invitation_directions(server):
    a_token, a_id = account(server, "alpha")
    b_token, b_id = account(server, "bravo")
    c_token, c_id = account(server, "charlie")
    a, b, c = Client(server, a_token), Client(server, b_token), Client(server, c_token)
    try:
        a.send(t="wait", mode="host", town_name="Hanabi")
        b.send(t="wait", mode="guest", town_name="Kirie")
        c.send(t="wait", mode="host", town_name="Tsubaki")
        for client in (a, b, c):
            client.expect_list(3)

        a.send(t="invite", to=b_id)
        outgoing_id = b.expect("invite")["invite_id"]
        a.expect("invited")
        c.send(t="invite", to=a_id)
        incoming_id = a.expect("invite")["invite_id"]
        c.expect("invited")

        a.send(t="cancel", request_id="all-1")
        assert a.expect("cancelled") == {"t": "cancelled", "status": "cancelled",
                                          "waiting": False, "removed": True,
                                          "request_id": "all-1"}
        assert b.expect("cancelled") == {"t": "cancelled", "from": a_id,
                                          "invite_id": outgoing_id}
        assert c.expect("cancelled") == {"t": "cancelled", "from": a_id,
                                          "invite_id": incoming_id}
        assert health(server)["waiting"] == 2
    finally:
        a.close()
        b.close()
        c.close()


def test_targeted_outgoing_cancel_is_scoped_and_idempotent(pair):
    server, (a, a_id), (b, b_id) = pair
    a.send(t="invite", to=b_id)
    invite_id = b.expect("invite")["invite_id"]
    assert a.expect("invited")["invite_id"] == invite_id

    a.send(t="cancel", request_id="invite-1", to=b_id, invite_id=invite_id)
    assert a.expect("cancelled") == {"t": "cancelled", "status": "cancelled",
                                      "waiting": True, "removed": True,
                                      "request_id": "invite-1", "to": b_id,
                                      "invite_id": invite_id}
    assert b.expect("cancelled") == {"t": "cancelled", "from": a_id,
                                      "invite_id": invite_id}
    assert health(server)["waiting"] == 2

    # Repeating the same scoped cancellation cannot emit a second peer event.
    a.send(t="cancel", request_id="invite-2", to=b_id, invite_id=invite_id)
    assert a.expect("cancelled") == {"t": "cancelled", "status": "cancelled",
                                      "waiting": True, "removed": False,
                                      "request_id": "invite-2", "to": b_id,
                                      "invite_id": invite_id}
    with pytest.raises(TimeoutError):
        b.ws.recv(timeout=0.5)


def test_cancel_after_match_reports_active_room_and_keeps_room(pair):
    server, (a, a_id), (b, b_id) = pair
    a.send(t="invite", to=b_id)
    invite_id = b.expect("invite")["invite_id"]
    assert a.expect("invited")["invite_id"] == invite_id
    b.send(t="accept", **{"from": a_id}, invite_id=invite_id)
    room = a.expect("matched")["room"]
    b.expect("matched")

    b.send(t="cancel", request_id="late-cancel")
    assert b.expect("cancelled") == {"t": "cancelled", "status": "matched",
                                      "waiting": False, "removed": False,
                                      "request_id": "late-cancel", "room": room}
    assert health(server)["rooms"] == 1


def test_malformed_cancel_ids_are_rejected_before_mutation(pair):
    server, (a, _a_id), (b, b_id) = pair
    a.send(t="invite", to=b_id)
    invite_id = b.expect("invite")["invite_id"]
    assert a.expect("invited")["invite_id"] == invite_id

    a.send(t="cancel", request_id=7)
    malformed = a.expect("error")
    assert "request_id" in malformed["msg"] and "request_id" not in malformed
    a.send(t="cancel", request_id="", to=b_id, invite_id=invite_id)
    malformed = a.expect("error")
    assert "request_id" in malformed["msg"] and "request_id" not in malformed
    a.send(t="cancel", request_id="ok", invite_id=invite_id)
    assert "requires" in a.expect("error")["msg"]
    assert health(server)["waiting"] == 2
    # The invitation survived all rejected requests.
    b.send(t="accept", **{"from": _a_id}, invite_id=invite_id)
    assert a.expect("matched")["room"] == b.expect("matched")["room"]


def test_malformed_invite_request_id_is_rejected_before_recording(server):
    a_token, a_id = account(server, "alpha")
    b_token, b_id = account(server, "bravo")
    a, b = Client(server, a_token), Client(server, b_token)
    try:
        a.send(t="wait", mode="host", town_name="Hanabi")
        b.send(t="wait", mode="guest", town_name="Kirie")
        a.expect_list(2)
        b.expect_list(2)
        a.send(t="invite", to=b_id, request_id=None)
        malformed = a.expect("error")
        assert "request_id" in malformed["msg"] and "request_id" not in malformed
        with pytest.raises(TimeoutError):
            b.ws.recv(timeout=0.5)
        a.send(t="invite", to=b_id, request_id="x" * 65)
        malformed = a.expect("error")
        assert "request_id" in malformed["msg"] and "request_id" not in malformed
        with pytest.raises(TimeoutError):
            b.ws.recv(timeout=0.5)
        assert health(server)["waiting"] == 2
        a.send(t="invite", to=b_id, request_id="fresh")
        invite_id = b.expect("invite")["invite_id"]
        assert a.expect("invited") == {"t": "invited", "to": b_id,
                                        "invite_id": invite_id, "request_id": "fresh"}
    finally:
        a.close()
        b.close()


def test_delayed_command_error_and_cancel_keep_distinct_request_ids(pair):
    server, (a, _a_id), (b, b_id) = pair
    # Both commands are already in one socket's receive stream.  The first response is an
    # error, but it must carry ticket A so the client cannot apply it to the following cancel B.
    a.send(t="wait", mode="invalid", town_name="Hanabi", request_id="A")
    a.send(t="cancel", to=b_id, request_id="B")
    assert a.expect("error") == {"t": "error",
                                  "msg": 'mode must be "host" or "guest"',
                                  "request_id": "A"}
    assert a.expect("cancelled") == {"t": "cancelled", "status": "cancelled",
                                      "waiting": True, "removed": False,
                                      "request_id": "B", "to": b_id}
    assert health(server)["waiting"] == 2


def test_valid_request_id_is_echoed_on_each_recognized_command_error(pair):
    server, (a, a_id), (b, b_id) = pair
    a.send(t="invite", to=999999, request_id="invite-error")
    invite_error = a.expect("error")
    assert invite_error["request_id"] == "invite-error"
    assert "another user's id" in invite_error["msg"] or "not waiting" in invite_error["msg"]

    b.send(t="accept", **{"from": a_id}, request_id="accept-error")
    assert b.expect("error")["request_id"] == "accept-error"

    b.send(t="decline", **{"from": a_id}, invite_id="bad", request_id="decline-error")
    decline_error = b.expect("error")
    assert decline_error["request_id"] == "decline-error"
    assert "invite_id" in decline_error["msg"]

    a.send(t="cancel", **{"from": b_id}, invite_id="bad", request_id="cancel-error")
    cancel_error = a.expect("error")
    assert cancel_error["request_id"] == "cancel-error"
    assert "invite_id" in cancel_error["msg"]


def test_legacy_leave_uses_cancellation_notification(pair):
    server, (a, a_id), (b, b_id) = pair
    a.send(t="invite", to=b_id)
    invite_id = b.expect("invite")["invite_id"]
    assert a.expect("invited")["invite_id"] == invite_id
    a.send(t="leave")
    assert b.expect("cancelled") == {"t": "cancelled", "from": a_id,
                                      "invite_id": invite_id}
    assert b.expect_list(1)
    assert health(server)["waiting"] == 1


def test_cancel_and_accept_have_both_lock_order_outcomes():
    async def exercise(cancel_first: bool):
        state = LobbyState()
        await state.wait(1, "alpha", "Hanabi", "host", object())
        await state.wait(2, "bravo", "Kirie", "guest", object())
        invitation = await state.offer_invite(1, 2)
        assert invitation is not None

        if cancel_first:
            cancel_result, room = await asyncio.gather(
                state.cancel(1), state.match(1, 2, invitation.invite_id))
        else:
            room, cancel_result = await asyncio.gather(
                state.match(1, 2, invitation.invite_id), state.cancel(1))
        return cancel_result, room, state.counts()

    cancel_result, room, counts = asyncio.run(exercise(True))
    assert room is None
    assert cancel_result.status == "cancelled"
    assert cancel_result.changed and cancel_result.waiting_changed
    assert counts == (1, 0)

    cancel_result, room, counts = asyncio.run(exercise(False))
    assert room is not None
    assert cancel_result.status == "matched"
    assert cancel_result.room_id == room.room_id
    assert not cancel_result.changed
    assert counts == (0, 1)


def test_replaced_lobby_generation_cannot_wait_or_cancel_against_its_replacement():
    async def exercise():
        state = LobbyState()
        first, replacement = object(), object()
        await state.attach_session(1, first)
        assert await state.wait(1, "alpha", "Hanabi", "host", first)
        _old, _session = await state.attach_session(1, replacement)

        # The old generation is retired even after the replacement has gone through the
        # same account's wait/cancel API.  The waiter remains owned by the replacement.
        assert not await state.wait(1, "stale", "OldTown", "guest", first)
        result = await state.cancel(1, socket=first)
        assert not result.changed
        assert state.waiter(1) is not None
        assert state.waiter(1).socket is replacement

    asyncio.run(exercise())


def test_replaced_accept_is_refused_but_replacement_can_accept_and_replay_is_ordered():
    async def exercise():
        state = LobbyState()
        a_socket, b_old, b_new = object(), object(), object()
        _a_old, a_session = await state.attach_session(
            1, a_socket, {"t": "list", "users": [], "capabilities": ["cancel"]})
        _b_old, b_session = await state.attach_session(
            2, b_old, {"t": "list", "users": [], "capabilities": ["cancel"]})
        assert (await state.next_session_event(a_session))["t"] == "list"
        assert (await state.next_session_event(b_session))["t"] == "list"
        await state.wait(1, "alpha", "Hanabi", "host", a_socket)
        await state.wait(2, "bravo", "Kirie", "guest", b_old)
        invitation = await state.offer_invite(1, 2)
        assert invitation is not None
        # The old target generation has an invite queued, but replacement atomically retires
        # it and seeds list -> replay(invite) in the new generation's FIFO.
        _displaced, b_new_session = await state.attach_session(
            2, b_new, {"t": "list", "users": [], "capabilities": ["cancel"]})
        bootstrap = await state.next_session_event(b_new_session)
        assert bootstrap["t"] == "list"
        assert bootstrap["capabilities"] == ["cancel"]
        assert {row["user_id"] for row in bootstrap["users"]} == {1, 2}
        replay = await state.next_session_event(b_new_session)
        assert replay["t"] == "invite" and replay["invite_id"] == invitation.invite_id
        assert await state.next_session_event(b_session) is None

        # A delayed accept from the displaced target cannot consume the pending invitation;
        # the replacement can accept the same still-current nonce exactly once.
        assert await state.match(1, 2, invitation.invite_id, b_old) is None
        room = await state.match(1, 2, invitation.invite_id, b_new)
        assert room is not None
        assert (await state.next_session_event(a_session))["t"] == "matched"
        assert (await state.next_session_event(b_new_session))["t"] == "matched"

    asyncio.run(exercise())


def test_replaced_lobby_socket_replays_pending_invite_to_new_generation(server):
    a_token, a_id = account(server, "alpha")
    b_token, b_id = account(server, "bravo")
    a = Client(server, a_token)
    b_old = Client(server, b_token)
    b_new = None
    try:
        a.send(t="wait", mode="host", town_name="Hanabi")
        b_old.send(t="wait", mode="guest", town_name="Kirie")
        a.expect_list(2)
        b_old.expect_list(2)
        a.send(t="invite", to=b_id, request_id="replace")
        invitation = b_old.expect("invite")
        assert a.expect("invited")["invite_id"] == invitation["invite_id"]

        # The invitation is committed before the replacement.  Its old delivery is retired,
        # and the new session receives list -> replay(invite) in FIFO order.
        b_new = Client(server, b_token)
        assert b_new.expect("list")["users"]
        replay = b_new.expect("invite")
        assert replay["invite_id"] == invitation["invite_id"]
        b_new.send(t="accept", **{"from": a_id}, invite_id=replay["invite_id"])
        assert a.expect("matched")["invite_id"] == replay["invite_id"]
        assert b_new.expect("matched")["invite_id"] == replay["invite_id"]
    finally:
        a.close()
        b_old.close()
        if b_new is not None:
            b_new.close()


def test_repeated_lobby_replacements_do_not_retain_retired_socket_objects():
    class Socket:
        pass

    async def exercise():
        state = LobbyState()
        current = Socket()
        await state.attach_session(1, current)
        retired = []
        for _ in range(100):
            retired.append(weakref.ref(current))
            current = Socket()
            await state.attach_session(1, current)
        assert len(state._sessions) == 1
        await state.detach(1, current)
        del current
        gc.collect()
        assert all(reference() is None for reference in retired)

    asyncio.run(exercise())
