"""Real HTTP update delivery; fixtures contain invented bytes, never game inputs."""
import hashlib
import os
import struct
import asyncio

import httpx
import pytest

from app.updates import Updates, UpdateUnavailable, DownloadResponse


def executable(content=b"invented test payload"):
    pe = bytearray(256)
    pe[:2] = b"MZ"
    struct.pack_into("<I", pe, 60, 64)
    pe[64:70] = b"PE\0\0\x4c\x01"
    pe[88:90] = b"\x0b\x01"
    directory = (b"ACWWPAY2" + struct.pack("<4I", 1, 24, 80 + len(content), 80)
                 + struct.pack("<4I", 72, 80, len(content), 0)
                 + hashlib.sha256(content).digest() + b"test\0\0\0\0")
    payload = directory + content
    return bytes(pe) + payload + b"ACWWTAIL" + struct.pack("<II", 256, len(payload)) + hashlib.sha256(directory).digest()


def auth(server):
    r = httpx.post(server.base + "/v1/auth/register",
                   json={"username": "updater", "password": "test-update-password"})
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_update_auth_absent_and_live_replacement(server, tmp_path):
    url = server.base + "/v1/updates/windows"
    assert httpx.get(url).status_code == 401
    headers = auth(server)
    assert httpx.get(url, headers=headers).status_code == 404
    directory = tmp_path / "updates"
    directory.mkdir()
    target = directory / "acww.exe"
    first = executable()
    target.write_bytes(first)
    response = httpx.get(url, headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    m = response.json()
    assert m["size"] == len(first) and m["sha256"] == hashlib.sha256(first).hexdigest()
    assert httpx.get(server.base + m["path"]).status_code == 401
    r = httpx.get(server.base + m["path"], headers=headers)
    assert r.content == first and r.headers["content-length"] == str(len(first))
    # An in-place partial upload is unavailable; no restart/reconstruction of app.
    target.write_bytes(first[:300])
    assert httpx.get(url, headers=headers).status_code == 404
    second = executable(b"different version")
    upload = directory / "upload.tmp"
    upload.write_bytes(second)
    os.replace(upload, target)
    m2 = httpx.get(url, headers=headers).json()
    assert m2["sha256"] != m["sha256"]
    assert httpx.get(server.base + m["path"], headers=headers).status_code == 404
    assert httpx.get(server.base + m2["path"], headers=headers).content == second
    assert httpx.get(url + "/not-a-digest.exe", headers=headers).status_code == 404


@pytest.mark.parametrize("damage", ["pe", "footer", "directory", "blob", "truncated", "extra"])
def test_reject_damaged_packages(tmp_path, damage):
    path = tmp_path / "updates" / "acww.exe"
    path.parent.mkdir()
    data = bytearray(executable())
    if damage == "truncated":
        data = data[:-1]
    elif damage == "extra":
        data += b"x"
    else:
        offset = {"pe": 0, "footer": -48, "directory": 256 + 72, "blob": 256 + 80}[damage]
        data[offset] ^= 1
    path.write_bytes(data)
    updates = Updates(tmp_path)
    with pytest.raises(UpdateUnavailable):
        updates.manifest()
    with pytest.raises(UpdateUnavailable):
        updates.manifest()  # Invalid fingerprints are cached too.
    path.write_bytes(executable())
    assert updates.manifest()["protocol"] == 1


def test_download_descriptor_pins_atomic_release(tmp_path, monkeypatch):
    path = tmp_path / "updates" / "acww.exe"
    path.parent.mkdir()
    data = executable()
    path.write_bytes(data)
    updates = Updates(tmp_path)
    manifest = updates.manifest()
    f, _ = updates.open_download(manifest["sha256"])
    try:
        # Python's Windows CRT denies replacement of an open descriptor. Linux/NAS
        # permits it; there the old descriptor must still yield the old release.
        if os.name != "nt":
            other = path.with_suffix(".new")
            other.write_bytes(executable(b"new"))
            os.replace(other, path)
        assert f.read() == data
    finally:
        f.close()
    # A file shortened between stat/read may raise struct.error rather than the
    # explicit validity exception; it must still close and release the HTTP slot.
    opened = []

    def open_file():
        f = path.open("rb")
        opened.append(f)
        return f

    def truncated(_):
        raise struct.error("short header during upload")

    monkeypatch.setattr(updates, "_open", open_file)
    monkeypatch.setattr(updates, "_inspect", truncated)
    with pytest.raises(UpdateUnavailable):
        updates.open_download(manifest["sha256"])
    assert opened[0].closed


def test_disconnect_before_first_body_releases_download():
    from starlette.requests import ClientDisconnect
    closed = []

    async def send(_):
        raise OSError("client disconnected before response headers")

    async def receive():
        return {"type": "http.disconnect"}

    response = DownloadResponse(iter([b"invented"]), cleanup=lambda: closed.append(True))
    with pytest.raises(ClientDisconnect):
        asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    assert closed == [True]
