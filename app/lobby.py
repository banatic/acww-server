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

    def members(self) -> tuple[int, int]:
        return (self.parent_id, self.child_id)

    def peer_of(self, user_id: int) -> int:
        return self.child_id if user_id == self.parent_id else self.parent_id


class LobbyState:
    def __init__(self) -> None:
        self._waiters: dict[int, Waiter] = {}
        self._sockets: dict[int, Any] = {}          # every connected lobby socket
        self._rooms: dict[int, Room] = {}
        self._room_ids = itertools.count(1)
        self._lock = asyncio.Lock()

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
        """Register a lobby socket. Returns the socket it displaced, if any."""
        async with self._lock:
            old = self._sockets.get(user_id)
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
            return self._waiters.pop(user_id, None) is not None

    def waiter(self, user_id: int) -> Waiter | None:
        return self._waiters.get(user_id)

    def socket_of(self, user_id: int) -> Any:
        return self._sockets.get(user_id)

    # ------------------------------------------------------------------ rooms

    async def match(self, inviter_id: int, accepter_id: int) -> Room | None:
        """Create the room for an accepted invite and take both out of the list."""
        async with self._lock:
            a = self._waiters.get(inviter_id)
            b = self._waiters.get(accepter_id)
            if a is None or b is None:
                return None
            if a.mode != b.mode:
                parent, child = (a, b) if a.mode == "host" else (b, a)
            else:
                parent, child = a, b          # the invite is the thing that happened
            self._sweep_locked()
            room = Room(next(self._room_ids), parent.user_id, child.user_id,
                        asyncio.get_running_loop().time())
            self._rooms[room.room_id] = room
            self._waiters.pop(inviter_id, None)
            self._waiters.pop(accepter_id, None)
            return room

    async def join_room(self, room_id: int, user_id: int, socket: Any) -> Room | None:
        async with self._lock:
            room = self._rooms.get(room_id)
            if room is None or user_id not in room.members():
                return None
            room.sockets[user_id] = socket
            return room

    async def leave_room(self, room_id: int, user_id: int, socket: Any) -> Any:
        """Remove a peer and FREE the room. Returns the survivor's socket, if connected."""
        async with self._lock:
            room = self._rooms.pop(room_id, None)
            if room is None:
                return None
            if room.sockets.get(user_id) is not socket:
                # A stale close for a socket that was already replaced: put it back.
                self._rooms[room_id] = room
                return None
            room.sockets.pop(user_id, None)
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
