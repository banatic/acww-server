#!/usr/bin/env python3
"""End-to-end smoke test against a RUNNING container (not the ASGI app).

    python server/tools/smoke.py http://127.0.0.1:18080 [--save <256 KB card image>]  (default: a synthetic ROM-valid image)

The pytest suite runs uvicorn in the test process; this runs nothing at all and only
speaks HTTP and WebSocket to whatever is on that URL.  That is the point: it is the check
that the IMAGE works -- that the pinned wheels installed, that uvicorn can serve a
websocket from inside the container, that the non-root user can write to the mounted
volume, and that 262,144 bytes survive the round trip through the network stack.

Exit 0 means every step passed.  Each step prints a line so a failure says which one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import struct
import sys
from pathlib import Path

import httpx
from websockets.sync.client import connect

TIMEOUT = 60
steps = 0


def step(msg: str) -> None:
    global steps
    steps += 1
    print("  %2d. %s" % (steps, msg))
    sys.stdout.flush()


def check(cond: bool, msg: str) -> None:
    if not cond:
        print("SMOKE FAILED: %s" % msg)
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="smoke test a running acww-online container")
    ap.add_argument("base", help="e.g. http://127.0.0.1:18080")
    ap.add_argument("--save", default=None, help="a 256 KB card image; default: a synthetic ROM-valid image")
    args = ap.parse_args(argv)
    base = args.base.rstrip("/")
    ws_base = "ws" + base[len("http"):]
    if args.save:
        data = Path(args.save).read_bytes()
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
        from conftest import make_sample_image  # noqa: E402
        data = make_sample_image()
    digest = hashlib.sha256(data).hexdigest()
    print("smoke: %s  save %s (%d bytes, sha256 %s)" % (base, args.save, len(data), digest))

    step("GET /v1/health")
    h = httpx.get(base + "/v1/health", timeout=TIMEOUT)
    check(h.status_code == 200, "health returned %d" % h.status_code)
    check(h.json().get("ok") is True, "health said %r" % h.text)
    print("      -> %s" % h.text)

    suffix = secrets.token_hex(3)
    users = {}
    for name in ("smokeA_" + suffix, "smokeB_" + suffix):
        step("POST /v1/auth/register %s" % name)
        r = httpx.post(base + "/v1/auth/register",
                       json={"username": name, "password": secrets.token_hex(12)},
                       timeout=TIMEOUT)
        check(r.status_code == 200, "register returned %d: %s" % (r.status_code, r.text))
        users[name] = r.json()
    a_name, b_name = list(users)
    a, b = users[a_name], users[b_name]
    auth = {"Authorization": "Bearer " + a["token"]}

    step("GET /v1/me")
    me = httpx.get(base + "/v1/me", headers=auth, timeout=TIMEOUT)
    check(me.status_code == 200 and me.json()["save"] is None, "me said %s" % me.text)

    step("PUT /v1/save (262,144 bytes)")
    put = httpx.put(base + "/v1/save", content=data, timeout=TIMEOUT,
                    headers={**auth, "Content-Type": "application/octet-stream"})
    check(put.status_code == 200, "put returned %d: %s" % (put.status_code, put.text))
    check(put.json()["sha256"] == digest, "the server computed a different sha256")
    version = put.json()["version"]
    print("      -> version %d, ETag %s" % (version, put.headers.get("ETag")))

    step("GET /v1/save and compare every byte")
    got = httpx.get(base + "/v1/save", headers=auth, timeout=TIMEOUT)
    check(got.status_code == 200, "get returned %d" % got.status_code)
    check(got.content == data, "the bytes came back DIFFERENT (%d vs %d)"
          % (len(got.content), len(data)))
    check(got.headers.get("X-Save-Sha256") == digest, "X-Save-Sha256 disagrees")
    check(got.headers.get("ETag") == '"%d"' % version, "ETag disagrees")

    step("PUT /v1/save with a stale If-Match -> 412")
    stale = httpx.put(base + "/v1/save", content=data, timeout=TIMEOUT,
                      headers={**auth, "If-Match": '"%d"' % (version - 1),
                               "Content-Type": "application/octet-stream"})
    check(stale.status_code == 412, "stale If-Match returned %d" % stale.status_code)

    step("PUT a corrupt image -> 400 with the reason")
    broken = bytearray(data)
    broken[0x100] ^= 0xFF
    bad = httpx.put(base + "/v1/save", content=bytes(broken), timeout=TIMEOUT,
                    headers={**auth, "Content-Type": "application/octet-stream"})
    check(bad.status_code == 400, "a corrupt image returned %d" % bad.status_code)
    print("      -> %s" % bad.json()["detail"])

    step("GET /v1/save/history")
    hist = httpx.get(base + "/v1/save/history", headers=auth, timeout=TIMEOUT)
    check(hist.status_code == 200 and len(hist.json()) == 1, "history said %s" % hist.text)

    step("WS /v1/lobby/ws x2, wait, accept, matched")
    with connect(ws_base + "/v1/lobby/ws?token=" + a["token"], open_timeout=TIMEOUT) as wa, \
            connect(ws_base + "/v1/lobby/ws?token=" + b["token"], open_timeout=TIMEOUT) as wb:
        wa.send(json.dumps({"t": "wait", "mode": "host", "town_name": "Smoke"}))
        wb.send(json.dumps({"t": "wait", "mode": "guest", "town_name": "Test"}))
        _walk(wa, "list", lambda m: len(m["users"]) == 2)
        wb.send(json.dumps({"t": "accept", "from": a["user_id"]}))
        ma = _walk(wa, "matched")
        mb = _walk(wb, "matched")
        check(ma["room"] == mb["room"], "the two sides got different rooms")
        check(ma["role"] == "parent" and mb["role"] == "child", "the roles are wrong")
        room = ma["room"]
        print("      -> room %d, %s is parent" % (room, a_name))

        step("WS /v1/relay/%d, a frame each way, verbatim" % room)
        with connect("%s/v1/relay/%d?token=%s" % (ws_base, room, a["token"]),
                     open_timeout=TIMEOUT) as ra, \
                connect("%s/v1/relay/%d?token=%s" % (ws_base, room, b["token"]),
                        open_timeout=TIMEOUT) as rb:
            down = struct.pack("<BBH", 1, 12, 5) + b"hello"
            ra.send(down)
            check(rb.recv(timeout=TIMEOUT) == down, "the parent's frame did not arrive intact")
            up = struct.pack("<BBH", 1, 13, 3) + b"ack"
            rb.send(up)
            check(ra.recv(timeout=TIMEOUT) == up, "the child's frame did not arrive intact")
            ra.send(json.dumps({"t": "ping"}))
            check(json.loads(ra.recv(timeout=TIMEOUT)) == {"t": "pong"}, "no pong")

            step("close one side -> peer_left to the survivor, room freed")
            ra.close()
            check(json.loads(rb.recv(timeout=TIMEOUT)) == {"t": "peer_left"},
                  "the survivor was not told")

    step("GET /v1/health again")
    h = httpx.get(base + "/v1/health", timeout=TIMEOUT)
    check(h.json()["users"] >= 2, "health lost the accounts")
    check(h.json()["rooms"] == 0, "the room was not freed: %s" % h.text)
    print("      -> %s" % h.text)

    print("SMOKE OK (%d steps)" % steps)
    return 0


def _walk(ws, kind, extra=None, tries=8):
    for _ in range(tries):
        msg = json.loads(ws.recv(timeout=TIMEOUT))
        if msg.get("t") == kind and (extra is None or extra(msg)):
            return msg
    check(False, "no %r message arrived" % kind)


if __name__ == "__main__":
    sys.exit(main())
