"""State-machine contracts; endpoint/socket tests are a separate required gate."""
import pytest
import asyncio

from app.chat import ChatError, ChatHub, MAX_EVENTS, validate_text
from app.lobby import LobbyState, Room


def drain(session):
    result = []
    while not session.events.empty():
        result.append(session.events.get_nowait())
    return result


def test_auth_identity_echo_dedup_and_conflicting_id():
    hub = ChatHub()
    a, b = hub.attach(1, "alpha"), hub.attach(2, "bravo")
    drain(a); drain(b)
    event = hub.send(a, "one", "안녕하세요")
    assert (event["user_id"], event["username"]) == (1, "alpha")
    assert [e["t"] for e in drain(a)] == ["accepted", "message"]
    assert drain(b) == [event]
    hub.send(a, "one", "안녕하세요")
    assert [e["t"] for e in drain(a)] == ["accepted"]
    assert drain(b) == []
    with pytest.raises(ChatError, match="id_conflict"):
        hub.send(a, "one", "different")
    assert hub.sequence == 1


def test_replaced_session_cannot_send_or_detach_its_successor():
    hub = ChatHub(max_sessions=1)
    old = hub.attach(1, "alpha")
    new = hub.attach(1, "alpha")
    assert old.closed.is_set() and old.close_reason == "replaced"
    hub.close(old, "late_disconnect")
    assert hub.current(new)
    with pytest.raises(ChatError, match="stale_session"):
        hub.send(old, "x", "hello")
    with pytest.raises(ChatError, match="capacity"):
        hub.attach(2, "bravo")


@pytest.mark.parametrize("text", ["", " ", "x"*17, "a\x00b", "a\x1ab", "\ud800", "😀", "\u0103"])
def test_text_rejects_original_parser_commands_and_malformed_units(text):
    with pytest.raises(ChatError, match="invalid_text"):
        validate_text(text)


def test_text_boundary_and_message_immutability():
    assert validate_text("가"*16) == "가"*16
    hub = ChatHub()
    a, b = hub.attach(1, "alpha"), hub.attach(2, "bravo")
    drain(a); drain(b)
    event = hub.send(a, "id", "safe")
    event["text"] = "mutated"
    assert drain(b)[0]["text"] == "safe"
    assert hub.history[0][1]["text"] == "safe"


def test_slow_reader_does_not_block_other_peers():
    hub = ChatHub()
    slow, a, b = [hub.attach(i, str(i)) for i in range(3)]
    drain(a); drain(b)
    for _ in range(MAX_EVENTS):
        hub.emit(slow, {"t": "filler"})
    assert slow.closed.is_set() and slow.close_reason == "slow_reader"
    event = hub.send(a, "id", "hello")
    assert drain(b) == [event]


def test_rate_limit_survives_reconnect_and_refills_fractionally():
    now = [0.0]
    hub = ChatHub(clock=lambda: now[0])
    a = hub.attach(1, "alpha")
    for i in range(3):
        hub.send(a, str(i), "hello")
    a = hub.attach(1, "alpha")
    with pytest.raises(ChatError, match="rate_limited"):
        hub.send(a, "4", "hello")
    now[0] = .5
    with pytest.raises(ChatError, match="rate_limited"):
        hub.send(a, "4", "hello")
    now[0] = 1
    hub.send(a, "4", "hello")
    assert hub.sequence == 4


def test_stable_private_presence_pages_and_expiry():
    now = [0.0]
    hub = ChatHub(clock=lambda: now[0])
    sessions = [hub.attach(i, "user"+str(i)) for i in range(10)]
    for s in sessions: drain(s)
    a = sessions[0]
    town = {"user_id": 9, "username": "user9", "town_name": "Test",
            "players": 4, "capacity": 4, "state": "full", "secret": "excluded"}
    hub.presence(a, [town])
    first = drain(a)[0]
    assert first["total"] == 11 and first["pages"] == 2
    hub.close(sessions[-1], "left")
    town["players"] = 1
    hub.presence(a, [], snapshot_id=first["snapshot"], page=1)
    second = drain(a)[0]
    assert second["total"] == 11 and second["rows"][-1]["players"] == 4
    assert "secret" not in second["rows"][-1]
    assert all(drain(s) == [] for s in sessions[1:])
    now[0] = 30
    hub.presence(a, [], snapshot_id=first["snapshot"], page=1)
    assert drain(a)[0]["total"] == 11
    # A live reader keeps its session fresh while slowly reading game bubbles;
    # the stable snapshot still has its own finite lifetime.
    now[0] = 300
    a.last_seen = 300
    with pytest.raises(ChatError, match="snapshot_expired"):
        hub.presence(a, [], snapshot_id=first["snapshot"], page=1)
    now[0] = 360
    hub.sweep()
    assert not hub.sessions


def test_replay_pages_and_restart_gap():
    now = [0.0]
    hub = ChatHub(clock=lambda: now[0])
    a = hub.attach(1, "alpha")
    for i in range(100):
        now[0] = float(i)
        hub.touch(a)
        hub.send(a, str(i), "hello")
        drain(a)
    b = hub.attach(2, "bravo"); drain(b)
    hub.resume(b, hub.epoch, 0)
    page = drain(b)
    assert len(page) == 64 and page[-1]["t"] == "resumed" and page[-1]["more"]
    assert [e["seq"] for e in page[:-1]] == list(range(1, 64))
    hub.resume(b, hub.epoch, page[-1]["seq"])
    rest = drain(b)
    assert [e["seq"] for e in rest[:-1]] == list(range(64, 101))
    assert not rest[-1]["more"]
    hub.resume(b, "old-server-epoch", 100)
    assert drain(b)[0]["t"] == "gap"


def test_public_town_snapshot_includes_full_room_and_excludes_private_members():
    lobby = LobbyState()
    full = Room(room_id=1, parent_id=10, created_mono=0, public_open=True,
                parent_public={"username": "host", "town_name": "town"})
    full.children = {11: 1, 12: 2, 13: 3}
    private = Room(room_id=2, parent_id=20, created_mono=0, public_open=False)
    lobby._rooms = {1: full, 2: private}
    assert asyncio.run(lobby.public_town_snapshot()) == [
        {"user_id": 10, "username": "host", "town_name": "town", "players": 4,
         "capacity": 4, "state": "full"}]


def test_notice_reaches_only_capable_sessions_and_never_the_sequence():
    clock = [1000.0]
    hub = ChatHub(clock=lambda: clock[0])
    old = hub.attach(1, "old")                     # an older client: no notice_v1
    new = hub.attach(2, "new", notices=True)
    drain(old); drain(new)
    hub.attach(3, "arrive", notices=True)
    assert drain(old) == []
    [join] = drain(new)
    assert (join["t"], join["kind"], join["username"]) == ("notice", "join", "arrive")
    hub.activity(new, "shop_sell", 1200)
    events = drain(new)
    assert [(e["t"], e["kind"], e["bells"]) for e in events] == [("notice", "shop_sell", 1200)]
    assert drain(old) == [] and hub.sequence == 0 and not hub.history


def test_join_notice_skips_replacements_and_quick_reconnects():
    clock = [1000.0]
    hub = ChatHub(clock=lambda: clock[0])
    watcher = hub.attach(9, "watch", notices=True)
    drain(watcher)
    a = hub.attach(1, "alpha", notices=True)
    assert [e["kind"] for e in drain(watcher)] == ["join"]
    assert [e["t"] for e in drain(a)] == ["hello"]          # no notice of one's own arrival
    hub.attach(1, "alpha", notices=True)                    # replaced socket
    assert drain(watcher) == []
    hub.close(hub.sessions[1], "disconnected")
    clock[0] += 30
    hub.attach(1, "alpha", notices=True)                    # back within JOIN_QUIET
    assert drain(watcher) == []
    hub.close(hub.sessions[1], "disconnected")
    clock[0] += 121
    watcher.last_seen = clock[0]
    hub.attach(1, "alpha", notices=True)
    assert [e["kind"] for e in drain(watcher)] == ["join"]


def test_activity_is_validated_and_rate_limited():
    clock = [1000.0]
    hub = ChatHub(clock=lambda: clock[0])
    s = hub.attach(1, "alpha", notices=True)
    drain(s)
    for kind, bells in (("shop_steal", 5), ("shop_sell", 0), ("shop_buy", -3),
                        ("shop_sell", 10_000_000), ("shop_sell", 1.5), ("shop_sell", True)):
        with pytest.raises(ChatError, match="invalid_activity"):
            hub.activity(s, kind, bells)
    for _ in range(3):
        hub.activity(s, "shop_buy", 80)
    with pytest.raises(ChatError, match="rate_limited"):
        hub.activity(s, "shop_buy", 80)
    clock[0] += 2.0
    hub.activity(s, "shop_buy", 80)
    assert len(drain(s)) == 4
