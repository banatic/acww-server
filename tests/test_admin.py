"""The admin save routes: off without ACWW_ADMIN_TOKEN, 401 with a wrong token, and a round
trip that writes a NEW version (the player's history keeps the old one)."""

from __future__ import annotations

import hashlib

import httpx
import pytest

from conftest import make_sample_image

TIMEOUT = 60
TOKEN = "admin-token-for-tests-0123456789"


@pytest.fixture
def admin_server(server_factory):
    return server_factory("admin", admin_token=TOKEN)


def register(server, name="mayor"):
    r = httpx.post(server.base + "/v1/auth/register",
                   json={"username": name, "password": "a long enough password"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def player_put(server, auth, data):
    headers = dict(auth)
    headers["Content-Type"] = "application/octet-stream"
    return httpx.put(server.base + "/v1/save", content=data, headers=headers, timeout=TIMEOUT)


def test_routes_do_not_exist_without_a_token(server):
    r = httpx.get(server.base + "/v1/admin/users", headers={"X-Admin-Token": TOKEN},
                  timeout=TIMEOUT)
    assert r.status_code == 404


def test_wrong_or_missing_token_is_401(admin_server):
    assert httpx.get(admin_server.base + "/v1/admin/users", timeout=TIMEOUT).status_code == 401
    r = httpx.get(admin_server.base + "/v1/admin/users",
                  headers={"X-Admin-Token": TOKEN + "x"}, timeout=TIMEOUT)
    assert r.status_code == 401


def test_read_and_repair_a_players_save(admin_server):
    auth = register(admin_server)
    first = make_sample_image(turnip=100)
    assert player_put(admin_server, auth, first).status_code == 200
    adm = {"X-Admin-Token": TOKEN}

    users = httpx.get(admin_server.base + "/v1/admin/users", headers=adm, timeout=TIMEOUT).json()
    assert [u["username"] for u in users] == ["mayor"]
    assert users[0]["save"]["version"] == 1

    got = httpx.get(admin_server.base + "/v1/admin/users/mayor/save", headers=adm,
                    timeout=TIMEOUT)
    assert got.status_code == 200 and got.content == first
    assert got.headers["ETag"] == '"1"'

    fixed = make_sample_image(turnip=101)
    h = dict(adm)
    h["Content-Type"] = "application/octet-stream"
    h["If-Match"] = '"1"'
    r = httpx.put(admin_server.base + "/v1/admin/users/mayor/save", content=fixed, headers=h,
                  timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    assert r.json()["sha256"] == hashlib.sha256(fixed).hexdigest()

    # the player sees the repair, and the old version is still in the history
    me = httpx.get(admin_server.base + "/v1/save", headers=auth, timeout=TIMEOUT)
    assert me.content == fixed
    old = httpx.get(admin_server.base + "/v1/admin/users/mayor/save?version=1", headers=adm,
                    timeout=TIMEOUT)
    assert old.content == first


def test_admin_upload_is_validated_and_guarded(admin_server):
    auth = register(admin_server)
    assert player_put(admin_server, auth, make_sample_image()).status_code == 200
    h = {"X-Admin-Token": TOKEN, "Content-Type": "application/octet-stream"}
    bad = httpx.put(admin_server.base + "/v1/admin/users/mayor/save", content=b"\0" * 100,
                    headers=h, timeout=TIMEOUT)
    assert bad.status_code == 400
    h["If-Match"] = '"7"'
    stale = httpx.put(admin_server.base + "/v1/admin/users/mayor/save",
                      content=make_sample_image(), headers=h, timeout=TIMEOUT)
    assert stale.status_code == 412
    missing = httpx.get(admin_server.base + "/v1/admin/users/nobody/save",
                        headers={"X-Admin-Token": TOKEN}, timeout=TIMEOUT)
    assert missing.status_code == 404


def test_admin_save_tool_round_trip(admin_server, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    auth = register(admin_server)
    first = make_sample_image(turnip=100)
    assert player_put(admin_server, auth, first).status_code == 200
    tool = Path(__file__).resolve().parents[1] / "tools" / "admin_save.py"
    env = dict(os.environ, ACWW_ADMIN_TOKEN=TOKEN)
    run = lambda *a: subprocess.run([sys.executable, str(tool), "--url", admin_server.base, *a],
                                    env=env, capture_output=True, text=True, timeout=TIMEOUT)
    r = run("users")
    assert r.returncode == 0 and "mayor" in r.stdout
    out = tmp_path / "got.sav"
    assert run("get", "mayor", "--out", str(out)).returncode == 0
    assert out.read_bytes() == first
    fixed = tmp_path / "fixed.sav"
    fixed.write_bytes(make_sample_image(turnip=102))
    r = run("put", "mayor", str(fixed), "--if-match", "1")
    assert r.returncode == 0, r.stderr
    assert TOKEN not in r.stdout + r.stderr
    got = httpx.get(admin_server.base + "/v1/save", headers=auth, timeout=TIMEOUT)
    assert got.content == fixed.read_bytes()
