"""Fixtures: a real uvicorn on a real loopback port, one per test that needs isolation.

The tests do NOT drive the ASGI app through a transport shim.  Half of this contract is
WebSockets -- the lobby and the relay -- and an in-process ASGI shim tests a different
object from the one the container runs: it never opens a socket, never negotiates the
websocket handshake, and would happily pass while uvicorn's `--ws` extra is missing from
the image.  So each fixture starts uvicorn on port 0 in a thread and the tests speak to it
with `httpx` and `websockets.sync.client` exactly as the standalone client will.

Every server gets its own `tmp_path` data directory, its own SQLite file and its own rate
limiter, because the limiter's bucket is the client IP and every test is 127.0.0.1: a
shared limiter would make test order decide test results.  `auth_rate_limit` is therefore
raised for every fixture except the one that is actually testing the limit.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn                                   # noqa: E402
from app.config import Settings                  # noqa: E402
from app.main import create_app                  # noqa: E402
from app.savecheck import BANK_SIZE, BANK2_OFF, CHECKSUM_OFF, compute_checksum  # noqa: E402



class LiveServer:
    """A uvicorn instance on a loopback port, torn down at the end of the test."""

    def __init__(self, app, ws_max_size: int = 65536, access_log: bool = False,
                 log_level: str = "warning") -> None:
        # `ws_max_size` and `--ws-max-size` in the Dockerfile are the SAME number and have to
        # stay that way: F3's protocol-layer ceiling is not tested at all if the fixture runs
        # with uvicorn's 16 MiB default while the container runs with 64 KiB.
        #
        # `proxy_headers=False` is F1, and it was found by this fixture failing. uvicorn has
        # its OWN `ProxyHeadersMiddleware`, ON BY DEFAULT, which rewrites `scope["client"]`
        # from `X-Forwarded-For` whenever the peer is in `forwarded_allow_ips` (default
        # `127.0.0.1`) -- so the application's careful decision about whose header to believe
        # was being made on an address uvicorn had already replaced from the same header.
        # The service decides this itself, in `main._client_key`, from `ACWW_TRUSTED_PROXIES`;
        # the Dockerfile passes `--no-proxy-headers` for the same reason.
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level=log_level,
                                access_log=access_log, ws_max_size=ws_max_size,
                                proxy_headers=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 30.0
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.01)
        sock: socket.socket = self._server.servers[0].sockets[0]
        self.port = sock.getsockname()[1]

    @property
    def base(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    @property
    def ws_base(self) -> str:
        return "ws://127.0.0.1:%d" % self.port

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=30)


def make_server(tmp_path: Path, ws_max_size: int = 65536, access_log: bool = False,
                log_level: str = "warning", **overrides) -> LiveServer:
    kwargs = dict(
        data_dir=tmp_path,
        secret="test-secret-0123456789abcdef0123456789abcdef",  # >= 32 bytes: PyJWT warns below that
        allow_register=True,
        auth_rate_limit=1000,          # raised: see the module docstring
        auth_rate_window=60,
        save_history=20,
    )
    kwargs.update(overrides)
    (tmp_path / "saves").mkdir(parents=True, exist_ok=True)
    return LiveServer(create_app(Settings(**kwargs)), ws_max_size=ws_max_size,
                      access_log=access_log, log_level=log_level)


@pytest.fixture
def server(tmp_path):
    s = make_server(tmp_path)
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture
def server_factory(tmp_path):
    made: list[LiveServer] = []

    def _make(name: str = "s", **overrides) -> LiveServer:
        d = tmp_path / name
        d.mkdir(parents=True, exist_ok=True)
        s = make_server(d, **overrides)
        made.append(s)
        return s

    _make.tmp_path = tmp_path                       # type: ignore[attr-defined]

    try:
        yield _make
    finally:
        for s in made:
            s.stop()


def make_sample_image(turnip: int = 100) -> bytes:
    """A synthetic 256 KB card image that passes the ROM's three checks on both banks.

    No real save is checked in (the repository never commits a player's save): gamecode
    byte +0 == 0x32, flag byte +0x173fa == 2, and the checksum word at +0x173f8 chosen so
    the 16-bit word sum over the bank is zero; bank 2 mirrors bank 1 as the game does.
    """
    bank = bytearray(BANK_SIZE)
    bank[0] = 0x32
    bank[0x173FA] = 2
    bank[0x17370] = turnip & 0xFF
    ck = compute_checksum(bytes(bank))
    bank[CHECKSUM_OFF] = ck & 0xFF
    bank[CHECKSUM_OFF + 1] = (ck >> 8) & 0xFF
    image = bytearray(0x40000)
    image[:BANK_SIZE] = bank
    image[BANK2_OFF:BANK2_OFF + BANK_SIZE] = bank
    return bytes(image)


@pytest.fixture(scope="session")
def sample_save() -> bytes:
    """A synthetic, ROM-valid 256 KB card image (see make_sample_image)."""
    return make_sample_image()


def mutate_save(data: bytes, turnip: int) -> bytes:
    """A DIFFERENT but still valid image: change one byte and repair both banks.

    The turnip price at +0x17370 is a single u8 inside bank 1, so writing it and then
    recomputing the checksum and re-mirroring is exactly what `savetool.py fix` does --
    which is how the tests get twenty-odd distinct images that the ROM would still load.
    """
    bank = bytearray(data[:BANK_SIZE])
    bank[0x17370] = turnip & 0xFF
    ck = compute_checksum(bytes(bank))
    bank[CHECKSUM_OFF] = ck & 0xFF
    bank[CHECKSUM_OFF + 1] = (ck >> 8) & 0xFF
    out = bytearray(data)
    out[0:BANK_SIZE] = bank
    out[BANK2_OFF:BANK2_OFF + BANK_SIZE] = bank      # Sav::Finish's unconditional mirror
    return bytes(out)
