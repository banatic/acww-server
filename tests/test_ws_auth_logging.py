"""SERVERFIX108 / F2: a websocket authenticates in a HEADER, and no log line holds a token.

server-audit-1 measured this against the pinned uvicorn: opening `/v1/lobby/ws?token=<jwt>`
makes uvicorn format `'%s - "WebSocket %s" [accepted]'` with the path AND its query string,
on the `uvicorn.error` logger at INFO. That record is outside `logging_._FORBIDDEN` and
outside `--no-access-log`, and it carries a reusable 30-day bearer credential into every log
export, backup and shipped log stream.

Two halves, and this file tests both:

* the TRANSPORT moved. `Authorization: Bearer <jwt>` on the upgrade is what the native
  client now sends (`port/platform/online.c`, `ws_open`), so in the ordinary case there is
  no token anywhere near a URL. `?token=` still works for one release, because a player's
  `dist/acww.exe` is repacked on their schedule and not the server's.
* the LOG was redacted anyway. Defence in depth for the compatibility path, for a browser
  that cannot set a header, and for anything else that ever puts a secret in a query.

THE TOKEN IS NEVER WRITTEN ANYWHERE BY THIS FILE. Records are inspected in memory, the
assertion is a substring test, and what is printed on failure is a boolean -- never the
record. A test that proved a credential does not leak by printing it would be the funniest
possible way to fail this review.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

TIMEOUT = 30
REFUSED = (ConnectionClosed, InvalidStatus, OSError)


def account(server, name="villager"):
    r = httpx.post(server.base + "/v1/auth/register",
                   json={"username": name, "password": "a long enough password"},
                   timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()["token"], r.json()["user_id"]


class Captured(logging.Handler):
    """Every record the uvicorn loggers emit, rendered, held in memory only."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(record.getMessage())
        except Exception:                                 # pragma: no cover
            self.lines.append("<unformattable>")


@pytest.fixture
def captured():
    """Attach to uvicorn's own loggers for the length of one test, then detach.

    `logging_.install_query_redaction` puts its filter on the LOGGER, which runs before any
    handler, so what this handler sees is exactly what a real handler -- a file, a shipper,
    `docker logs` -- would see.

    INFO AND NOT DEBUG, and that is a measurement decision (M1). At DEBUG the `websockets`
    library -- which uvicorn hands `uvicorn.error` as its logger -- prints the whole
    handshake including every request header, so a fixture at DEBUG reports a leak for the
    header transport too and the test fails for the wrong reason. It found exactly that on
    its first run. DEBUG is a mode an operator turns on knowing they are printing wire
    traffic; the finding is about the record that appears at the DEFAULT level.
    """
    handler = Captured()
    handler.setLevel(logging.INFO)
    names = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
    touched = []
    for name in names:
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        touched.append((logger, logger.level, logger.propagate))
        logger.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        for logger, level, propagate in touched:
            logger.removeHandler(handler)
            logger.setLevel(level)
            logger.propagate = propagate
        handler.lines.clear()


def _no_token_in(lines, token: str) -> bool:
    """True when no captured line holds the credential or a recognisable slice of it."""
    parts = [token] + [p for p in token.split(".") if len(p) > 12]
    return not any(any(p in line for p in parts) for line in lines)


def test_the_websocket_accept_record_holds_no_token(server, captured):
    """The finding, as a regression: the query form still works and still does not leak.

    On the base this fails -- the accepted-handshake record contains the whole JWT.
    """
    token, _ = account(server)
    with connect(server.ws_base + "/v1/lobby/ws?token=" + token,
                 open_timeout=TIMEOUT) as ws:
        assert json.loads(ws.recv(timeout=TIMEOUT))["t"] == "list"

    assert captured.lines, "no uvicorn record was captured at all (M1: check the fixture)"
    assert any("WebSocket" in line or "/v1/lobby/ws" in line for line in captured.lines), \
        "the handshake was never logged, so this test would pass for the wrong reason"
    assert _no_token_in(captured.lines, token)
    assert any("<redacted>" in line for line in captured.lines)


def test_the_refused_websocket_record_holds_no_token(server, captured):
    """A REFUSAL logs too, and server-audit-1 named both records. A wrong token is still a
    secret -- it may be a right one for a different server, or a typo of the right one."""
    token, _ = account(server)
    forged = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    with pytest.raises(REFUSED):
        ws = connect(server.ws_base + "/v1/lobby/ws?token=" + forged, open_timeout=TIMEOUT)
        ws.recv(timeout=TIMEOUT)
    assert _no_token_in(captured.lines, forged)


def test_the_header_authenticates_the_lobby_socket(server, captured):
    """F2's real fix: no token in the URL at all.

    `additional_headers` is what WinHTTP's `WinHttpAddRequestHeaders` does on the upgrade in
    `ws_open`. The URL that reaches the log has no query string to redact.
    """
    token, user_id = account(server)
    with connect(server.ws_base + "/v1/lobby/ws",
                 additional_headers={"Authorization": "Bearer " + token},
                 open_timeout=TIMEOUT) as ws:
        first = json.loads(ws.recv(timeout=TIMEOUT))
        assert first["t"] == "list"
        ws.send(json.dumps({"t": "wait", "mode": "host", "town_name": "Hanabi"}))
        for _ in range(8):
            msg = json.loads(ws.recv(timeout=TIMEOUT))
            if msg.get("t") == "list" and len(msg["users"]) == 1:
                break
        else:                                             # pragma: no cover
            raise AssertionError("the header-authenticated socket never saw its own wait")
        assert msg["users"][0]["user_id"] == user_id
    assert _no_token_in(captured.lines, token)


def test_the_header_authenticates_the_relay_socket(server):
    """Both sockets moved, so both are tested: the relay is the one that carries the game."""
    a_token, a_id = account(server, "alpha")
    b_token, b_id = account(server, "bravo")

    def sock(token):
        return connect(server.ws_base + "/v1/lobby/ws",
                       additional_headers={"Authorization": "Bearer " + token},
                       open_timeout=TIMEOUT)

    def walk_to(ws, kind, users=None):
        """The next message of a kind -- the server pushes a list on every change, so a test
        that wants `matched` has to be allowed past the lists (test_lobby's own rule)."""
        for _ in range(10):
            msg = json.loads(ws.recv(timeout=TIMEOUT))
            if msg.get("t") != kind:
                continue
            if users is not None and len(msg.get("users", [])) != users:
                continue
            return msg
        raise AssertionError("no %r arrived" % kind)       # pragma: no cover

    a, b = sock(a_token), sock(b_token)
    try:
        a.send(json.dumps({"t": "wait", "mode": "host", "town_name": "Hanabi"}))
        b.send(json.dumps({"t": "wait", "mode": "guest", "town_name": "Kirie"}))
        # Both waits must be REGISTERED before the invite, or the invite races them and is
        # answered "that player is not waiting" -- measured on this test's first run.
        walk_to(a, "list", users=2)
        a.send(json.dumps({"t": "invite", "to": b_id}))
        walk_to(b, "invite")
        b.send(json.dumps({"t": "accept", "from": a_id}))
        room = walk_to(a, "matched")["room"]
    finally:
        a.close()
        b.close()

    def relay(token):
        return connect("%s/v1/relay/%s" % (server.ws_base, room),
                       additional_headers={"Authorization": "Bearer " + token},
                       open_timeout=TIMEOUT)

    with relay(a_token) as parent, relay(b_token) as child:
        out = bytes([3, 0, 3, 0]) + b"\x01\x02\x03"
        parent.send(out)
        assert child.recv(timeout=TIMEOUT) == out


def test_a_bad_header_token_is_refused_like_a_bad_query_token(server):
    with pytest.raises(REFUSED):
        ws = connect(server.ws_base + "/v1/lobby/ws",
                     additional_headers={"Authorization": "Bearer nonsense"},
                     open_timeout=TIMEOUT)
        ws.recv(timeout=TIMEOUT)


def test_the_header_wins_over_a_query_token(server):
    """A client mid-migration may send both. The header is the one that counts, so a stale
    `?token=` left in a URL cannot authenticate as somebody else."""
    good, good_id = account(server, "alpha")
    other, _ = account(server, "bravo")
    with connect(server.ws_base + "/v1/lobby/ws?token=" + other,
                 additional_headers={"Authorization": "Bearer " + good},
                 open_timeout=TIMEOUT) as ws:
        assert json.loads(ws.recv(timeout=TIMEOUT))["t"] == "list"
        ws.send(json.dumps({"t": "wait", "mode": "host", "town_name": "Hanabi"}))
        for _ in range(8):
            msg = json.loads(ws.recv(timeout=TIMEOUT))
            if msg.get("t") == "list" and msg["users"]:
                assert msg["users"][0]["user_id"] == good_id
                return
        raise AssertionError("no list named the header's account")     # pragma: no cover
