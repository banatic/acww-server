"""The waiting room and the relay rooms -- both in memory, both per process.

THE LOBBY is the window where two players see each other before a gate opens.  A client
connects its lobby socket, says `wait`, and everyone holding a socket gets the new list.
An `invite` reaches one person; their `accept` creates a ROOM and both sides are told
`matched` with their role and the room id.  Nothing here is persisted: a lobby that
survived a restart would be a list of people who are no longer there.

THE ROLE.  `parent` is the host, `child` the guest -- the spec's relay section fixes that,
because WM's parent is the one that beacons and assigns AIDs.  When the two players
declared different modes we honour the declaration (host -> parent); when they declared
the same one -- both said "host", which the client should prevent but the server must not
trust -- the INVITER is the parent, because the invite is the thing that actually happened.

THE RELAY forwards binary frames verbatim between exactly two users and never parses a
payload.  The frame contract `[u8 kind][u8 port][u16 len][payload]` belongs to the WM
bridge on both ends; the server's ignorance of it is deliberate, so that a change to the
bridge is not also a server deployment.

SERVERFIX108 changed two rules here and added one bound.

THE INVITATION IS NOW A THING THE SERVER HOLDS (F4).  It used to be a message the server
forwarded and then forgot, so `accept` could only check that both parties were waiting --
which means anyone with an account could read a victim's id out of the list and match with
them without being invited, taking them out of the list they were choosing a partner from.
`invite` now records a DIRECTED pending invitation with an expiry, `match` consumes exactly
that one inside the same lock it creates the room under, and `decline`, `leave` and a lost
socket clear every invitation the user is either end of.  An accept with nothing to consume
is refused.  The invitation is one-shot in both directions on purpose: replaying an accept
after a decline is the same attack with an extra step.

A RELAY SOCKET CARRIES A GENERATION (F6).  `join_room` used to overwrite the socket map
entry and leave the previous connection running: its receive loop never asked whether it
was still the registered writer, so a stale or stolen session kept forwarding as that
account alongside its replacement.  It now returns the generation it was registered with
and returns the socket it displaced, and the forward path checks the generation before
every frame -- so the room has exactly one writer per side at any instant, and the loser
finds out rather than being silently ignored.

CAPS (F5).  `max_lobby_sockets` and `max_rooms` are what this one process will hold at
once, and `_sweep_locked` still frees a room nobody ever connected to after
ROOM_IDLE_TIMEOUT.  A room whose two peers ARE connected is deliberately not on a timer: a
visit takes as long as the visit takes.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import itertools
from dataclasses import dataclass, field
from typing import Any

# A room nobody ever connected to is a leak; a match that went nowhere (the game crashed
# between `matched` and the relay connect) must not hold a slot forever.
ROOM_IDLE_TIMEOUT = 300.0

# How long a directed invitation stays pending when the settings do not say (F4).
INVITE_TTL = 60.0


class Refused(Exception):
    """A cap or a missing invitation. `reason` is what the socket is told."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class Waiter:
    user_id: int
    username: str
    town_name: str
    mode: str                  # "host" | "guest"
    since_utc: str
    socket: Any = None

    def public(self) -> dict:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "town_name": self.town_name,
            "mode": self.mode,
            "since_utc": self.since_utc,
        }


@dataclass
class Room:
    room_id: int
    parent_id: int
    child_id: int
    created_mono: float
    sockets: dict[int, Any] = field(default_factory=dict)
    # F6. The generation the CURRENT socket of each side was registered with. A receive loop
    # holding an older one is a superseded connection and may not forward.
    gens: dict[int, int] = field(default_factory=dict)

    def members(self) -> tuple[int, int]:
        return (self.parent_id, self.child_id)

    def peer_of(self, user_id: int) -> int:
        return self.child_id if user_id == self.parent_id else self.parent_id

    def current(self, user_id: int, generation: int) -> bool:
        return self.gens.get(user_id) == generation


class LobbyState:
    def __init__(self, max_lobby_sockets: int = 32, max_rooms: int = 16,
                 invite_ttl: float = INVITE_TTL) -> None:
        self._waiters: dict[int, Waiter] = {}
        self._sockets: dict[int, Any] = {}          # every connected lobby socket
        self._rooms: dict[int, Room] = {}
        self._room_ids = itertools.count(1)
        self._gens = itertools.count(1)
        # F4. (inviter_id, target_id) -> the monotonic deadline it stops being valid at.
        self._invites: dict[tuple[int, int], float] = {}
        self._lock = asyncio.Lock()
        self.max_lobby_sockets = int(max_lobby_sockets)
        self.max_rooms = int(max_rooms)
        self.invite_ttl = float(invite_ttl)

    # ------------------------------------------------------------- accounting

    def waiting_list(self) -> list[dict]:
        return [w.public() for w in sorted(self._waiters.values(), key=lambda w: w.since_utc)]

    def counts(self) -> tuple[int, int]:
        return len(self._waiters), len(self._rooms)

    def connected_ids(self) -> list[int]:
        """Everyone holding a lobby socket right now -- the broadcast's audience."""
        return list(self._sockets.keys())

    def room(self, room_id: int) -> Room | None:
        return self._rooms.get(room_id)

    # ------------------------------------------------------------ lobby socket

    async def attach(self, user_id: int, socket: Any) -> Any:
        """Register a lobby socket. Returns the socket it displaced, if any.

        Raises `Refused` when the process is already holding `max_lobby_sockets` (F5).  An
        account that is REPLACING its own socket is never refused by the cap: it costs no new
        slot, and refusing it would make a reconnect after a dropped connection impossible
        exactly when the lobby is busy.
        """
        async with self._lock:
            old = self._sockets.get(user_id)
            if old is None and len(self._sockets) >= self.max_lobby_sockets:
                raise Refused("this server is holding as many lobby connections as it will "
                              "(%d); try again in a moment" % self.max_lobby_sockets)
            self._sockets[user_id] = socket
            w = self._waiters.get(user_id)
            if w is not None:
                w.socket = socket
            return old

    async def detach(self, user_id: int, socket: Any) -> bool:
        """Drop a socket and any wait it held. True when the list changed."""
        async with self._lock:
            if self._sockets.get(user_id) is socket:
                self._sockets.pop(user_id, None)
                # F4. A socket that is gone cannot consent to anything: every invitation this
                # account sent or received dies with it.
                self._forget_invites_locked(user_id)
            w = self._waiters.get(user_id)
            if w is not None and w.socket is socket:
                del self._waiters[user_id]
                return True
            return False

    async def wait(self, user_id: int, username: str, town_name: str, mode: str,
                   socket: Any) -> None:
        async with self._lock:
            existing = self._waiters.get(user_id)
            since = existing.since_utc if existing else _utc()
            self._waiters[user_id] = Waiter(user_id, username, town_name, mode, since, socket)

    async def leave(self, user_id: int) -> bool:
        async with self._lock:
            self._forget_invites_locked(user_id)
            return self._waiters.pop(user_id, None) is not None

    def waiter(self, user_id: int) -> Waiter | None:
        return self._waiters.get(user_id)

    def socket_of(self, user_id: int) -> Any:
        return self._sockets.get(user_id)

    # ---------------------------------------------------------------- invites (F4)

    def _now(self) -> float:
        try:
            return asyncio.get_running_loop().time()
        except RuntimeError:      # pragma: no cover - only reachable outside a loop
            return 0.0

    def _forget_invites_locked(self, user_id: int) -> None:
        for key in [k for k in self._invites if user_id in k]:
            del self._invites[key]

    def _expire_invites_locked(self, now: float) -> None:
        for key in [k for k, deadline in self._invites.items() if deadline <= now]:
            del self._invites[key]

    async def offer_invite(self, inviter_id: int, target_id: int) -> None:
        """Record the directed invitation `invite` just sent."""
        async with self._lock:
            now = self._now()
            self._expire_invites_locked(now)
            self._invites[(inviter_id, target_id)] = now + self.invite_ttl

    async def withdraw_invite(self, inviter_id: int, target_id: int) -> bool:
        """A decline. True when there was something to withdraw."""
        async with self._lock:
            return self._invites.pop((inviter_id, target_id), None) is not None

    async def pending_invites(self) -> list[tuple[int, int]]:
        async with self._lock:
            self._expire_invites_locked(self._now())
            return sorted(self._invites)

    # ------------------------------------------------------------------ rooms

    async def match(self, inviter_id: int, accepter_id: int) -> Room | None:
        """Create the room for an accepted invite and take both out of the list.

        None means "that invitation is not something this server is holding": no pending
        directed invite from `inviter_id` to `accepter_id`, or it expired, or one of the two
        is no longer waiting.  The invitation is consumed in the SAME lock acquisition that
        creates the room, so two accepts of one invitation cannot both win (F4).

        Raises `Refused` when the room cap is reached (F5).
        """
        async with self._lock:
            now = self._now()
            self._expire_invites_locked(now)
            if (inviter_id, accepter_id) not in self._invites:
                return None
            a = self._waiters.get(inviter_id)
            b = self._waiters.get(accepter_id)
            if a is None or b is None:
                return None
            self._sweep_locked()
            if len(self._rooms) >= self.max_rooms:
                raise Refused("this server is holding as many relay rooms as it will (%d); "
                              "try again when a visit ends" % self.max_rooms)
            del self._invites[(inviter_id, accepter_id)]
            self._forget_invites_locked(inviter_id)
            self._forget_invites_locked(accepter_id)
            if a.mode != b.mode:
                parent, child = (a, b) if a.mode == "host" else (b, a)
            else:
                parent, child = a, b          # the invite is the thing that happened
            room = Room(next(self._room_ids), parent.user_id, child.user_id, now)
            self._rooms[room.room_id] = room
            self._waiters.pop(inviter_id, None)
            self._waiters.pop(accepter_id, None)
            return room

    async def join_room(self, room_id: int, user_id: int,
                        socket: Any) -> tuple[Room, int, Any] | None:
        """Register a relay socket. `(room, generation, displaced_socket)` or None.

        F6.  The generation is what the caller must present to forward a frame, and a second
        connection for the same side ATOMICALLY replaces the first: the displaced socket is
        handed back for the caller to close, and its generation is already stale by the time
        this returns, so it cannot forward another frame even if it is mid-receive.
        """
        async with self._lock:
            room = self._rooms.get(room_id)
            if room is None or user_id not in room.members():
                return None
            displaced = room.sockets.get(user_id)
            generation = next(self._gens)
            room.sockets[user_id] = socket
            room.gens[user_id] = generation
            return room, generation, displaced

    async def may_forward(self, room_id: int, user_id: int, generation: int) -> Any:
        """The peer's socket when this connection is still the room's writer for its side.

        None means "not any more" -- the room is gone, or a newer socket took the side.  This
        is read under the lock for every frame: the check is a dictionary lookup, and the
        alternative (trusting a captured reference) is exactly the defect F6 named.
        """
        async with self._lock:
            room = self._rooms.get(room_id)
            if room is None or not room.current(user_id, generation):
                return None
            return room.sockets.get(room.peer_of(user_id))

    async def leave_room(self, room_id: int, user_id: int, socket: Any) -> Any:
        """Remove a peer and FREE the room. Returns the survivor's socket, if connected."""
        async with self._lock:
            room = self._rooms.pop(room_id, None)
            if room is None:
                return None
            if room.sockets.get(user_id) is not socket:
                # A stale close for a socket that was already replaced: put it back. The
                # replacement keeps the room and its own generation (F6).
                self._rooms[room_id] = room
                return None
            room.sockets.pop(user_id, None)
            room.gens.pop(user_id, None)
            survivors = list(room.sockets.values())
            return survivors[0] if survivors else None

    def _sweep_locked(self) -> None:
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:      # pragma: no cover - no loop, nothing to sweep
            return
        for rid, room in list(self._rooms.items()):
            if not room.sockets and now - room.created_mono > ROOM_IDLE_TIMEOUT:
                del self._rooms[rid]
