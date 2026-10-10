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


def _damaged_image(player="슈슈"):
    """A synthetic image whose player is `player` and whose villager 1 calls them by the
    0x0a-damaged nickname (NICKFIX172)."""
    from app.savecheck import BANK2_OFF, BANK_SIZE, CHECKSUM_OFF, compute_checksum
    img = bytearray(make_sample_image())
    bank = bytearray(img[:BANK_SIZE])
    name = "민성".encode("utf-16le")
    pn = player.encode("utf-16le")
    bank[0x14 + 0x248E:0x14 + 0x248E + len(pn)] = pn
    for v in range(8):
        bank[0x9284 + v * 0x7EC + 0x7AF] = 0xFF if v != 1 else 0x38
    rec = 0x9284 + 1 * 0x7EC
    bank[rec + 0x10:rec + 0x10 + len(name)] = name
    bank[rec + 0x1E:rec + 0x1E + len(name)] = name
    bank[rec + 0x20] = 0x0A                          # 민성 -> 민섊
    ck = compute_checksum(bytes(bank))
    bank[CHECKSUM_OFF] = ck & 0xFF
    bank[CHECKSUM_OFF + 1] = ck >> 8
    img[:BANK_SIZE] = bank
    img[BANK2_OFF:BANK2_OFF + BANK_SIZE] = bank
    return bytes(img), rec


def test_nickfix_repairs_the_image_and_is_idempotent():
    from app import nickfix
    from app.savecheck import validate_card_image
    img, rec = _damaged_image()
    fixed, found = nickfix.repair(img)
    assert [(f["villager"], f["offset"], f["was"], f["now"]) for f in found] == [(1, 0x20, 0xC10A, 0xC131)]
    validate_card_image(fixed)
    assert fixed[rec + 0x20] == 0x31
    assert nickfix.repair(fixed) == (fixed, [])
    assert nickfix.player_names(img) == ["슈슈"]


def test_admin_save_tool_nickfix(admin_server, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    auth = register(admin_server, "shushu")
    img, rec = _damaged_image()
    assert player_put(admin_server, auth, img).status_code == 200
    tool = Path(__file__).resolve().parents[1] / "tools" / "admin_save.py"
    env = dict(os.environ, ACWW_ADMIN_TOKEN=TOKEN, PYTHONIOENCODING="utf-8")
    run = lambda *a: subprocess.run([sys.executable, str(tool), "--url", admin_server.base, *a],
                                    env=env, capture_output=True, text=True, encoding="utf-8",
                                    timeout=TIMEOUT)
    dry = run("nickfix", "--player", "슈슈", "--dry-run", "--backup", str(tmp_path / "b"))
    assert dry.returncode == 0, dry.stderr
    assert "1 damaged" in dry.stdout
    assert httpx.get(admin_server.base + "/v1/save", headers=auth, timeout=TIMEOUT).content == img
    r = run("nickfix", "--player", "슈슈", "--backup", str(tmp_path / "b"))
    assert r.returncode == 0, r.stderr
    got = httpx.get(admin_server.base + "/v1/save", headers=auth, timeout=TIMEOUT)
    assert got.headers["ETag"] == '"2"'
    assert got.content[rec + 0x20] == 0x31
    assert (tmp_path / "b" / "shushu-v1.sav").read_bytes() == img
    again = run("nickfix", "--player", "슈슈", "--backup", str(tmp_path / "b"))
    assert "clean" in again.stdout


def test_admin_token_is_generated_into_the_data_dir(tmp_path, monkeypatch):
    from app.config import ADMIN_TOKEN_MIN, _admin_token
    monkeypatch.delenv("ACWW_ADMIN_TOKEN", raising=False)
    first = _admin_token(tmp_path)
    assert len(first) >= ADMIN_TOKEN_MIN
    assert (tmp_path / "admin.key").read_text(encoding="ascii").strip() == first
    assert _admin_token(tmp_path) == first            # stable across restarts
    monkeypatch.setenv("ACWW_ADMIN_TOKEN", "x" * 30)
    assert _admin_token(tmp_path) == "x" * 30         # the environment wins
