"""Bounded global-chat state, owned by one server event loop.

No socket I/O, lobby lock or database access occurs here. The endpoint must drain
each session independently and close it when ``closed`` is set. Session identity
is a lease: replacing chat never touches a lobby/relay connection. Socket lifetime
and token enforcement live in chat_routes.py.
"""
from __future__ import annotations

import asyncio
import copy
import re
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

MAX_TEXT = 16                 # BMP units; the original editor's source bound
MAX_EVENTS = 64
MAX_HISTORY = 128
RETENTION = 600.0
HEARTBEAT_EXPIRY = 60.0
PAGE_SIZE = 8
SNAPSHOT_EXPIRY = 300.0       # A full original-game bubble page can take over a minute.
ID = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
# NOTICE155: ephemeral system lines ("X joined", "X sold N bells"). They are never sequenced
# or replayed, and they go ONLY to sessions that announced `notice_v1` when they connected:
# an older client treats any event type it cannot decode as a protocol error and reconnects.
NOTICE_FEATURE = "notice_v1"
JOIN_QUIET = 120.0            # a reconnect within this window is a blip, not an arrival
ACTIVITY_KINDS = ("shop_sell", "shop_buy")
MAX_BELLS = 9_999_999


class ChatError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def validate_text(text: object) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ChatError("invalid_text")
    # Original text parser escapes and keyboard commands must not be wire text.
    for c in text:
        n = ord(c)
        if n < 32 or 0x7f <= n < 0xa0 or 0x100 <= n < 0x124 or n > 0xffff or \
                0xd800 <= n <= 0xdfff or n in (0xfffe, 0xffff):
            raise ChatError("invalid_text")
    return text


def display_label(value: object) -> str:
    """Public lobby labels are user metadata, not trusted game text.

    Keep one malformed town name from poisoning every presence response. Font
    coverage is a client concern, but parser escapes/non-BMP units are not.
    """
    if not isinstance(value, str):
        return "?"
    return "".join(c if 32 <= ord(c) < 0xfffe and not (
        0x7f <= ord(c) < 0xa0 or 0x100 <= ord(c) < 0x124 or
        0xd800 <= ord(c) <= 0xdfff) else "?" for c in value[:32])


@dataclass(eq=False)
class ChatSession:
    user_id: int
    username: str
    generation: int
    last_seen: float
    events: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(MAX_EVENTS))
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    close_reason: str = ""
    snapshot: tuple[str, float, list[dict]] | None = None
    notices: bool = False


class ChatHub:
    def __init__(self, *, max_sessions: int = 128, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.max_sessions = max_sessions
        self.epoch = secrets.token_hex(16)
        self.sequence = 0
        self.generation = 0
        self.sessions: dict[int, ChatSession] = {}
        self.history: deque[tuple[float, dict]] = deque(maxlen=MAX_HISTORY)
        self.dedup: OrderedDict[tuple[int, str], tuple[float, dict]] = OrderedDict()
        self.rates: OrderedDict[int, tuple[float, float]] = OrderedDict()
        self.activity_rates: OrderedDict[int, tuple[float, float]] = OrderedDict()
        self.last_left: OrderedDict[int, float] = OrderedDict()

    def close(self, session: ChatSession, reason: str) -> None:
        if not session.closed.is_set():
            session.close_reason = reason
            session.snapshot = None
            session.closed.set()
        if self.sessions.get(session.user_id) is session:
            del self.sessions[session.user_id]
            self.last_left[session.user_id] = self.clock()
            self.last_left.move_to_end(session.user_id)
            while len(self.last_left) > 4096:
                self.last_left.popitem(last=False)

    def notice(self, kind: str, session: ChatSession, *, include_self: bool = True, **extra) -> dict:
        """Broadcast one system line to every notice-capable session."""
        event = {"v": 1, "t": "notice", "kind": kind, "user_id": session.user_id,
                 "username": session.username,
                 "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), **extra}
        for target in list(self.sessions.values()):
            if target.notices and (include_self or target is not session):
                self.emit(target, event)
        return copy.deepcopy(event)

    def sweep(self) -> None:
        now = self.clock()
        for session in list(self.sessions.values()):
            if now - session.last_seen >= HEARTBEAT_EXPIRY:
                self.close(session, "heartbeat_expired")
        while self.history and now - self.history[0][0] >= RETENTION:
            self.history.popleft()
        while self.dedup and now - next(iter(self.dedup.values()))[0] >= RETENTION:
            self.dedup.popitem(last=False)

    def current(self, session: ChatSession) -> bool:
        return self.sessions.get(session.user_id) is session and not session.closed.is_set()

    def require(self, session: ChatSession) -> None:
        self.sweep()
        if not self.current(session):
            raise ChatError("stale_session")

    def emit(self, session: ChatSession, event: dict) -> bool:
        if not self.current(session):
            return False
        try:
            session.events.put_nowait(copy.deepcopy(event))
            return True
        except asyncio.QueueFull:
            self.close(session, "slow_reader")
            return False

    def attach(self, user_id: int, username: str, *, notices: bool = False) -> ChatSession:
        self.sweep()
        if user_id not in self.sessions and len(self.sessions) >= self.max_sessions:
            raise ChatError("capacity")
        old = self.sessions.get(user_id)
        # An arrival is a user with no session now and none within JOIN_QUIET: a replaced
        # socket or a reconnect after a network blip says nothing.
        left = self.last_left.get(user_id)
        arrival = old is None and (left is None or self.clock() - left >= JOIN_QUIET)
        if old is not None:
            self.close(old, "replaced")
        self.generation += 1
        session = ChatSession(user_id, username, self.generation, self.clock(), notices=notices)
        self.sessions[user_id] = session
        self.emit(session, {"v": 1, "t": "hello", "epoch": self.epoch,
                            "seq": self.sequence, "max_text_units": MAX_TEXT,
                            "heartbeat_seconds": 20,
                            "capabilities": ["chat_v1", "presence_v1", NOTICE_FEATURE]})
        if arrival:
            self.notice("join", session, include_self=False)
        return session

    def activity(self, session: ChatSession, kind: object, bells: object,
                 npc: object = None) -> dict:
        """A client-reported game event (a shop sale or purchase), broadcast as a notice.

        Only the kind and the amount come from the client; the name is the session's."""
        self.require(session)
        if kind not in ACTIVITY_KINDS:
            raise ChatError("invalid_activity")
        if type(bells) is not int or not 1 <= bells <= MAX_BELLS:
            raise ChatError("invalid_activity")
        # MERCHANTS155: optional, the merchant's actor id (0xd000..0xd0ff); clients name it.
        if npc is not None and (type(npc) is not int or not 0xd000 <= npc <= 0xd0ff):
            raise ChatError("invalid_activity")
        now = self.clock()
        tokens, previous = self.activity_rates.get(session.user_id, (3.0, now))
        tokens = min(3.0, tokens + max(0.0, now - previous) / 2.0)
        if tokens < 1.0:
            self.activity_rates[session.user_id] = (tokens, now)
            raise ChatError("rate_limited")
        self.activity_rates[session.user_id] = (tokens - 1.0, now)
        self.activity_rates.move_to_end(session.user_id)
        while len(self.activity_rates) > 4096:
            self.activity_rates.popitem(last=False)
        extra = {"bells": bells}
        if npc is not None:
            extra["npc"] = npc
        return self.notice(kind, session, **extra)

    def touch(self, session: ChatSession) -> None:
        self.require(session)
        session.last_seen = self.clock()

    def _send_budget(self, user_id: int) -> None:
        now = self.clock()
        tokens, previous = self.rates.get(user_id, (3.0, now))
        tokens = min(3.0, tokens + max(0.0, now - previous))
        self.rates[user_id] = (max(0.0, tokens - 1.0), now)
        self.rates.move_to_end(user_id)
        while len(self.rates) > 4096:
            self.rates.popitem(last=False)
        if tokens < 1.0:
            # A rejected request must not consume the fractional refill.
            self.rates[user_id] = (tokens, now)
            raise ChatError("rate_limited")

    def send(self, session: ChatSession, client_id: object, text: object) -> dict:
        self.require(session)
        if not isinstance(client_id, str) or not ID.fullmatch(client_id):
            raise ChatError("invalid_id")
        text = validate_text(text)
        key = (session.user_id, client_id)
        old = self.dedup.get(key)
        if old is not None:
            event = old[1]
            if event["text"] != text:
                raise ChatError("id_conflict")
            self.emit(session, {"v": 1, "t": "accepted", "id": client_id,
                                "epoch": self.epoch, "seq": event["seq"]})
            return copy.deepcopy(event)
        self._send_budget(session.user_id)
        self.sequence += 1
        event = {"v": 1, "t": "message", "epoch": self.epoch, "seq": self.sequence,
                 "id": client_id, "user_id": session.user_id, "username": session.username,
                 "text": text, "utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        now = self.clock()
        self.history.append((now, event))
        self.dedup[key] = (now, event)
        while len(self.dedup) > 4096:
            self.dedup.popitem(last=False)
        self.emit(session, {"v": 1, "t": "accepted", "id": client_id,
                            "epoch": self.epoch, "seq": self.sequence})
        for target in list(self.sessions.values()):
            self.emit(target, event)
        return copy.deepcopy(event)

    def resume(self, session: ChatSession, epoch: object, sequence: object) -> None:
        self.require(session)
        if type(sequence) is not int or sequence < 0:
            raise ChatError("invalid_cursor")
        oldest = self.history[0][1]["seq"] if self.history else self.sequence + 1
        if epoch != self.epoch or sequence < oldest - 1 or sequence > self.sequence:
            self.emit(session, {"v": 1, "t": "gap", "epoch": self.epoch,
                                "seq": self.sequence, "reason": "history_unavailable"})
            return
        replay = [event for _, event in self.history if event["seq"] > sequence]
        # Endpoint writers cannot drain during this synchronous mutation. Batch a
        # bounded page and tell the client the last delivered cursor explicitly.
        available = MAX_EVENTS - session.events.qsize() - 1
        if available < 1:
            self.close(session, "slow_reader")
            return
        page = replay[:available]
        for event in page:
            self.emit(session, event)
        self.emit(session, {"v": 1, "t": "resumed", "epoch": self.epoch,
                            "seq": page[-1]["seq"] if page else sequence,
                            "more": len(page) < len(replay)})

    def presence(self, session: ChatSession, towns: list[dict], *,
                 snapshot_id: str | None = None, page: int = 0) -> None:
        self.require(session)
        if type(page) is not int or page < 0:
            raise ChatError("invalid_page")
        now = self.clock()
        if snapshot_id is None:
            if page != 0:
                raise ChatError("invalid_page")
            rows = [{"kind": "user", "user_id": s.user_id, "username": s.username}
                    for s in sorted(self.sessions.values(), key=lambda s: s.user_id)]
            # Only a copied public lobby snapshot is allowed here, never private
            # room records. Bound independently from the caller's room limit.
            if len(towns) > 128:
                raise ChatError("town_snapshot_too_large")
            for town in towns:
                row = {"kind": "town", **{key: town[key] for key in
                    ("user_id", "username", "town_name", "players", "capacity", "state")
                    if key in town}}
                row["username"] = display_label(row.get("username", "?"))
                row["town_name"] = display_label(row.get("town_name", ""))
                rows.append(row)
            session.snapshot = (secrets.token_hex(12), now, copy.deepcopy(rows))
        snapshot = session.snapshot
        if snapshot is None or (snapshot_id is not None and snapshot_id != snapshot[0]) or \
                now - snapshot[1] >= SNAPSHOT_EXPIRY:
            raise ChatError("snapshot_expired")
        rows = snapshot[2]
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        if page >= pages:
            raise ChatError("invalid_page")
        self.emit(session, {"v": 1, "t": "presence_page", "snapshot": snapshot[0],
                            "page": page, "pages": pages, "total": len(rows),
                            "user_count": sum(row["kind"] == "user" for row in rows),
                            "town_count": sum(row["kind"] == "town" for row in rows),
                            "coverage": "chat_sessions", "rows": rows[page*PAGE_SIZE:(page+1)*PAGE_SIZE]})
