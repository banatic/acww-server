"""The cloud save: the round trip, the ETag, the two 400s and the history.

The sample image is the owner's own played save (`scratchpad/live47/A-after.sav`, copied
as a synthetic image from conftest.make_sample_image).  No ROM, no extracted asset: a .sav is the cartridge's
flash contents, which is the owner's data and the only 256 KB the server ever handles.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from conftest import mutate_save

TIMEOUT = 60


@pytest.fixture
def account(server):
    r = httpx.post(server.base + "/v1/auth/register",
                   json={"username": "mayor", "password": "a long enough password"},
                   timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def put(server, auth, data, if_match=None):
    headers = dict(auth)
    headers["Content-Type"] = "application/octet-stream"
    if if_match is not None:
        headers["If-Match"] = if_match
    return httpx.put(server.base + "/v1/save", content=data, headers=headers, timeout=TIMEOUT)


def test_round_trip_is_byte_identical(server, account, sample_save):
    assert httpx.get(server.base + "/v1/save", headers=account,
                     timeout=TIMEOUT).status_code == 404

    r = put(server, account, sample_save)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1
    assert r.json()["sha256"] == hashlib.sha256(sample_save).hexdigest()
    assert r.headers["ETag"] == '"1"'

    got = httpx.get(server.base + "/v1/save", headers=account, timeout=TIMEOUT)
    assert got.status_code == 200
    assert got.content == sample_save                     # byte for byte, 262,144 of them
    assert got.headers["ETag"] == '"1"'
    assert got.headers["X-Save-Sha256"] == hashlib.sha256(sample_save).hexdigest()
    assert got.headers["content-type"] == "application/octet-stream"

    me = httpx.get(server.base + "/v1/me", headers=account, timeout=TIMEOUT).json()
    assert me["save"]["version"] == 1
    assert me["save"]["size"] == 262144
    assert me["save"]["sha256"] == hashlib.sha256(sample_save).hexdigest()


def test_if_match_guards_the_write(server, account, sample_save):
    assert put(server, account, sample_save).json()["version"] == 1
    second = mutate_save(sample_save, 90)
    assert put(server, account, second, if_match='"1"').json()["version"] == 2

    # The other PC still believes it holds version 1. Its write must not land.
    third = mutate_save(sample_save, 120)
    stale = put(server, account, third, if_match='"1"')
    assert stale.status_code == 412, stale.text
    assert "version 2" in stale.json()["detail"]
    assert httpx.get(server.base + "/v1/save", headers=account,
                     timeout=TIMEOUT).content == second

    # W/"2" is the same expectation written the other way, and it is honoured.
    assert put(server, account, third, if_match='W/"2"').json()["version"] == 3


def test_wrong_size_is_400(server, account, sample_save):
    short = put(server, account, sample_save[:-1])
    assert short.status_code == 400
    assert "262144" in short.json()["detail"]
    assert put(server, account, sample_save + b"\x00").status_code == 400
    assert put(server, account, b"").status_code == 400
    # Nothing was stored by any of those.
    assert httpx.get(server.base + "/v1/save", headers=account,
                     timeout=TIMEOUT).status_code == 404


def test_a_save_the_rom_would_refuse_is_400(server, account, sample_save):
    # 1. a broken checksum: flip one byte and do NOT repair the word sum.
    broken = bytearray(sample_save)
    broken[0x100] ^= 0xFF
    r = put(server, account, bytes(broken))
    assert r.status_code == 400, r.text
    assert "word sum" in r.json()["detail"] and "func_02050920" in r.json()["detail"]

    # 2. the right checksum but the wrong gamecode -- func_0209f180's first test. The ROM
    #    rejects this bank even though it verifies, which is why the two are reported apart.
    from app.savecheck import BANK_SIZE, BANK2_OFF, CHECKSUM_OFF, compute_checksum
    bank = bytearray(sample_save[:BANK_SIZE])
    bank[0] = 0x45                                   # not Korea's 0x32
    ck = compute_checksum(bytes(bank))
    bank[CHECKSUM_OFF] = ck & 0xFF
    bank[CHECKSUM_OFF + 1] = (ck >> 8) & 0xFF
    img = bytearray(sample_save)
    img[0:BANK_SIZE] = bank
    img[BANK2_OFF:BANK2_OFF + BANK_SIZE] = bank
    r = put(server, account, bytes(img))
    assert r.status_code == 400
    assert "gamecode" in r.json()["detail"]

    # 3. the flag at +0x173fa, the third independent test.
    bank = bytearray(sample_save[:BANK_SIZE])
    bank[0x173FA] = 0
    ck = compute_checksum(bytes(bank))
    bank[CHECKSUM_OFF] = ck & 0xFF
    bank[CHECKSUM_OFF + 1] = (ck >> 8) & 0xFF
    img = bytearray(sample_save)
    img[0:BANK_SIZE] = bank
    img[BANK2_OFF:BANK2_OFF + BANK_SIZE] = bank
    r = put(server, account, bytes(img))
    assert r.status_code == 400
    assert "+0x173fa" in r.json()["detail"]

    assert httpx.get(server.base + "/v1/save", headers=account,
                     timeout=TIMEOUT).status_code == 404


def test_the_mutated_fixture_is_itself_accepted(server, account, sample_save):
    """Guard against the refusal tests passing for the wrong reason (M1)."""
    good = mutate_save(sample_save, 77)
    assert good != sample_save
    assert put(server, account, good).status_code == 200


def test_history_keeps_the_last_twenty(server, account, sample_save):
    versions = []
    for i in range(22):
        body = mutate_save(sample_save, 10 + i)
        r = put(server, account, body)
        assert r.status_code == 200, r.text
        versions.append(r.json()["version"])
    assert versions == list(range(1, 23))

    hist = httpx.get(server.base + "/v1/save/history", headers=account, timeout=TIMEOUT)
    assert hist.status_code == 200
    rows = hist.json()
    assert [r["version"] for r in rows] == list(range(22, 2, -1))     # newest first, 20 of them
    assert len(rows) == 20
    assert all(r["size"] == 262144 for r in rows)
    assert all(r["updated_utc"] for r in rows)

    # An old version is fetchable by number; a pruned one is gone, row and file together.
    v = httpx.get(server.base + "/v1/save/5", headers=account, timeout=TIMEOUT)
    assert v.status_code == 200
    assert v.content == mutate_save(sample_save, 14)
    assert v.headers["ETag"] == '"5"'
    assert httpx.get(server.base + "/v1/save/1", headers=account,
                     timeout=TIMEOUT).status_code == 404
    assert httpx.get(server.base + "/v1/save/999", headers=account,
                     timeout=TIMEOUT).status_code == 404


def test_one_account_cannot_read_another(server, account, sample_save):
    put(server, account, sample_save)
    r = httpx.post(server.base + "/v1/auth/register",
                   json={"username": "neighbour", "password": "another long password"},
                   timeout=TIMEOUT)
    other = {"Authorization": "Bearer " + r.json()["token"]}
    assert httpx.get(server.base + "/v1/save", headers=other, timeout=TIMEOUT).status_code == 404
    assert httpx.get(server.base + "/v1/save/1", headers=other,
                     timeout=TIMEOUT).status_code == 404
