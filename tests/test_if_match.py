"""SERVERFIX108 / F7: an `If-Match` this service cannot evaluate is refused, not ignored.

server-audit-1's seventh finding. `_parse_if_match` returned `None` for anything it could
not parse -- a tag LIST like `"0", "999"`, a malformed tag, an empty header -- and `None` was
also the value for "no header at all", which the API deliberately treats as an unconditional
write. So a client or proxy that sent a tag list got its precondition silently DROPPED and
could overwrite a newer version. Measured: a stale single tag returned 412 while two
nonmatching tags returned 200 and version 2.

428 is the code: "your request needs a precondition and the one you sent is not one I can
apply". The supported forms -- `"7"`, `W/"7"`, `7`, `*` -- keep their exact old behaviour,
which is the half that matters to the client (`port/platform/online.c` sends `"<version>"`
and reads the 412's message).
"""

from __future__ import annotations

import httpx
import pytest

from conftest import mutate_save

TIMEOUT = 60

# The three words `_parse_if_match` returns instead of `None`, spelled out rather than
# imported so this module still COLLECTS against the pinned code (where they do not exist)
# and each test fails there for its own reason. `test_the_parser_words_match_the_module`
# pins them to the module's own constants.
IF_MATCH_ABSENT = "absent"
IF_MATCH_INVALID = "invalid"
IF_MATCH_ANY = "any"


@pytest.fixture
def auth(server):
    r = httpx.post(server.base + "/v1/auth/register",
                   json={"username": "mayor", "password": "a long enough password"},
                   timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def put(server, auth, data, if_match=None):
    headers = dict(auth)
    headers["Content-Type"] = "application/octet-stream"
    if if_match is not None:
        headers["If-Match"] = if_match
    return httpx.put(server.base + "/v1/save", content=data, headers=headers,
                     timeout=TIMEOUT)


def version(server, auth) -> int | None:
    save = httpx.get(server.base + "/v1/me", headers=auth, timeout=TIMEOUT).json()["save"]
    return save["version"] if save else None


@pytest.mark.parametrize("raw, expected", [
    (None, IF_MATCH_ABSENT),
    ('"7"', 7),
    ('W/"7"', 7),
    ("7", 7),
    ("*", IF_MATCH_ANY),
    ('"0", "999"', IF_MATCH_INVALID),       # THE FINDING: a tag list
    ('"1","2"', IF_MATCH_INVALID),
    ("", IF_MATCH_INVALID),
    ("   ", IF_MATCH_INVALID),
    ('"abc"', IF_MATCH_INVALID),
    ('"7', IF_MATCH_INVALID),               # an unterminated tag
    ("garbage", IF_MATCH_INVALID),
    ('W/"abc"', IF_MATCH_INVALID),
])
def test_the_parser_tells_absent_from_invalid(raw, expected):
    from app.main import _parse_if_match
    assert _parse_if_match(raw) == expected


def test_the_parser_words_match_the_module():
    from app.main import IF_MATCH_ABSENT as A, IF_MATCH_ANY as Y, IF_MATCH_INVALID as I
    assert (A, I, Y) == (IF_MATCH_ABSENT, IF_MATCH_INVALID, IF_MATCH_ANY)


def test_a_tag_list_is_refused_and_writes_nothing(server, auth, sample_save):
    """THE FINDING, end to end. On the base this is a 200 and version 2."""
    assert put(server, auth, sample_save).json()["version"] == 1
    second = mutate_save(sample_save, 90)
    assert put(server, auth, second, if_match='"1"').json()["version"] == 2

    third = mutate_save(sample_save, 120)
    refused = put(server, auth, third, if_match='"0", "999"')
    assert refused.status_code == 428, refused.text
    assert "If-Match" in refused.json()["detail"]
    assert version(server, auth) == 2
    assert httpx.get(server.base + "/v1/save", headers=auth,
                     timeout=TIMEOUT).content == second


def test_a_malformed_tag_is_refused_and_writes_nothing(server, auth, sample_save):
    assert put(server, auth, sample_save).json()["version"] == 1
    for bad in ('"abc"', "", '"1', "W/", "not-a-tag"):
        r = put(server, auth, mutate_save(sample_save, 33), if_match=bad)
        assert r.status_code == 428, (bad, r.status_code)
    assert version(server, auth) == 1


def test_the_refusal_happens_before_the_body_is_read(server, auth):
    """428 for a precondition is decided before the upload is accepted, so a client that
    sent an unsupported header does not also pay 256 KB of upstream for the refusal.

    The body here is deliberately NOT a valid card image and NOT the right size: if the
    order were the other way round this would be a 400 or a 413 about the body.
    """
    r = put(server, auth, b"\x00" * 1024, if_match='"0", "999"')
    assert r.status_code == 428, r.text


def test_the_supported_forms_are_unchanged(server, auth, sample_save):
    """M1's control, and the client's contract: every form the API documents still works."""
    assert put(server, auth, sample_save).json()["version"] == 1
    assert put(server, auth, mutate_save(sample_save, 2), if_match='"1"'
               ).json()["version"] == 2
    assert put(server, auth, mutate_save(sample_save, 3), if_match='W/"2"'
               ).json()["version"] == 3
    assert put(server, auth, mutate_save(sample_save, 4), if_match="3"
               ).json()["version"] == 4
    assert put(server, auth, mutate_save(sample_save, 5), if_match="*"
               ).json()["version"] == 5
    stale = put(server, auth, mutate_save(sample_save, 6), if_match='"1"')
    assert stale.status_code == 412 and "version 5" in stale.json()["detail"]
    assert put(server, auth, mutate_save(sample_save, 7)).json()["version"] == 6


def test_if_match_star_on_an_empty_account_is_still_412(server, auth, sample_save):
    r = put(server, auth, sample_save, if_match="*")
    assert r.status_code == 412, r.text
    assert "no save" in r.json()["detail"]
