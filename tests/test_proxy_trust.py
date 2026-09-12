"""SERVERFIX108 / F1: `X-Forwarded-For` is evidence only when it comes from a known proxy.

server-audit-1's highest finding. `ACWW_TRUST_PROXY=1` -- which the compose file sets and
README-ko tells every operator to set -- made `_client_key` return the FIRST element of
`X-Forwarded-For`, and the nginx line the same README documents is
`proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`, which PRESERVES whatever the
caller sent and appends the real client. So the first element was attacker-chosen: a new
invented prefix bought a fresh unauthenticated login budget every time, and repeating
someone else's prefix spent theirs. Measured on the pinned code as `[401, 401, 429, 401]`
with limit 2 -- the fourth attempt was a forged prefix walking around a spent window.

These tests pin the three things that make the header safe:

1. by DEFAULT (`ACWW_TRUSTED_PROXIES` empty) the header does nothing at all, so a directly
   reachable server cannot be talked around;
2. behind a DECLARED proxy the chain is read from the RIGHT, because an appending proxy
   writes the right-hand end -- so a forged prefix cannot change the key;
3. the limiter's key map is BOUNDED, which was the same finding's other half: invented
   prefixes used to accumulate a bucket each, for ever, unauthenticated.

They fail on the base: `Settings` has no `trusted_proxies` there, and with the legacy
`trust_proxy=True` alone the forged prefix is the key.
"""

from __future__ import annotations

import httpx
import pytest

# The two unit tests below import from `app.security` INSIDE the test body on purpose: this
# file has to be collectable against the pinned code so that each behavioural test fails
# there for its own reason, rather than the whole module failing to import.

TIMEOUT = 30
CREDS = {"username": "villager", "password": "correct horse battery"}


def login(server, xff: str | None = None):
    headers = {}
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    return httpx.post(server.base + "/v1/auth/login", json=CREDS, headers=headers,
                      timeout=TIMEOUT)


def codes(server, prefixes) -> list[int]:
    return [login(server, p).status_code for p in prefixes]


def test_a_forged_prefix_cannot_buy_a_fresh_budget_by_default(server_factory):
    """The default deployment: no declared proxy, so the header is not read.

    Every request really comes from 127.0.0.1, and every one of them spends the same bucket
    however the caller decorates it. The pinned code answered the third of these 401 by
    switching prefix; it must be 429.
    """
    s = server_factory("default", auth_rate_limit=2, auth_rate_window=60)
    seen = codes(s, [None, "9.9.9.9", "10.10.10.10", "203.0.113.5, 198.51.100.9"])
    assert seen[:2] == [401, 401], seen        # the two the window allows
    assert seen[2:] == [429, 429], seen        # forging the prefix changes nothing


def test_a_forged_prefix_cannot_buy_a_fresh_budget_behind_a_declared_proxy(server_factory):
    """The documented NAS shape, with the proxy declared.

    `$proxy_add_x_forwarded_for` appends, so what arrives is `<whatever the caller sent>,
    <the real client>`. The real client is the LAST untrusted hop, and it is the same for
    all four requests below however the prefix changes.
    """
    s = server_factory("declared", auth_rate_limit=2, auth_rate_window=60,
                       trust_proxy=True, trusted_proxies=("127.0.0.1",))
    real = "203.0.113.7"
    seen = codes(s, ["9.9.9.9, " + real,
                     "10.10.10.10, " + real,
                     "172.16.9.9, 10.0.0.9, " + real,
                     real])
    assert seen[:2] == [401, 401], seen
    assert seen[2:] == [429, 429], seen


def test_a_declared_proxy_still_separates_two_real_clients(server_factory):
    """M1, the other direction: the fix must not make the header useless.

    A limit that counted everyone behind the proxy as one client would be the bug
    ACWW_TRUST_PROXY existed to avoid. Two different real clients keep two budgets.
    """
    s = server_factory("separate", auth_rate_limit=2, auth_rate_window=60,
                       trust_proxy=True, trusted_proxies=("127.0.0.0/8",))
    assert codes(s, ["203.0.113.7", "203.0.113.7"]) == [401, 401]
    assert login(s, "203.0.113.7").status_code == 429          # that client is spent ...
    assert login(s, "198.51.100.4").status_code == 401         # ... and this one is not


def test_an_undeclared_peer_is_the_key_even_when_it_forwards(server_factory):
    """A header from an address the operator never declared is ignored entirely."""
    s = server_factory("undeclared", auth_rate_limit=2, auth_rate_window=60,
                       trust_proxy=True, trusted_proxies=("10.1.2.3",))
    seen = codes(s, ["203.0.113.7", "198.51.100.4", "192.0.2.9"])
    assert seen == [401, 401, 429], seen


@pytest.mark.parametrize("spec, address, expected", [
    ("127.0.0.1", "127.0.0.1", True),
    ("127.0.0.0/8", "127.0.0.9", True),
    ("10.0.0.0/8", "203.0.113.7", False),
    ("not-an-address", "127.0.0.1", False),      # a typo trusts NOTHING, not everything
    ("::1", "::1", True),
    ("127.0.0.1", "", False),
])
def test_the_trusted_proxy_matcher(spec, address, expected):
    from app.security import is_trusted_proxy
    assert is_trusted_proxy(address, (spec,)) is expected
    assert is_trusted_proxy(address, ()) is False        # nothing declared, nothing trusted


def test_the_limiter_key_map_is_bounded():
    """F1's second half: a bucket per invented prefix, for ever, without an account.

    `max_keys` is small here so the bound is visible in a test rather than in a memory
    graph. Two properties: the map is bounded, and a key that is STILL BEING USED keeps its
    spent window while the noise churns around it -- eviction is least-recently-used, so the
    only bucket a flood can drop is one whose owner has stopped making requests, and for
    that owner a dropped bucket is a fresh window rather than a wrong refusal.
    """
    from app.security import RateLimiter
    limiter = RateLimiter(limit=2, window=60, max_keys=8)
    for i in range(500):
        assert limiter.allow("invented-%d" % i) is True
    assert limiter.keys() <= 8

    steady = RateLimiter(limit=2, window=60, max_keys=8)
    assert steady.allow("mine") is True
    assert steady.allow("mine") is True
    for i in range(200):
        steady.allow("noise-%d" % i)
        assert steady.allow("mine") is False       # spent, and never evicted while active
    assert steady.keys() <= 8
