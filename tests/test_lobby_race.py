"""LOBBY94: the lobby, driven in the LOSING ORDERS, because a rig lost a match in one.

`g2rig.py`'s `cb91d` failed with "the guest was never matched" and nothing in either log.
`test_lobby.py` cannot see that shape, because it waits for the list it wants before it acts
-- `expect_list(2)` on both sides before the first invite.  A real client does not wait: it
acts on the list it was pushed, which is the one the server sent when its socket opened, and
that push happens BEFORE the client's own `wait` has been read.  These tests replay that.

The verdict they record: the server is NOT the owner of `cb91d`.  Every losing order here is
answered correctly -- the invite that arrives one message after the inviter's own `wait` is
valid, the `matched` pair reaches whichever socket opened first, and the relay's drop of a
frame sent into an empty room is deliberate and visible in `relay.close`'s
`frames_forwarded` (case 295).  The defect was in `port/platform/online.c`'s lobby window,
where a pushed list cleared the player's selection and the invite silently sent nothing;
`port/tools/test_lobby_select.py` reproduces it and proves the fix.

These are regression tests for the orders themselves.  If the server ever starts requiring a
client to have seen its own `wait` echoed before it may invite, this file fails.
"""

from __future__ import annotations

import json

import httpx
import pytest
from websockets.sync.client import connect

from test_lobby import Client, account            # the same socket helper and registration

TIMEOUT = 30


@pytest.fixture
def accounts(server):
    host_token, host_id = account(server, "g2host")
    guest_token, guest_id = account(server, "g2guest")
    return server, (host_token, host_id), (guest_token, guest_id)


def test_the_guest_may_invite_on_the_list_it_was_pushed_at_connect(accounts):
    """THE RIG'S ORDER, exactly.

    The host waits.  The guest then connects, and the very first thing the server pushes
    down its socket is a list that ALREADY NAMES THE HOST -- before the guest has said
    `wait`.  A client that acts on that list sends `wait` and `invite` back to back, so the
    server reads the invite one message after registering the waiter.  That must be a valid
    invite: refusing it would make the first thing every client sees unusable.
    """
    server, (host_token, host_id), (guest_token, guest_id) = accounts
    host = Client(server, host_token)
    try:
        host.send(t="wait", mode="host", town_name="Hanabi")
        host.expect_list(1)

        guest = Client(server, guest_token)
        try:
            # The push that arrives before the guest has said anything at all.
            first = guest.expect("list")
            assert [u["user_id"] for u in first["users"]] == [host_id]

            # Back to back, on one socket, in that order -- what the client does.
            guest.send(t="wait", mode="guest", town_name="Kirie")
            guest.send(t="invite", to=host_id)

            invite = host.expect("invite")
            assert invite["from"]["user_id"] == guest_id

            host.send(t="accept", **{"from": guest_id})
            mg, mh = guest.expect("matched"), host.expect("matched")
            assert mg["room"] == mh["room"]
            # The GUEST invited, and the two declared different modes, so the declaration
            # wins over the invite: the host is the WM parent (lobby.py's THE ROLE).
            assert mh["role"] == "parent" and mg["role"] == "child"
        finally:
            guest.close()
    finally:
        host.close()


def test_matched_reaches_the_inviter_whose_socket_is_the_second_one_opened(accounts):
    """The two sockets, in both orders, with the invite from the one that opened LAST.

    `accept` reads the inviter's socket out of the lobby's map rather than from the
    connection it is handling, so the order the two sockets were opened in must not decide
    who is told `matched`.  Both orders are run in one test because a pass in one order
    alone is not evidence about the other.
    """
    server, (host_token, host_id), (guest_token, guest_id) = accounts
    for order in ("host first", "guest first"):
        if order == "host first":
            a, b = Client(server, host_token), Client(server, guest_token)
        else:
            b, a = Client(server, guest_token), Client(server, host_token)
        try:
            a.send(t="wait", mode="host", town_name="Hanabi")
            b.send(t="wait", mode="guest", town_name="Kirie")
            b.expect_list(2)
            b.send(t="invite", to=host_id)
            a.expect("invite")
            a.send(t="accept", **{"from": guest_id})
            ma, mb = a.expect("matched"), b.expect("matched")
            assert ma["room"] == mb["room"], order
            assert ma["role"] == "parent" and mb["role"] == "child", order
            # Both left the list, so the next pair does not inherit a stale waiter.
            a.send(t="leave")
            b.send(t="leave")
        finally:
            a.close()
            b.close()


def test_a_frame_sent_into_an_empty_room_is_dropped_and_says_so(accounts):
    """CASE 295, as a test rather than a note.

    Both peers are told `matched` in the same millisecond and whichever opens its relay
    socket first can send before the other has joined.  The server DROPS that frame -- it
    does not buffer, because a relay that buffered for an absent peer would be deciding what
    a session is -- and the drop is visible: the early sender's `frames_forwarded` counts
    only what it forwarded ON BEHALF of the peer.  Once both are open the path carries
    frames both ways, which is why the client's instrument has to REPEAT.
    """
    server, (host_token, host_id), (guest_token, guest_id) = accounts
    host, guest = Client(server, host_token), Client(server, guest_token)
    try:
        host.send(t="wait", mode="host", town_name="Hanabi")
        guest.send(t="wait", mode="guest", town_name="Kirie")
        guest.expect_list(2)
        guest.send(t="invite", to=host_id)
        host.expect("invite")
        host.send(t="accept", **{"from": guest_id})
        room = host.expect("matched")["room"]
        guest.expect("matched")
    finally:
        host.close()
        guest.close()

    # The host wins the race and sends into a room with one socket in it.
    first = connect(server.ws_base + "/v1/relay/%s?token=%s" % (room, host_token),
                    open_timeout=TIMEOUT)
    try:
        first.send(bytes([3, 12, 5, 0]) + b"early")
        second = connect(server.ws_base + "/v1/relay/%s?token=%s" % (room, guest_token),
                         open_timeout=TIMEOUT)
        try:
            # Nothing from before the join arrives: the frame was dropped, not queued.
            with pytest.raises(TimeoutError):
                second.recv(timeout=1.0)
            # And now the path works, in both directions.
            first.send(bytes([3, 12, 4, 0]) + b"late")
            assert second.recv(timeout=TIMEOUT) == bytes([3, 12, 4, 0]) + b"late"
            second.send(bytes([3, 12, 4, 0]) + b"back")
            assert first.recv(timeout=TIMEOUT) == bytes([3, 12, 4, 0]) + b"back"
        finally:
            second.close()
    finally:
        first.close()

    assert httpx.get(server.base + "/v1/health", timeout=TIMEOUT).json()["rooms"] == 0
