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
from collections import deque
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
    public_open: bool = False

    def public(self) -> dict:
        result = {
            "user_id": self.user_id,
            "username": self.username,
            "town_name": self.town_name,
            "mode": self.mode,
            "since_utc": self.since_utc,
        }
        if self.public_open:
            result["open"] = True
        return result


@dataclass(frozen=True)
class Invitation:
    """One directed, one-shot invitation and its server correlation value."""

    inviter_id: int
    target_id: int
    invite_id: int
    deadline: float


@dataclass(frozen=True)
class InviteOffer:
    invitation: Invitation
    inviter: dict
    target: dict
    target_socket: Any


@dataclass(frozen=True)
class CancelNotice:
    """The peer socket and invitation ids invalidated by one cancellation operation."""

    peer_id: int
    socket: Any
    invite_ids: tuple[int, ...]


@dataclass(frozen=True)
class CancelResult:
    """The linearized outcome of ``cancel`` or disconnect cleanup."""

    status: str                  # "cancelled" or "matched"
    waiting: bool
    changed: bool
    waiting_changed: bool
    room_id: int | None
    notices: tuple[CancelNotice, ...] = ()


@dataclass(frozen=True)
class RelayLeaveResult:
    """Sockets affected when one authenticated relay generation closes."""

    room_closed: bool
    aid: int
    notify_sockets: tuple[Any, ...] = ()
    close_sockets: tuple[Any, ...] = ()


@dataclass
class LobbySession:
    """One lobby socket generation and its ordered outbound event queue."""

    user_id: int
    socket: Any
    generation: int
    outbox: deque[dict] = field(default_factory=deque)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    retired: bool = False


@dataclass
class Room:
    room_id: int
    parent_id: int
    created_mono: float
    children: dict[int, int] = field(default_factory=dict)  # user id -> AID 1..3
    sockets: dict[int, Any] = field(default_factory=dict)
    # F6. The generation the CURRENT socket of each side was registered with. A receive loop
    # holding an older one is a superseded connection and may not forward.
    gens: dict[int, int] = field(default_factory=dict)
    invite_ids: dict[int, int] = field(default_factory=dict)
    parent_public: dict | None = None
    children_public: dict[int, dict] = field(default_factory=dict)
    # Lobby generations that authorized this match.  They distinguish a repeated command
    # on the same session from a fresh matchmaking session after an abandoned pre-relay room.
    lobby_gens: dict[int, int] = field(default_factory=dict)
    public_open: bool = False

    @property
    def child_id(self) -> int:
        """The first child, retained for the legacy two-player API and tests."""
        return next(iter(self.children))

    @property
    def invite_id(self) -> int | None:
        return self.invite_ids.get(self.child_id) if self.children else None

    @property
    def child_public(self) -> dict | None:
        return self.children_public.get(self.child_id) if self.children else None

    def members(self) -> tuple[int, ...]:
        return (self.parent_id, *self.children.keys())

    def aid_of(self, user_id: int) -> int | None:
        if user_id == self.parent_id:
            return 0
        return self.children.get(user_id)

    def member_rows(self) -> list[dict]:
        rows: list[dict] = []
        parent = dict(self.parent_public or {"user_id": self.parent_id})
        parent["aid"] = 0
        rows.append(parent)
        for child_id, aid in sorted(self.children.items(), key=lambda item: item[1]):
            child = dict(self.children_public.get(child_id) or {"user_id": child_id})
            child["aid"] = aid
            rows.append(child)
        return rows

    def add_child(self, user_id: int, public: dict, invite_id: int) -> int:
        if user_id in self.children:
            return self.children[user_id]
        used = set(self.children.values())
        aid = next((candidate for candidate in range(1, 4) if candidate not in used), 0)
        if not aid:
            raise Refused("that town already has four players")
        self.children[user_id] = aid
        self.children_public[user_id] = public
        self.invite_ids[user_id] = invite_id
        return aid

    def remove_child(self, user_id: int) -> int | None:
        aid = self.children.pop(user_id, None)
        self.children_public.pop(user_id, None)
        self.invite_ids.pop(user_id, None)
        self.sockets.pop(user_id, None)
        self.gens.pop(user_id, None)
        return aid

    def is_full(self) -> bool:
        return len(self.children) >= 3

    def peer_of(self, user_id: int) -> int:
        return self.child_id if user_id == self.parent_id else self.parent_id

    def forward_targets(self, user_id: int) -> tuple[Any, ...]:
        if user_id == self.parent_id:
            return tuple(socket for child_id, socket in self.sockets.items()
                         if child_id in self.children)
        parent = self.sockets.get(self.parent_id)
        return (parent,) if parent is not None else ()

    def current(self, user_id: int, generation: int) -> bool:
        return self.gens.get(user_id) == generation


class LobbyState:
    def __init__(self, max_lobby_sockets: int = 32, max_rooms: int = 16,
                 invite_ttl: float = INVITE_TTL) -> None:
        self._waiters: dict[int, Waiter] = {}
        self._sockets: dict[int, Any] = {}          # every connected lobby socket
        self._sessions: dict[int, LobbySession] = {}
        self._rooms: dict[int, Room] = {}
        self._room_ids = itertools.count(1)
        self._gens = itertools.count(1)
        self._invite_ids = itertools.count(1)
        # F4. (inviter_id, target_id) -> directed invitation and its expiry/id.
        self._invites: dict[tuple[int, int], Invitation] = {}
        self._lock = asyncio.Lock()
        self.max_lobby_sockets = int(max_lobby_sockets)
        self.max_rooms = int(max_rooms)
        self.invite_ttl = float(invite_ttl)

    # ------------------------------------------------------------- accounting

    def waiting_list(self) -> list[dict]:
        result: list[dict] = []
        for waiter in sorted(self._waiters.values(), key=lambda w: w.since_utc):
            row = waiter.public()
            if waiter.public_open:
                room = self._active_room_locked(waiter.user_id)
                players = len(room.members()) if room is not None else 1
                if room is not None and room.is_full():
                    continue
                row["players"] = players
                row["capacity"] = 4
                if room is not None:
                    row["room"] = room.room_id
            result.append(row)
        return result

    def counts(self) -> tuple[int, int]:
        return len(self.waiting_list()), len(self._rooms)

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
        old, _session = await self.attach_session(user_id, socket)
        return old

    async def attach_session(self, user_id: int, socket: Any,
                             bootstrap: dict | None = None) -> tuple[Any, LobbySession]:
        """Register a socket and atomically seed its ordered replay queue.

        ``bootstrap`` is normally the initial ``list`` envelope.  The current incoming and
        outgoing invitations, followed by any active match, are appended while the same lock
        still protects the new generation.  A replacement therefore cannot receive an event
        from the displaced generation or miss an invitation committed during the hand-off.
        """
        async with self._lock:
            old = self._sockets.get(user_id)
            if old is None and len(self._sockets) >= self.max_lobby_sockets:
                raise Refused("this server is holding as many lobby connections as it will "
                              "(%d); try again in a moment" % self.max_lobby_sockets)
            old_session = self._sessions.get(user_id)
            if old_session is not None:
                old_session.retired = True
                old_session.wake.set()
            session = LobbySession(user_id, socket, next(self._gens))
            self._sockets[user_id] = socket
            self._sessions[user_id] = session
            w = self._waiters.get(user_id)
            if w is not None:
                w.socket = socket
            if bootstrap is not None:
                if bootstrap.get("t") == "list":
                    bootstrap = dict(bootstrap)
                    bootstrap["users"] = self.waiting_list()
                self._queue_session_locked(session, bootstrap)
            self._queue_replay_locked(session, user_id)
            return old, session

    async def detach(self, user_id: int, socket: Any) -> bool:
        """Drop a socket and any wait it held. True when the list changed."""
        changed, _notices = await self.detach_with_notices(user_id, socket)
        return changed

    async def detach_with_notices(self, user_id: int, socket: Any) -> tuple[bool, tuple[CancelNotice, ...]]:
        """Drop a socket and atomically cancel its wait and invitations.

        ``detach`` keeps its original boolean API.  The websocket endpoint uses this richer
        form so a disconnect cannot leave the other side displaying an invitation that no
        longer has a live owner.
        """
        async with self._lock:
            session = self._sessions.get(user_id)
            if session is None or session.socket is not socket:
                return False, ()
            self._sockets.pop(user_id, None)
            self._sessions.pop(user_id, None)
            session.retired = True
            session.outbox.clear()
            session.wake.set()
            result = self._cancel_locked(user_id, remove_wait=True)
            # A matched room outlives its lobby sockets.  The game closes the lobby channel
            # before opening the binary relay, so reaping an empty pre-relay room here races
            # the normal hand-off and makes the relay answer 1008.  The room idle sweep owns
            # cleanup for a match whose game never reaches the relay.
            return result.changed, result.notices

    async def wait(self, user_id: int, username: str, town_name: str, mode: str,
                   socket: Any, generation: int | None = None,
                   public_open: bool = False) -> bool:
        async with self._lock:
            if not self._mutation_owner_locked(user_id, socket, generation):
                return False
            # A fresh private matchmaking request supersedes an earlier match that never
            # opened a relay.  Do this on the explicit new `wait`, not on lobby disconnect:
            # closing the lobby socket is the normal game-to-relay hand-off.
            old_room = self._active_room_locked(user_id)
            if (old_room is not None and not old_room.public_open and not old_room.sockets
                    and old_room.lobby_gens.get(user_id) != generation):
                self._rooms.pop(old_room.room_id, None)
            existing = self._waiters.get(user_id)
            since = existing.since_utc if existing else _utc()
            self._waiters[user_id] = Waiter(user_id, username, town_name, mode, since, socket,
                                            public_open and mode == "host")
            return True

    async def leave(self, user_id: int) -> bool:
        async with self._lock:
            return self._cancel_locked(user_id, remove_wait=True).changed

    async def cancel(self, user_id: int, *, target_id: int | None = None,
                     outgoing: bool | None = None,
                     invite_id: int | None = None,
                     socket: Any = None,
                     generation: int | None = None) -> CancelResult:
        """Atomically leave/cancel a pending lobby operation.

        With no target this removes the caller from the waiting list and clears every invite
        where the caller is either end.  ``outgoing=True`` scopes the operation to ``user_id ->
        target_id`` and ``outgoing=False`` scopes it to ``target_id -> user_id``; a scoped cancel
        deliberately leaves the caller waiting.  A supplied id must still be current under this
        lock, so a delayed cancellation cannot remove a fresh invitation.  An active room is
        only reported in the result: this method never mutates rooms or relay sockets.
        """
        async with self._lock:
            if not self._mutation_owner_locked(user_id, socket, generation):
                return self._cancel_rejected_locked(user_id)
            return self._cancel_locked(user_id, target_id=target_id, outgoing=outgoing,
                                       invite_id=invite_id, remove_wait=target_id is None)

    def waiter(self, user_id: int) -> Waiter | None:
        return self._waiters.get(user_id)

    def socket_of(self, user_id: int) -> Any:
        return self._sockets.get(user_id)

    async def is_current_socket(self, user_id: int, socket: Any) -> bool:
        """Whether ``socket`` still owns the account's current lobby generation."""
        async with self._lock:
            current = self._sessions.get(user_id)
            return current is not None and current.socket is socket

    async def next_session_event(self, session: LobbySession) -> dict | None:
        """Pop one event for the current generation, waiting without holding ``_lock``."""
        while True:
            async with self._lock:
                current = self._sessions.get(session.user_id)
                if current is not session or session.retired:
                    session.retired = True
                    session.outbox.clear()
                    return None
                if session.outbox:
                    payload = session.outbox.popleft()
                    if not session.outbox:
                        session.wake.clear()
                    return payload
                session.wake.clear()
            await session.wake.wait()

    async def broadcast_list(self, payload: dict) -> None:
        """Append one list envelope to every current session under the state lock."""
        async with self._lock:
            for session in tuple(self._sessions.values()):
                self._queue_session_locked(session, payload)

    async def send_current(self, user_id: int, socket: Any, payload: dict,
                           generation: int | None = None) -> bool:
        """Queue a reply only when the supplied socket is still current."""
        async with self._lock:
            session = self._sessions.get(user_id)
            if (session is None or session.socket is not socket or session.retired
                    or (generation is not None and session.generation != generation)):
                return False
            self._queue_session_locked(session, payload)
            return True

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
        for key in [k for k, invitation in self._invites.items()
                    if invitation.deadline <= now]:
            del self._invites[key]

    async def offer_invite(self, inviter_id: int, target_id: int) -> Invitation | None:
        """Record a directed invitation and return its server correlation id, if possible."""
        result = await self.create_invite(inviter_id, target_id)
        return result.invitation if result is not None else None

    async def create_invite(self, inviter_id: int, target_id: int,
                            socket: Any = None,
                            generation: int | None = None) -> InviteOffer | None:
        """Validate and record an invite under the same lock used by cancel and match."""
        async with self._lock:
            now = self._now()
            self._expire_invites_locked(now)
            if not self._mutation_owner_locked(inviter_id, socket, generation):
                return None
            inviter = self._waiters.get(inviter_id)
            target = self._waiters.get(target_id)
            if inviter is None or target is None:
                return None
            # A public host has already authorized every visitor by opening the gate.  Its
            # pending requests may wait through another player's multi-minute arrival; keep
            # those requests until accept, close, or disconnect.  Private directed invites
            # retain the short consent expiry from F4.
            deadline = float("inf") if target.public_open else now + self.invite_ttl
            invitation = Invitation(inviter_id, target_id, next(self._invite_ids), deadline)
            self._invites[(inviter_id, target_id)] = invitation
            inviter_public, target_public = inviter.public(), target.public()
            self._queue_session_locked(
                self._sessions.get(target_id),
                {"t": "invite", "from": inviter_public,
                 "invite_id": invitation.invite_id},
            )
            return InviteOffer(invitation, inviter_public, target_public, target.socket)

    async def withdraw_invite(self, inviter_id: int, target_id: int,
                              invite_id: int | None = None) -> bool:
        """A decline. A supplied id must still name the current invitation."""
        invitation = await self.withdraw_invite_info(inviter_id, target_id, invite_id)
        return invitation is not None

    async def withdraw_invite_info(self, inviter_id: int, target_id: int,
                                   invite_id: int | None = None,
                                   socket: Any = None,
                                   generation: int | None = None) -> Invitation | None:
        """Consume a decline and return the removed invitation for peer correlation."""
        async with self._lock:
            now = self._now()
            self._expire_invites_locked(now)
            if not self._mutation_owner_locked(target_id, socket, generation):
                return None
            invitation = self._invites.get((inviter_id, target_id))
            if invitation is None or (invite_id is not None
                                      and invitation.invite_id != invite_id):
                return None
            del self._invites[(inviter_id, target_id)]
            self._queue_session_locked(
                self._sessions.get(inviter_id),
                {"t": "decline", "from": target_id,
                 "invite_id": invitation.invite_id},
            )
            return invitation

    async def pending_invites(self) -> list[tuple[int, int]]:
        async with self._lock:
            self._expire_invites_locked(self._now())
            return sorted(self._invites)

    # ------------------------------------------------------------------ rooms

    async def match(self, inviter_id: int, accepter_id: int,
                    invite_id: int | None = None,
                    socket: Any = None,
                    generation: int | None = None) -> Room | None:
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
            if not self._mutation_owner_locked(accepter_id, socket, generation):
                return None
            invitation = self._invites.get((inviter_id, accepter_id))
            if invitation is None or (invite_id is not None
                                      and invitation.invite_id != invite_id):
                return None
            a = self._waiters.get(inviter_id)
            b = self._waiters.get(accepter_id)
            if a is None or b is None:
                return None
            if a.mode != b.mode:
                parent, child = (a, b) if a.mode == "host" else (b, a)
            else:
                parent, child = a, b          # the invite is the thing that happened
            self._sweep_locked()
            child_room = self._active_room_locked(child.user_id)
            room = self._active_room_locked(parent.user_id)
            if child_room is not None:
                return None
            if room is not None and (room.parent_id != parent.user_id or not room.public_open):
                raise Refused("that player is already in another room")
            if room is None and len(self._rooms) >= self.max_rooms:
                raise Refused("this server is holding as many relay rooms as it will (%d); "
                              "try again when a visit ends" % self.max_rooms)

            del self._invites[(inviter_id, accepter_id)]
            # An open host may have two visitors waiting while the first admission finishes.
            # Consuming one invitation must not erase the other visitor's independent request.
            self._forget_invites_locked(child.user_id)
            if not parent.public_open:
                self._forget_invites_locked(parent.user_id)

            first_child = room is None
            if first_child:
                room = Room(next(self._room_ids), parent.user_id, now,
                            parent_public=parent.public(),
                            public_open=parent.public_open)
                self._rooms[room.room_id] = room
            prior_members = room.members()
            aid = room.add_child(child.user_id, child.public(), invitation.invite_id)
            if first_child:
                parent_session = self._sessions.get(parent.user_id)
                if parent_session is not None:
                    room.lobby_gens[parent.user_id] = parent_session.generation
            child_session = self._sessions.get(child.user_id)
            if child_session is not None:
                room.lobby_gens[child.user_id] = child_session.generation

            # A public host remains discoverable until three children occupy the town. Every
            # child leaves the waiting list as soon as its slot is committed. Legacy pair
            # rooms retain the original behavior in which both waiters disappear.
            self._waiters.pop(child.user_id, None)
            if not room.public_open:
                self._waiters.pop(parent.user_id, None)

            members = room.member_rows()
            if first_child:
                self._queue_session_locked(
                    self._sessions.get(parent.user_id),
                    {"t": "matched", "room": room.room_id,
                     "peer": room.children_public[child.user_id], "role": "parent",
                     "aid": 0, "members": members,
                     "invite_id": invitation.invite_id},
                )
            else:
                joined = {"t": "member_joined", "room": room.room_id,
                          "member": dict(room.children_public[child.user_id], aid=aid),
                          "members": members, "invite_id": invitation.invite_id}
                for member_id in prior_members:
                    self._queue_session_locked(self._sessions.get(member_id), joined)
            self._queue_session_locked(
                self._sessions.get(child.user_id),
                {"t": "matched", "room": room.room_id,
                 "peer": room.parent_public, "role": "child", "aid": aid,
                 "members": members, "invite_id": invitation.invite_id},
            )
            # A PC client deliberately keeps one visible invitation: the game admits one
            # visitor at a time.  Several guests can request the same open town while an
            # arrival is running, so their earlier pushes may have occupied that single
            # slot and then been superseded.  After each committed member, replay exactly
            # the oldest still-valid request.  The invite id is unchanged, and match()
            # still consumes it atomically, so this adds no second authorization path.
            pending = [item for item in self._invites.values()
                       if item.target_id == parent.user_id]
            if room.public_open and pending:
                next_invitation = min(pending, key=lambda item: item.invite_id)
                next_waiter = self._waiters.get(next_invitation.inviter_id)
                if next_waiter is not None:
                    self._queue_session_locked(
                        self._sessions.get(parent.user_id),
                        {"t": "invite", "from": next_waiter.public(),
                         "invite_id": next_invitation.invite_id},
                    )
            return room

    def _active_room_locked(self, user_id: int) -> Room | None:
        for room in self._rooms.values():
            if user_id in room.members():
                return room
        return None

    def _cancel_locked(self, user_id: int, *, target_id: int | None = None,
                       outgoing: bool | None = None, invite_id: int | None = None,
                       remove_wait: bool = True) -> CancelResult:
        """Perform one cancellation while ``_lock`` is held."""
        self._expire_invites_locked(self._now())
        changed_wait = False
        if remove_wait and target_id is None:
            changed_wait = self._waiters.pop(user_id, None) is not None

        if target_id is None:
            keys = [key for key in self._invites if user_id in key]
        elif outgoing is True:
            keys = [(user_id, target_id)]
        elif outgoing is False:
            keys = [(target_id, user_id)]
        else:
            keys = []

        removed: list[Invitation] = []
        for key in keys:
            invitation = self._invites.get(key)
            if invitation is None:
                continue
            # A scoped stale id is a no-op.  A bare cancel has no id and clears all current
            # invitations, which is exactly what closing the gate needs.
            if target_id is not None and invite_id is not None \
                    and invitation.invite_id != invite_id:
                continue
            removed.append(invitation)
            del self._invites[key]

        grouped: dict[int, list[int]] = {}
        sockets: dict[int, Any] = {}
        for invitation in removed:
            peer_id = (invitation.target_id if invitation.inviter_id == user_id
                       else invitation.inviter_id)
            grouped.setdefault(peer_id, []).append(invitation.invite_id)
            sockets.setdefault(peer_id, self._sockets.get(peer_id))
        notices = tuple(CancelNotice(peer_id, sockets[peer_id], tuple(ids))
                        for peer_id, ids in grouped.items())
        for notice in notices:
            payload = {"t": "cancelled", "from": user_id}
            if len(notice.invite_ids) == 1:
                payload["invite_id"] = notice.invite_ids[0]
            else:
                payload["invite_ids"] = list(notice.invite_ids)
            self._queue_session_locked(self._sessions.get(notice.peer_id), payload)
        room = self._active_room_locked(user_id)
        return CancelResult("matched" if room is not None else "cancelled",
                            user_id in self._waiters, changed_wait or bool(removed),
                            changed_wait,
                            room.room_id if room is not None else None, notices)

    def _queue_session_locked(self, session: LobbySession | None, payload: dict) -> bool:
        if session is None or session.retired or self._sessions.get(session.user_id) is not session:
            return False
        session.outbox.append(payload)
        session.wake.set()
        return True

    def _queue_replay_locked(self, session: LobbySession, user_id: int) -> None:
        """Append pending invitation and active-room replay after the bootstrap list."""
        self._expire_invites_locked(self._now())
        for invitation in self._invites.values():
            if invitation.target_id == user_id:
                inviter = self._waiters.get(invitation.inviter_id)
                if inviter is not None:
                    self._queue_session_locked(
                        session,
                        {"t": "invite", "from": inviter.public(),
                         "invite_id": invitation.invite_id},
                    )
            elif invitation.inviter_id == user_id:
                self._queue_session_locked(
                    session,
                    {"t": "invited", "to": invitation.target_id,
                     "invite_id": invitation.invite_id},
                )
        room = self._active_room_locked(user_id)
        if room is None:
            return
        if user_id == room.parent_id:
            peer, role, public, aid = room.child_id, "parent", room.child_public, 0
        else:
            peer, role, public, aid = (room.parent_id, "child", room.parent_public,
                                       room.aid_of(user_id))
        if public is None:
            public = {"user_id": peer}
        self._queue_session_locked(
            session,
            {"t": "matched", "room": room.room_id, "peer": public,
             "role": role, "aid": aid, "members": room.member_rows(),
             "invite_id": room.invite_ids.get(user_id) if role == "child"
                          else room.invite_id},
        )

    def _mutation_owner_locked(self, user_id: int, socket: Any,
                               generation: int | None = None) -> bool:
        """Check the caller generation while ``_lock`` is held.

        Direct LobbyState users may mutate an unattached fixture state.  Endpoint callers pass
        their accepted generation, so a replaced or retired object can never reclaim the
        account even after the replacement disconnects.  The socket-only form remains for
        small in-memory callers that do not create sessions.
        """
        if socket is None:
            return True
        current = self._sockets.get(user_id)
        session = self._sessions.get(user_id)
        if generation is not None:
            return (session is not None and not session.retired
                    and session.generation == generation and session.socket is socket)
        return current is None or current is socket

    def _cancel_rejected_locked(self, user_id: int) -> CancelResult:
        room = self._active_room_locked(user_id)
        return CancelResult("matched" if room is not None else "cancelled",
                            user_id in self._waiters, False, False,
                            room.room_id if room is not None else None, ())

    async def abort_room(self, room_id: int, user_id: int, socket: Any,
                         generation: int) -> tuple[Any, ...] | RelayLeaveResult | None:
        """Revoke this member's exact room; caller closes returned sockets outside the lock.

        Pending-invitation cancellation deliberately cannot do this. The gate's preparation
        Cancel needs an explicit room identity so a delayed command cannot end a later visit.
        Revocation also covers a match whose relay sockets have not connected yet.
        """
        async with self._lock:
            if not self._mutation_owner_locked(user_id, socket, generation):
                return None
            room = self._rooms.get(room_id)
            if room is None or user_id not in room.members():
                return None
            if room.public_open and user_id != room.parent_id:
                aid = room.aid_of(user_id)
                leaving = room.sockets.get(user_id)
                room.remove_child(user_id)
                survivors = tuple(room.sockets.values())
                payload = {"t": "member_left", "room": room_id, "aid": aid,
                           "user_id": user_id, "members": room.member_rows()}
                for member in room.members():
                    self._queue_session_locked(self._sessions.get(member), payload)
                return RelayLeaveResult(False, aid, survivors,
                                        (leaving,) if leaving is not None else ())
            del self._rooms[room_id]
            sockets = tuple(room.sockets.values())
            # Relay handlers retain the Room object. Invalidate its generations as well as
            # the dictionary entry, so their next forwarding check observes revocation.
            room.sockets.clear()
            room.gens.clear()
            for member in room.members():
                self._queue_session_locked(self._sessions.get(member),
                    {"t": "room_aborted", "room": room_id, "by": user_id})
            if room.public_open:
                self._waiters.pop(room.parent_id, None)
                return RelayLeaveResult(True, room.aid_of(user_id), (), sockets)
            return sockets

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
            targets = room.forward_targets(user_id)
            return targets[0] if targets else None

    async def may_forward_many(self, room_id: int, user_id: int,
                               generation: int) -> tuple[Any, ...] | None:
        """Current forwarding targets for a room writer, or None for a stale writer.

        The WM topology is parent-centred: parent broadcasts reach every connected child,
        while a child's frame reaches only the parent. The server still does not inspect the
        game's binary frame.
        """
        async with self._lock:
            room = self._rooms.get(room_id)
            if room is None or not room.current(user_id, generation):
                return None
            return room.forward_targets(user_id)

    async def leave_room(self, room_id: int, user_id: int,
                         socket: Any) -> RelayLeaveResult | None:
        """Remove one current relay generation and describe who remains.

        Legacy pair rooms retain their all-or-nothing close. In a public four-player room a
        child departure removes only that AID; the host and other children keep playing. A
        parent departure closes the room for everyone.
        """
        async with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                return None
            if room.sockets.get(user_id) is not socket:
                return None
            aid = room.aid_of(user_id)
            if aid is None:
                return None
            if room.public_open and user_id != room.parent_id:
                room.remove_child(user_id)
                survivors = tuple(room.sockets.values())
                payload = {"t": "member_left", "room": room_id, "aid": aid,
                           "user_id": user_id, "members": room.member_rows()}
                for member_id in room.members():
                    self._queue_session_locked(self._sessions.get(member_id), payload)
                return RelayLeaveResult(False, aid, survivors, ())

            del self._rooms[room_id]
            room.sockets.pop(user_id, None)
            room.gens.pop(user_id, None)
            survivors = tuple(room.sockets.values())
            room.sockets.clear()
            room.gens.clear()
            if room.public_open:
                self._waiters.pop(room.parent_id, None)
            return RelayLeaveResult(True, aid, (), survivors)

    def _sweep_locked(self) -> None:
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:      # pragma: no cover - no loop, nothing to sweep
            return
        for rid, room in list(self._rooms.items()):
            if not room.sockets and now - room.created_mono > ROOM_IDLE_TIMEOUT:
                del self._rooms[rid]
