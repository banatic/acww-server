"""Registration, login, the token, and the rate limit."""

from __future__ import annotations

import httpx

GOOD = {"username": "villager", "password": "correct horse battery"}


def register(server, **body):
    return httpx.post(server.base + "/v1/auth/register", json=body, timeout=30)


def login(server, **body):
    return httpx.post(server.base + "/v1/auth/login", json=body, timeout=30)


def test_health_needs_no_token(server):
    r = httpx.get(server.base + "/v1/health", timeout=30)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["users"] == 0 and body["waiting"] == 0 and body["rooms"] == 0
    assert "version" in body
    assert "commit" in body
    assert r.headers["cache-control"] == "no-store"


def test_register_then_me(server):
    r = register(server, **GOOD)
    assert r.status_code == 200, r.text
    token, user_id = r.json()["token"], r.json()["user_id"]
    assert isinstance(user_id, int)

    me = httpx.get(server.base + "/v1/me",
                   headers={"Authorization": "Bearer " + token}, timeout=30)
    assert me.status_code == 200
    assert me.json() == {"user_id": user_id, "username": GOOD["username"], "save": None}
    assert httpx.get(server.base + "/v1/health", timeout=30).json()["users"] == 1


def test_register_conflict_is_409(server):
    assert register(server, **GOOD).status_code == 200
    assert register(server, **GOOD).status_code == 409


def test_register_validation(server):
    assert register(server, username="ab", password="longenough1").status_code == 400
    assert register(server, username="bad name", password="longenough1").status_code == 400
    assert register(server, username="a" * 25, password="longenough1").status_code == 400
    assert register(server, username="okname", password="short").status_code == 400


def test_login_right_and_wrong(server):
    register(server, **GOOD)
    ok = login(server, **GOOD)
    assert ok.status_code == 200 and ok.json()["token"]
    assert login(server, username=GOOD["username"], password="wrong pass word").status_code == 401
    assert login(server, username="nobody", password="wrong pass word").status_code == 401


def test_a_bad_or_absent_token_is_401(server):
    r = register(server, **GOOD)
    token = r.json()["token"]
    assert httpx.get(server.base + "/v1/me", timeout=30).status_code == 401
    assert httpx.get(server.base + "/v1/me",
                     headers={"Authorization": "Bearer nonsense"}, timeout=30).status_code == 401
    # A token signed with a different secret must not pass: HS256 is pinned and the
    # secret is the only thing separating this account from anyone on the internet.
    import jwt
    forged = jwt.encode({"sub": "1", "exp": 4102444800}, "some other secret",
                        algorithm="HS256")
    assert httpx.get(server.base + "/v1/me",
                     headers={"Authorization": "Bearer " + forged}, timeout=30).status_code == 401
    assert httpx.get(server.base + "/v1/me",
                     headers={"Authorization": "Bearer " + token}, timeout=30).status_code == 200


def test_registration_can_be_closed(server_factory):
    s = server_factory("closed", allow_register=False)
    assert register(s, **GOOD).status_code == 403
    assert httpx.get(s.base + "/v1/health", timeout=30).json()["users"] == 0


def test_rate_limit_is_ten_a_minute(server_factory):
    s = server_factory("limited", auth_rate_limit=10, auth_rate_window=60)
    assert register(s, **GOOD).status_code == 200          # attempt 1
    codes = [login(s, username=GOOD["username"], password="wrong pass word").status_code
             for _ in range(12)]
    assert codes[:9] == [401] * 9, codes                   # attempts 2..10
    assert codes[9:] == [429, 429, 429], codes             # the window is spent
    # The limit counts attempts, not failures: a correct password is refused too.
    assert login(s, **GOOD).status_code == 429
