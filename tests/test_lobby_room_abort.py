"""Gate preparation cancellation must revoke exactly the displayed room."""
import asyncio
import json
from contextlib import ExitStack

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from app.lobby import LobbyState
from test_lobby import Client, account


@pytest.mark.parametrize("connected", [False, True])
def test_abort_endpoint_revokes_room_and_scopes_ack(server, connected):
    ta, aid = account(server, "abortalpha")
    tb, bid = account(server, "abortbravo")
    a, b = Client(server, ta), Client(server, tb)
    relays = ExitStack()
    try:
        a.send(t="wait", mode="host")
        b.send(t="wait", mode="guest")
        a.expect_list(2)
        b.expect_list(2)
        a.send(t="invite", to=bid)
        nonce = b.expect("invite")["invite_id"]
        a.expect("invited")
        b.send(t="accept", **{"from": aid}, invite_id=nonce)
        room = b.expect("matched")["room"]
        assert a.expect("matched")["room"] == room
        peers = []
        if connected:
            for token in (ta, tb):
                peer = relays.enter_context(connect(
                    f"{server.ws_base}/v1/relay/{room}",
                    additional_headers={"Authorization": f"Bearer {token}"},
                    proxy=None, open_timeout=10))
                peer.send('{"t":"ping"}')
                assert json.loads(peer.recv(timeout=10)) == {"t": "pong"}
                peers.append(peer)
        b.send(t="abort_room", room=room + 1, request_id="stale")
        assert b.expect("error")["request_id"] == "stale"
        b.send(t="abort_room", room=room, request_id="cancel-prepare")
        notice = {"t": "room_aborted", "room": room, "by": bid}
        assert b.expect("room_aborted") == notice
        assert a.expect("room_aborted") == notice
        assert b.expect("room_abort_ack") == {
            "t": "room_abort_ack", "room": room, "request_id": "cancel-prepare"}
        for peer in peers:
            assert json.loads(peer.recv(timeout=10)) == {"t": "peer_left"}
            with pytest.raises(ConnectionClosed):
                peer.recv(timeout=10)
        b.send(t="abort_room", room=room, request_id="old-retry")
        assert b.expect("error")["request_id"] == "old-retry"
        # Both sockets remain usable for a fresh visit.
        a.send(t="wait", mode="host")
        b.send(t="wait", mode="guest")
        a.expect_list(2)
        b.expect_list(2)
    finally:
        relays.close()
        a.close()
        b.close()


def test_abort_checks_membership_room_and_current_lobby_generation():
    async def exercise():
        state = LobbyState()
        a, old_b, new_b = object(), object(), object()
        _, sa = await state.attach_session(1, a)
        _, sb = await state.attach_session(2, old_b)
        await state.wait(1, "alpha", "A", "host", a)
        await state.wait(2, "bravo", "B", "guest", old_b)
        invitation = await state.offer_invite(1, 2)
        room = await state.match(1, 2, invitation.invite_id)
        ra, rb = object(), object()
        _, ga, _ = await state.join_room(room.room_id, 1, ra)
        await state.join_room(room.room_id, 2, rb)
        _, replacement = await state.attach_session(2, new_b)

        assert await state.abort_room(room.room_id, 2, old_b, sb.generation) is None
        assert await state.abort_room(room.room_id + 1, 2, new_b,
                                      replacement.generation) is None
        assert await state.abort_room(room.room_id, 3, a, sa.generation) is None
        assert await state.may_forward(room.room_id, 1, ga) is rb

        sockets = await state.abort_room(room.room_id, 2, new_b,
                                         replacement.generation)
        assert set(sockets) == {ra, rb}
        assert await state.may_forward(room.room_id, 1, ga) is None
        assert await state.join_room(room.room_id, 1, object()) is None
        assert await state.abort_room(room.room_id, 2, new_b,
                                      replacement.generation) is None
        assert not room.gens and not room.sockets
        for session in (sa, replacement):
            notices = [event for event in session.outbox if event["t"] == "room_aborted"]
            assert notices == [{"t": "room_aborted", "room": room.room_id, "by": 2}]
        # A delayed old-room abort cannot touch the next visit.
        await state.wait(1, "alpha", "A", "host", a)
        await state.wait(2, "bravo", "B", "guest", new_b)
        invitation = await state.offer_invite(1, 2)
        fresh = await state.match(1, 2, invitation.invite_id)
        assert fresh.room_id != room.room_id
        assert await state.abort_room(room.room_id, 2, new_b,
                                      replacement.generation) is None
        assert await state.join_room(fresh.room_id, 1, object()) is not None

    asyncio.run(exercise())
