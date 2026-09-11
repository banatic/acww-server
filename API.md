# `server/` API -- the endpoint table, as implemented

The contract is `docs/kb/hybrid/online-spec.md`. This file is that page's endpoint table
plus, at the top, every place the implementation had to decide something the page left
open. The same list is mirrored into the spec under "Deviations and resolutions,
SERVER79, 2026-09-11" -- the two move together or neither is trustworthy.

## Deviations and resolutions (SERVER79, 2026-09-11)

No endpoint, method, status code or message shape named in the spec was changed. These are
decisions, not departures, and the client must follow them.

1. **The `PUT /v1/save` acceptance test is the ROM's whole test, on both banks.** The spec
   says "a checksum that fails the ROM's own bank test". The ROM's own test
   (`func_020a1a40`) is three independent checks, and the checksum is only one of them:
   gamecode byte `+0x0000 == 0x32`, flag byte `+0x173fa == 2`, and the 16-bit wrapping word
   sum over the whole 0x173fc-byte bank `== 0`. All three are run on bank 1 AND on bank 2,
   and the 400's `detail` names the bank and the check that failed. Requiring both banks is
   deliberate: the game mirrors bank 1 into bank 2 after every save, so one good bank is an
   interrupted write, and storing it as the owner's cloud copy would hand a half-written
   town to the next PC.
2. **`{"t":"list"}` carries the array in a `users` field.** The spec wrote `{"t":"list",...}`.
   Each entry is the same object `GET /v1/lobby` returns.
3. **`decline` is echoed to the inviter** as `{"t":"decline","from":<user_id>}`. The spec
   lists `decline` only as a client -> server message; without the echo the inviter's window
   waits forever on an invite that was already refused.
4. **`{"t":"ping"}` / `{"t":"pong"}` are accepted on the lobby socket too**, not only on the
   relay, so one keep-alive serves both sockets.
5. **Role when both peers declared the same mode: the inviter is `parent`.** When the two
   modes differ, `host` is `parent` and `guest` is `child`, exactly as the relay section
   says. The same-mode case is one the client should prevent and the server must not trust.
6. **`PUT /v1/save` returns `updated_utc` as well as `{version, sha256}`, and sets `ETag`.**
   `GET /v1/save/{version}` carries `ETag` and `X-Save-Sha256` like `GET /v1/save`.
7. **Status codes the spec did not name**: `403` when `ACWW_ALLOW_REGISTER=0`, `429` when the
   auth rate limit is spent, `400` for a malformed or non-JSON auth body. Every error body
   is `{"detail": "<reason>"}`.
8. **A refused websocket** is accepted and then closed `1008` (with `{"t":"error"}` first
   where there is something useful to say), or refused with HTTP `403` at the handshake when
   the token cannot be read at all -- the connection is never left open either way.
9. **One lobby socket per account.** A second connection displaces the first, which gets
   `{"t":"error"}` and a `1008` close.
10. **`If-Match: *`** means "there must already be a save" (412 when there is none);
    `W/"7"`, `"7"` and `7` all mean version 7.
11. **A room nobody ever connects to is freed after 300 seconds**, so a match that died
    between `matched` and the relay connect does not hold its id forever.
12. **Environment variables beyond `ACWW_SERVER_SECRET` and `ACWW_ALLOW_REGISTER`**, each
    defaulting to the spec's own value: `ACWW_DATA_DIR` (`/data`), `ACWW_TOKEN_DAYS` (30),
    `ACWW_AUTH_RATE_LIMIT` (10), `ACWW_AUTH_RATE_WINDOW` (60), `ACWW_SAVE_HISTORY` (20),
    `ACWW_TRUST_PROXY` (0). They exist because the tests need several isolated servers in
    one process, and because the rate limiter behind a reverse proxy sees only the proxy
    unless it is told the proxy is there.

## Transport and auth

HTTP/1.1 + JSON on one port (default 8080; TLS is the NAS reverse proxy's job). Bearer
token from login (JWT, HS256, secret from `ACWW_SERVER_SECRET`, 30-day expiry). Registration
open when `ACWW_ALLOW_REGISTER=1`. Passwords hashed with argon2id (fallback bcrypt). Rate
limit 10 auth attempts / minute / IP. All binary bodies are `application/octet-stream`.

## Endpoints

| method + path | body / params | reply |
|---|---|---|
| `POST /v1/auth/register` | `{username, password}` (username 3..24 `[A-Za-z0-9_]`, password >= 8) | `{token, user_id}`; 409 taken; 403 closed; 400 malformed; 429 limited |
| `POST /v1/auth/login` | `{username, password}` | `{token, user_id}`; 401 wrong; 429 limited |
| `GET /v1/me` | bearer | `{user_id, username, save: {version, size, sha256, updated_utc} or null}` |
| `GET /v1/save` | bearer | the 262,144-byte card image, headers `ETag: "<version>"`, `X-Save-Sha256`; 404 if none |
| `PUT /v1/save` | bearer, body = 262,144 bytes, optional `If-Match: "<version>"` | `{version, sha256, updated_utc}` + `ETag`; 412 on a stale `If-Match`; 400 on a wrong size or an image the ROM would refuse, with the reason |
| `GET /v1/save/history` | bearer | `[{version, sha256, size, updated_utc}]`, newest first, the last 20 |
| `GET /v1/save/{version}` | bearer | that version's bytes + `ETag`, `X-Save-Sha256`; 404 once it is pruned |
| `GET /v1/lobby` | bearer | `[{user_id, username, town_name, mode, since_utc}]` -- everyone waiting |
| `WS /v1/lobby/ws?token=` | client sends `{"t":"wait","mode":"host"\|"guest","town_name":...}` / `{"t":"leave"}` / `{"t":"invite","to":id}` / `{"t":"accept","from":id}` / `{"t":"decline","from":id}` / `{"t":"ping"}` | server pushes `{"t":"list","users":[...]}` on connect and on every change, `{"t":"invite","from":{...}}`, `{"t":"decline","from":id}`, `{"t":"matched","room":id,"peer":{...},"role":"parent"\|"child"}` to both, `{"t":"pong"}`, `{"t":"error","msg":...}` |
| `WS /v1/relay/{room}?token=` | only the two matched users | binary frames forwarded verbatim to the other peer; `{"t":"ping"}` -> `{"t":"pong"}`; the room closes when either side leaves and the survivor gets `{"t":"peer_left"}` |
| `GET /v1/health` | -- | `{ok: true, version, users, waiting, rooms}` |

## The relay frame contract

`[u8 kind][u8 port][u16 len][payload]`: kind 1 = MP data, kind 2 = beacon/scan reply,
kind 3 = control. **The server never parses a payload**, and the test suite proves it by
forwarding a frame whose declared `len` disagrees with the bytes that follow. `parent` is
the host ("inviting"), `child` the guest ("visiting").

## Storage

`/data/acww.sqlite` (`users`, `save_versions`; the lobby and the rooms are in memory) and
`/data/saves/<user_id>/<version>.sav`. Backups are the NAS's. No ROM data ever touches the
server.
