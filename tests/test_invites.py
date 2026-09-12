"""SERVERFIX108 / F4: a match needs an invitation the server is actually holding.

server-audit-1's fourth finding. `accept` checked only that both parties were WAITING, and
nothing in the server was a record of an invitation -- `invite` was forwarded and forgotten.
So an account could read a victim's id out of the waiting list, send
`{"t":"accept","from":<victim>}`, and both were taken out of the list and told `matched`:
a consent bypass that also interferes with somebody who was in the middle of choosing a
different partner. `server/tests/test_relay.py`'s own fixture relied on it, which is how a
hole stays open -- the test suite was pinning it.

The state machine these tests pin:

    invite(a -> b)        records a DIRECTED pending invitation, expiring after invite_ttl
    accept(b, from=a)     consumes exactly that one, atomically, inside the room's lock
    decline(b, from=a)    clears it
    leave / socket lost   clears every invitation the user is either end of
    anything else         "that invite is no longer valid"

`test_a_bare_accept_is_refused` is the finding itself and fails on the base.
"""

from __future__ import annotations

import httpx
import pytest

from test_lobby import Client, account

TIMEOUT = 30


@pytest.fixture
def two(server):
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


def rooms(server) -> int:
    return httpx.get(server.base + "/v1/health", timeout=TIMEOUT).json()["rooms"]


def waiting(server) -> int:
    return httpx.get(server.base + "/v1/health", timeout=TIMEOUT).json()["waiting"]


def test_a_bare_accept_is_refused(two):
    """THE FINDING. No invite was ever sent, so there is nothing to accept.

    Both must still be waiting afterwards: the attack was not only "I get a room", it was
    "I drag somebody out of the list they were choosing from".
    """
    server, (a, a_id), (b, b_id) = two
    b.send(t="accept", **{"from": a_id})
    assert "no longer valid" in b.expect("error")["msg"]
    assert rooms(server) == 0
    assert waiting(server) == 2


def test_an_invitation_is_directed_and_not_a_licence_to_match_anyone(server):
    """A pending invitation from a to b does not let b accept as if it came from c."""
    a_token, a_id = account(server, "alpha")
    b_token, b_id = account(server, "bravo")
    c_token, c_id = account(server, "charlie")
    a, b, c = Client(server, a_token), Client(server, b_token), Client(server, c_token)
    try:
        a.send(t="wait", mode="host", town_name="Hanabi")
        b.send(t="wait", mode="guest", town_name="Kirie")
        c.send(t="wait", mode="host", town_name="Tsubaki")
        b.expect_list(3)
        a.send(t="invite", to=b_id)
        b.expect("invite")
        b.send(t="accept", **{"from": c_id})          # c never invited anybody
        assert "no longer valid" in b.expect("error")["msg"]
        assert rooms(server) == 0
        # And the real invitation still works, so the refusal above was about direction.
        b.send(t="accept", **{"from": a_id})
        assert b.expect("matched")["room"] == a.expect("matched")["room"]
    finally:
        a.close()
        b.close()
        c.close()


def test_the_invitation_runs_the_other_way_only(two):
    """b was invited; that does not mean a may accept from b."""
    server, (a, a_id), (b, b_id) = two
    a.send(t="invite", to=b_id)
    b.expect("invite")
    a.send(t="accept", **{"from": b_id})
    assert "no longer valid" in a.expect("error")["msg"]
    assert rooms(server) == 0


def test_an_invitation_is_consumed_once(two):
    """A second accept of the same invitation is refused -- including after the room it made
    has been freed, which is the replay an attacker would try."""
    server, (a, a_id), (b, b_id) = two
    a.send(t="invite", to=b_id)
    b.expect("invite")
    b.send(t="accept", **{"from": a_id})
    b.expect("matched")
    a.expect("matched")

    a.send(t="wait", mode="host", town_name="Hanabi")
    b.send(t="wait", mode="guest", town_name="Kirie")
    b.expect_list(2)
    b.send(t="accept", **{"from": a_id})              # the old invitation, again
    assert "no longer valid" in b.expect("error")["msg"]
    assert rooms(server) == 1                          # only the first match's room


def test_a_declined_invitation_cannot_be_accepted_afterwards(two):
    """F4's "clear it on decline". A refusal that left the invitation pending would let the
    same accept work a minute later, which is the finding with an extra step."""
    server, (a, a_id), (b, b_id) = two
    a.send(t="invite", to=b_id)
    b.expect("invite")
    b.send(t="decline", **{"from": a_id})
    a.expect("decline")
    b.send(t="accept", **{"from": a_id})
    assert "no longer valid" in b.expect("error")["msg"]
    assert rooms(server) == 0
    # A fresh invitation is a fresh consent and does work.
    a.send(t="invite", to=b_id)
    b.expect("invite")
    b.send(t="accept", **{"from": a_id})
    assert b.expect("matched")["room"] == a.expect("matched")["room"]


def test_leaving_the_list_clears_the_invitation(two):
    """`leave` is the player saying "not now". The pending invitation goes with it."""
    server, (a, a_id), (b, b_id) = two
    a.send(t="invite", to=b_id)
    b.expect("invite")
    a.send(t="leave")
    b.expect_list(1)
    a.send(t="wait", mode="host", town_name="Hanabi")
    b.expect_list(2)
    b.send(t="accept", **{"from": a_id})
    assert "no longer valid" in b.expect("error")["msg"]
    assert rooms(server) == 0


def test_a_lost_socket_clears_the_invitation(server):
    """A socket that is gone cannot consent to anything. The invitation dies with it, even
    though the account reconnects and waits again a moment later."""
    a_token, a_id = account(server, "alpha")
    b_token, b_id = account(server, "bravo")
    a, b = Client(server, a_token), Client(server, b_token)
    try:
        a.send(t="wait", mode="host", town_name="Hanabi")
        b.send(t="wait", mode="guest", town_name="Kirie")
        b.expect_list(2)
        a.send(t="invite", to=b_id)
        b.expect("invite")
        a.close()
        b.expect_list(1)
        a = Client(server, a_token)
        a.send(t="wait", mode="host", town_name="Hanabi")
        b.expect_list(2)
        b.send(t="accept", **{"from": a_id})
        assert "no longer valid" in b.expect("error")["msg"]
        assert rooms(server) == 0
    finally:
        a.close()
        b.close()


def test_an_expired_invitation_is_refused(server_factory):
    """The expiry, with `invite_ttl` turned down to nothing so the test does not wait.

    An invitation that never expired would accumulate consent: a player who was asked an
    hour ago and walked away has not agreed to be matched when they come back.
    """
    s = server_factory("ttl", invite_ttl=0.0)
    a_token, a_id = account(s, "alpha")
    b_token, b_id = account(s, "bravo")
    a, b = Client(s, a_token), Client(s, b_token)
    try:
        a.send(t="wait", mode="host", town_name="Hanabi")
        b.send(t="wait", mode="guest", town_name="Kirie")
        b.expect_list(2)
        a.send(t="invite", to=b_id)
        b.expect("invite")
        b.send(t="accept", **{"from": a_id})
        assert "no longer valid" in b.expect("error")["msg"]
        assert rooms(s) == 0
    finally:
        a.close()
        b.close()


def test_the_ordinary_invite_accept_path_still_works(two):
    """M1's control: every refusal above would also pass if `accept` were simply broken."""
    server, (a, a_id), (b, b_id) = two
    a.send(t="invite", to=b_id)
    assert b.expect("invite")["from"]["user_id"] == a_id
    b.send(t="accept", **{"from": a_id})
    ma, mb = a.expect("matched"), b.expect("matched")
    assert ma["room"] == mb["room"]
    assert ma["role"] == "parent" and mb["role"] == "child"
    assert rooms(server) == 1 and waiting(server) == 0
