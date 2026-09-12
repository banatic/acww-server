# `server/` API -- the endpoint table, as implemented

The contract is `docs/kb/hybrid/online-spec.md`. This file is that page's endpoint table
plus, at the top, every place the implementation had to decide something the page left
open. **That list is now HERE ONLY** -- the spec used to carry a verbatim copy and points at
this one instead (serverfix108), because two copies of a contract is one copy plus a thing
that goes stale.

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
    `ACWW_AUTH_RATE_LIMIT` (10), `ACWW_AUTH_RATE_WINDOW` (60), `ACWW_SAVE_HISTORY` (20).
    They exist because the tests need several isolated servers in one process. The bounds,
    budgets and caps SERVERFIX108 added are variables of the same kind and are listed in
    that section below; `ACWW_TRUST_PROXY` survives only as a legacy name that on its own
    now trusts nothing -- `ACWW_TRUSTED_PROXIES` is the one that reads a forwarded header.

## Transport and auth

HTTP/1.1 + JSON on one port (default 8080; TLS is the NAS reverse proxy's job). Bearer
token from login (JWT, HS256, secret from `ACWW_SERVER_SECRET`, 30-day expiry). Registration
open when `ACWW_ALLOW_REGISTER=1`. Passwords hashed with argon2id (fallback bcrypt). Rate
limit 10 auth attempts / minute / IP. All binary bodies are `application/octet-stream`.
**Both websockets authenticate with `Authorization: Bearer` like every other endpoint**; the
`?token=` query form is read only as a fallback and is deprecated (SERVERFIX108).

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
| `WS /v1/lobby/ws` (bearer header; `?token=` deprecated) | client sends `{"t":"wait","mode":"host"\|"guest","town_name":...,"request_id":...}` / `{"t":"leave"}` / `{"t":"cancel","request_id":...}` / `{"t":"cancel","request_id":...,"to":id,"invite_id":n}` / `{"t":"cancel","request_id":...,"from":id,"invite_id":n}` / `{"t":"invite","to":id,"request_id":...}` / `{"t":"accept","from":id,"invite_id":n,"request_id":...}` / `{"t":"decline","from":id,"invite_id":n,"request_id":...}` / `{"t":"ping"}` | server pushes `{"t":"list","users":[...],"capabilities":["cancel","invite_id","command_request_id"]}` on connect and on every change, `{"t":"invite","from":{...},"invite_id":n}`, `{"t":"invited","to":id,"invite_id":n,"request_id":...}` to the inviter, `{"t":"cancelled","from":id,"invite_id":n}`, `{"t":"decline","from":id,"invite_id":n}`, `{"t":"matched","room":id,"peer":{...},"role":"parent"\|"child","invite_id":n}` to both, `{"t":"cancelled",...}` acknowledgment to the caller, `{"t":"pong"}`, `{"t":"error","msg":...,"request_id":...}` when a valid command id was supplied |
| `WS /v1/relay/{room}` (bearer header; `?token=` deprecated) | only the two matched users | binary frames forwarded verbatim to the other peer; `{"t":"ping"}` -> `{"t":"pong"}`; the room closes when either side leaves and the survivor gets `{"t":"peer_left"}` |
| `GET /v1/health` | -- | `{ok: true, version, users, waiting, rooms}` |

## Cancellable waiting and invitation correlation (GATE-SERVER-CANCEL-1)

The initial and changed `list` pushes carry additive `capabilities:["cancel","invite_id",
"command_request_id"]`.  A client may use `command_request_id` to correlate an error with the
specific command ticket that caused it; legacy clients ignore the additive capability and field.
A new client stays in its unknown/legacy mode until it sees `cancel` in that field.  Each
`invite` is assigned a server monotonic `invite_id`, returned in the invite push, the initiating
`invited` acknowledgment, and both `matched` messages.  This is a correlation value, not a
security nonce.  New clients echo it on `accept`, `decline`, and targeted `cancel`.  Omitting it
keeps the existing `from`-only and `to`-only clients compatible; when present, it must match the
current invitation exactly.  Sending a second invite to the same pair replaces the first id, so
a delayed response for the old invitation cannot consume or cancel the new one.

An initiating client may put `request_id` on `invite`; it follows the same non-empty string,
64-character bound as cancellation. On success it is echoed in `{"t":"invited","to":id,
"invite_id":n,"request_id":...}`; post-parse failures echo it in `error`. This additive acknowledgment lets a new client bind an
outgoing invite id to the operation that created it.  Omitting `request_id` still produces the
`invited` acknowledgment without that field for compatibility.

The additive `abort_room` capability permits `{"t":"abort_room","room":n,"request_id":"..."}`
for cancelling gate preparation after matching. Both fields are required: a positive integer
room and the usual bounded request id. Under the lobby lock, the current connection generation
must own a membership in that exact room. Success revokes the room and both relay generations,
queues `{"t":"room_aborted","room":n,"by":user_id}` to both current lobby sessions, then
queues `{"t":"room_abort_ack","room":n,"request_id":"..."}` to the requester. Connected
relays receive `peer_left` and close with1000; absent relays can no longer join. A stale room,
outsider, or replaced connection cannot revoke a newer visit. Failure uses correlated `error`.
The acknowledgment proves server revocation, not completion of the client's relay cleanup;
clients must wait for their own transport to close before resuming local dialogue.

`cancel` accepts an optional `request_id`, which must be a non-empty string of at most 64
characters and is echoed only to the caller.  A bare `{"t":"cancel","request_id":"close-1"}`
atomically removes the caller from the waiting list and clears every pending invitation where
the caller is either end.  The server replies with
`{"t":"cancelled","request_id":"close-1","status":"cancelled"|"matched","waiting":bool,"removed":bool}`.
`removed` is false for a retry after the state is already clear.  If a match won the race first,
the reply is `status:"matched"` and adds `"room":id`; this is an informational result and never
closes or changes that relay room.  The operation is idempotent: a retry gets an acknowledgment,
but does not notify a peer a second time.

Targeted cancellation names exactly one direction: `to` cancels the caller's outgoing invite,
`from` cancels an incoming invite, and `to` and `from` cannot both be present.  It leaves the
caller waiting.  The acknowledgment echoes the supplied scope (`to` or `from`) and `invite_id`
so a client can bind it to its operation.  A supplied `invite_id` must still be current under
the lobby lock; a stale id is a no-op and cannot remove a fresh invite.  Each affected peer receives one
`{"t":"cancelled","from":id,"invite_id":n}` notification (or `invite_ids:[...]` when one
operation invalidates more than one invitation to that peer).  Legacy `leave` uses the same
atomic all-invites cleanup and peer notification, without requiring an acknowledgment field.

Each lobby connection has a server-side session generation.  A replacement retires the old
generation before returning; every `wait`, `cancel`, `invite`, `decline`, and `accept` mutation
checks that generation while holding the lobby lock, so a delayed command from the displaced
socket is rejected without touching the replacement's state.  Outbound lobby events use a
per-session FIFO.  On attach, the FIFO starts with the current list and then replays pending
invites (and an active match, if any); transitions append to that same FIFO under the state
lock.  A retired generation's queued events are dropped, so a replacement cannot see an old
cancel/decline/match after its replay or miss an invitation committed during socket hand-off.

Cancellation and `accept` linearize on the same lock.  If cancellation acquires it first,
`accept` receives the existing `that invite is no longer valid` error and no room is created.  If
`accept` acquires it first, `cancel` reports `status:"matched"` and the active room remains
untouched.  Malformed `request_id` or `invite_id` values are rejected before any state change.

`request_id` is optional on `wait`, `invite`, `accept`, `decline`, and `cancel`.  For each
recognized command, a valid non-empty string of at most 64 characters is echoed in every
post-parse `error` generated for that command, including malformed command fields or a missing
invitation.  The echo is omitted when the field is absent.  An invalid, empty, non-string, or
overlong request id is rejected without echoing it, so an unvalidated value can never be used
to complete another pending ticket. For example, an invalid-mode or invalid-town `wait`
carrying the valid `request_id:"A"`
returns `{"t":"error","msg":"...","request_id":"A"}`, followed by an independent
`cancel` acknowledgment carrying only its own `request_id:"B"`.

## The relay frame contract

`[u8 kind][u8 port][u16 len][payload]`: kind 1 = MP data, kind 2 = beacon/scan reply,
kind 3 = control. **The server never parses a payload**, and the test suite proves it by
forwarding a frame whose declared `len` disagrees with the bytes that follow. `parent` is
the host ("inviting"), `child` the guest ("visiting").

## Storage

`/data/acww.sqlite` (`users`, `save_versions`; the lobby and the rooms are in memory) and
`/data/saves/<user_id>/<version>.sav`. Backups are the NAS's. No ROM data ever touches the
server.

## Confirmed against the real client (INTEGRATE83, 2026-09-11)

`port/tools/test_online_integration.py` runs this image on a fresh volume and drives
`dist/acww.exe` against it -- registration through the game's own window, login, a save round
trip with `If-Match`, a forced 412, an offline launch, two game processes meeting in the
lobby, and the whole of it again behind an nginx TLS proxy. Seven steps, all passing.

**Every contested choice on this page stood; the client changed on all five.** The list is in
`docs/kb/hybrid/online-spec.md`, "The two halves met". In short:

* `{"t":"list","users":[...]}` -- the key is `users`, and the spec now says so.
* `user_id` is an integer here and the type checks on `invite.to` / `accept.from` /
  `decline.from` stay. The client was sending quoted ids; **the specific error message is what
  made that findable in a minute rather than a day, so keep it specific.**
* `PUT /v1/save` -> 400 for an image the ROM would refuse is right even though it fires on
  every fresh account's first launch. The client now runs the same test before the PUT.
* `{"t":"peer_left"}` then a 1000 close to the survivor: the client now empties its relay ring
  and returns to the waiting list on it.
* Relay frames are forwarded or DROPPED, never buffered. A client whose instrument sent a
  single probe on connect lost the race with the peer's join; the instrument was changed.

**For anyone writing a fixture against this service:** the structured stdout events are the
contract that made the above debuggable -- `auth.login`, `save.put` (with `if_match`),
`save.stale`, `save.rejected` (with `reason`), `lobby.invite`, `lobby.matched` (with `parent`
and `child`) and `relay.close` (with `frames_forwarded`). They go to **stdout**; uvicorn's
access lines go to **stderr**, and `docker logs` returns the two as separate blocks rather
than interleaved.

## SERVERFIX108 -- the security review's seven findings (2026-09-12)

`scratchpad/handoff/server-audit-1` reviewed this service and found seven. All seven are fixed
and each has its own regression test file under `server/tests/`; `docs/kb/hybrid/online-spec.md`
carries the same list as contract. Nothing in the endpoint table above changed except the two
websocket rows and the three new refusal codes named below.

| # | what was wrong | what it is now | test file |
|---|---|---|---|
| F1 | `ACWW_TRUST_PROXY=1` read `X-Forwarded-For`'s FIRST element, which the documented appending nginx line leaves attacker-chosen -- a forged prefix bought a fresh unauthenticated login budget, and repeating somebody else's spent theirs. The limiter's key map also grew one bucket per invented prefix, for ever | the transport peer must itself be in `ACWW_TRUSTED_PROXIES` (**empty by default**, so the header does nothing out of the box; literal addresses or CIDR, and an unparseable entry matches nothing), and then the chain is read from the RIGHT -- the last hop that is not itself a trusted proxy. uvicorn's own `ProxyHeadersMiddleware` is turned OFF (`--no-proxy-headers`) so the decision is made in one place. The limiter map is bounded (`ACWW_LIMITER_MAX_KEYS`, 4,096) and evicts least-recently-used | `test_proxy_trust.py` |
| F2 | both websockets took their JWT in the query string, and uvicorn logs a handshake as `"WebSocket <path-with-query>" [accepted]` at INFO -- a reusable 30-day credential in stdout on every lobby and relay connect, outside this service's own field scrubbing and outside `--no-access-log` | `Authorization: Bearer` on the upgrade, read first; `?token=` still accepted for one release as the compatibility path (the client puts it back only behind `ACWW_ONLINE_WS_QUERY_TOKEN=1` / `ws_query_token=1`, and says so in its log). `app/logging_.py` also installs a filter on uvicorn's own loggers that rewrites every query string to `?<redacted>` | `test_ws_auth_logging.py` |
| F3 | sizes were checked after the allocation they would pay for: an unauthenticated login buffered and parsed a body of any size and then hashed a password of any length with argon2id; `PUT /v1/save` buffered the whole upload before asking whether it was 262,144 bytes; a websocket message had no application ceiling (uvicorn's default is 16 MiB) and the relay had no frame cap at all -- an 8,193-byte message reached the peer unchanged | `ACWW_MAX_AUTH_BODY` (4,096) refused from `Content-Length` and again while streaming; username/password bounded at 24 / 256 bytes before hashing; the save body streamed and abandoned above 262,144 (**413**); `ACWW_MAX_LOBBY_MESSAGE` (8,192) and `ACWW_RELAY_FRAME_MAX` (4,096) close **1009**; `--ws-max-size 65536` refuses at the protocol layer first | `test_bounds.py` |
| F4 | `accept` checked only that both parties were waiting -- no invitation existed in server state -- so any account could read a victim's id out of the list and force a match, taking them out of the list they were choosing from | `invite` records a DIRECTED pending invitation with `ACWW_INVITE_TTL` (60 s); `accept` consumes exactly that one inside the lock that creates the room; `decline`, `leave` and a lost socket clear every invitation the account is either end of. A bare, replayed, wrong-direction, declined or expired accept is all one refusal | `test_invites.py` |
| F5 | no message, transfer or room budget: repeated `wait` with unchanged state re-serialized and re-broadcast the whole list to every socket; invite/decline/ping/relay/save had no rate; there was no lobby-socket or active-room quota | token buckets per account AND per client address over HTTP operations and bytes, lobby messages, relay frames and bytes (**429** on HTTP, close **1013** on a socket); an unchanged list is not re-broadcast; `ACWW_MAX_LOBBY_SOCKETS` (32) and `ACWW_MAX_ROOMS` (16). Every default is far above the game's own traffic and there is a test that says so | `test_budgets.py` |
| F6 | a second relay socket for the same side of a room overwrote the map entry and left the first connection running as an authorized writer -- a stale client or a stolen token interleaved frames with its replacement | `join_room` hands the connection a GENERATION and returns the socket it displaced; the displaced one is told `{"t":"error"}` and closed **1008**, and the generation is checked under the lock before every forward. A stale disconnect cannot free the room its replacement is in | `test_relay_sessions.py` |
| F7 | an `If-Match` the parser could not read -- a tag list like `"0", "999"`, a malformed tag, an empty header -- returned the same `None` as "no header", so an unsupported precondition silently became an unconditional write and could overwrite a newer save | absent, invalid and `*` are three different answers; invalid is **428** and is decided before the body is read. `"7"`, `W/"7"`, `7` and `*` keep their exact old behaviour | `test_if_match.py` |

**Environment added** (all optional, all defaulting to the values above):
`ACWW_TRUSTED_PROXIES`, `ACWW_LIMITER_MAX_KEYS`, `ACWW_MAX_AUTH_BODY`, `ACWW_MAX_PASSWORD`,
`ACWW_MAX_LOBBY_MESSAGE`, `ACWW_WS_MAX_MESSAGE`, `ACWW_RELAY_FRAME_MAX`, `ACWW_INVITE_TTL`,
`ACWW_HTTP_OPS_BURST` / `_PER_SEC`, `ACWW_HTTP_BYTES_BURST` / `_PER_SEC`,
`ACWW_LOBBY_OPS_BURST` / `_PER_SEC`, `ACWW_RELAY_OPS_BURST` / `_PER_SEC`,
`ACWW_RELAY_BYTES_BURST` / `_PER_SEC`, `ACWW_MAX_LOBBY_SOCKETS`, `ACWW_MAX_ROOMS`.

**Still true, and still the operator's job.** One worker only: the save store's
compare-and-swap is a lock inside one process, so `--workers 1` is in the CMD and a second
replica would make two writers believe they both won. The image still lacks a read-only
rootfs, dropped capabilities, `no-new-privileges` and resource limits; the compose file still
publishes `8080` on every host interface rather than `127.0.0.1`; a weak `ACWW_SERVER_SECRET`
is still accepted at startup; and a password reset still leaves existing tokens valid, so a
suspected stolen token means rotating the signing key. Those are deployment findings from the
same review that this unit did not change.

**Confirmed against the real client** (2026-09-12): the image rebuilt and
`port/tools/test_online_integration.py`'s seven steps run end to end against it with a freshly
packed `dist/acww.exe` -- register, login, save round trip with `If-Match`, the 412, offline,
two processes matched and relaying in the lobby, and TLS through nginx. The container's whole
log contains **zero** occurrences of `token=`, and the handshake records read
`"WebSocket /v1/lobby/ws" [accepted]` with no query string at all.
