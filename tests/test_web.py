"""WEB05: the browser build is served from `<data>/web/` on the API's own origin.

Nothing is served without a bundle; with one, the page and its module come back with the types
a browser needs (`application/wasm` for WebAssembly.compileStreaming, a JavaScript type for
`.mjs` modules), and every API route still wins over the static mount."""

from __future__ import annotations

import io
import zipfile

import httpx

TIMEOUT = 30


def test_no_bundle_no_page(server):
    assert httpx.get(server.base + "/", timeout=TIMEOUT).status_code == 404
    assert httpx.get(server.base + "/v1/health", timeout=TIMEOUT).status_code == 200


def test_bundle_is_served_beside_the_api(server_factory):
    srv = server_factory("web")
    web = server_factory.tmp_path / "web" / "web"
    (web / "build").mkdir(parents=True)
    (web / "web").mkdir()
    (web / "index.html").write_text("<!doctype html><title>ACWW</title>", encoding="utf-8")
    (web / "build" / "acww.wasm").write_bytes(b"\0asm\1\0\0\0")
    (web / "web" / "acww-web.mjs").write_text("export {};", encoding="utf-8")

    page = httpx.get(srv.base + "/", timeout=TIMEOUT)
    assert page.status_code == 200 and "ACWW" in page.text
    wasm = httpx.get(srv.base + "/build/acww.wasm", timeout=TIMEOUT)
    assert wasm.status_code == 200 and wasm.headers["content-type"].startswith("application/wasm")
    mjs = httpx.get(srv.base + "/web/acww-web.mjs", timeout=TIMEOUT)
    assert mjs.status_code == 200 and "javascript" in mjs.headers["content-type"]
    assert httpx.get(srv.base + "/v1/health", timeout=TIMEOUT).json()["ok"] is True
    assert httpx.get(srv.base + "/../secret.key", timeout=TIMEOUT).status_code == 404
    assert httpx.get(srv.base + "/v1/me", timeout=TIMEOUT).status_code == 401


ADMIN = "admin-token-for-tests-0123456789"


def zipped(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            info = zipfile.ZipInfo("member")
            info.filename = name        # as given: ZipInfo() itself would turn os.sep into "/"
            z.writestr(info, data)
    return buf.getvalue()


def publish(srv, blob, token=ADMIN):
    return httpx.put(srv.base + "/v1/admin/web", content=blob, timeout=TIMEOUT,
                     headers={"X-Admin-Token": token})


def test_operator_publishes_and_replaces_the_bundle(server_factory):
    srv = server_factory("publish", admin_token=ADMIN)
    data = server_factory.tmp_path / "publish"
    one = {"index.html": b"<title>first</title>", "config.json": b"{}",
           "build/acww.wasm": b"\0asm\1\0\0\0"}
    r = publish(srv, zipped(one))
    assert r.status_code == 200 and r.json()["files"] == 3
    assert "first" in httpx.get(srv.base + "/", timeout=TIMEOUT).text
    assert httpx.get(srv.base + "/build/acww.wasm", timeout=TIMEOUT).content == b"\0asm\1\0\0\0"

    two = {"index.html": b"<title>second</title>", "config.json": b"{}"}
    assert publish(srv, zipped(two)).status_code == 200
    assert "second" in httpx.get(srv.base + "/", timeout=TIMEOUT).text
    # The new build replaces the old one whole; the old one is kept once, for an undo.
    assert httpx.get(srv.base + "/build/acww.wasm", timeout=TIMEOUT).status_code == 404
    assert (data / "web.prev" / "build" / "acww.wasm").is_file()
    assert not (data / "web.incoming").exists()


def test_bundle_upload_refusals_leave_the_live_build_alone(server_factory):
    srv = server_factory("refuse", admin_token=ADMIN)
    good = {"index.html": b"<title>live</title>", "config.json": b"{}"}
    assert publish(srv, zipped(good)).status_code == 200
    assert publish(srv, zipped(good), token="wrong-token-wrong-token-wrong").status_code == 401
    for bad in (b"not a zip",
                zipped({"index.html": b"x"}),                               # no config.json
                zipped({**good, "../secret.key": b"x"}),
                zipped({**good, "/etc/passwd": b"x"})):
        assert publish(srv, bad).status_code == 400
    assert "live" in httpx.get(srv.base + "/", timeout=TIMEOUT).text
    assert not (server_factory.tmp_path / "secret.key").exists()


def test_bundle_upload_does_not_exist_without_an_admin_token(server):
    assert publish(server, zipped({"index.html": b"", "config.json": b""})).status_code == 404
