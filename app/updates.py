"""Opt-in Windows updates from one operator-owned file, without a service restart.

Only a complete ACWWPAY2 executable is advertised. All data is read from an open
descriptor: a rename cannot switch the file between validation and download.
An in-place upload during a stream is additionally caught by the client's SHA-256.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import struct
import threading
from pathlib import Path
from fastapi.responses import StreamingResponse

MAX_EXE = 256 * 1024 * 1024
CHUNK = 128 * 1024


class UpdateUnavailable(Exception):
    pass


class DownloadResponse(StreamingResponse):
    """Release the descriptor/budget even when ASGI cancels before iteration."""
    def __init__(self, *args, cleanup, **kwargs):
        super().__init__(*args, **kwargs)
        self.cleanup = cleanup

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.cleanup()


def fingerprint(s):
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def validate(f) -> dict:
    before = os.fstat(f.fileno())
    size = before.st_size
    if not stat.S_ISREG(before.st_mode) or not 256 <= size <= MAX_EXE:
        raise UpdateUnavailable()
    f.seek(0)
    header = f.read(64)
    if header[:2] != b"MZ":
        raise UpdateUnavailable()
    pe = struct.unpack_from("<I", header, 60)[0]
    if pe > min(size - 26, 1024 * 1024):
        raise UpdateUnavailable()
    f.seek(pe)
    pe_header = f.read(26)
    if pe_header[:6] != b"PE\0\0\x4c\x01" or pe_header[24:26] != b"\x0b\x01":
        raise UpdateUnavailable()
    f.seek(size - 48)
    tail = f.read(48)
    if tail[:8] != b"ACWWTAIL":
        raise UpdateUnavailable()
    start, length = struct.unpack_from("<II", tail, 8)
    if start < pe + 26 or start + length + 48 != size or length < 24:
        raise UpdateUnavailable()
    f.seek(start)
    head = f.read(24)
    count, directory, total, data = struct.unpack_from("<4I", head, 8)
    if (head[:8] != b"ACWWPAY2" or not 1 <= count <= 128 or directory != 24
            or total != length or not directory + count * 48 <= data <= min(length, 1048576)):
        raise UpdateUnavailable()
    f.seek(start)
    metadata = f.read(data)
    if hashlib.sha256(metadata).digest() != tail[16:]:
        raise UpdateUnavailable()
    previous_end = data
    for i in range(count):
        row = directory + 48 * i
        name, offset, n, reserved = struct.unpack_from("<4I", metadata, row)
        if (reserved or not directory + count * 48 <= name < data
                or b"\0" not in metadata[name:] or offset < previous_end or offset + n > length):
            raise UpdateUnavailable()
        previous_end = offset + n
        f.seek(start + offset)
        digest = hashlib.sha256()
        while n:
            block = f.read(min(CHUNK, n))
            if not block:
                raise UpdateUnavailable()
            digest.update(block)
            n -= len(block)
        if digest.digest() != metadata[row + 16:row + 48]:
            raise UpdateUnavailable()
    f.seek(0)
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        block = f.read(min(CHUNK, remaining))
        if not block:
            raise UpdateUnavailable()
        digest.update(block)
        remaining -= len(block)
    if fingerprint(before) != fingerprint(os.fstat(f.fileno())):
        raise UpdateUnavailable()
    sha = digest.hexdigest()
    return {"protocol": 1, "sha256": sha, "size": size,
            "path": "/v1/updates/windows/" + sha + ".exe"}


class Updates:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "updates" / "acww.exe"
        self.lock = threading.Lock()
        self.key = None
        self.cached = None
        self.downloads = threading.BoundedSemaphore(2)

    def _open(self):
        # A symlink would make this fixed publication path an arbitrary data endpoint.
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise UpdateUnavailable()
        return self.path.open("rb")

    def _inspect(self, f):
        key = fingerprint(os.fstat(f.fileno()))
        with self.lock:
            if key != self.key:
                self.cached = None
                self.key = key
                self.cached = validate(f)
            if self.cached is None:
                raise UpdateUnavailable()
            return dict(self.cached)

    def manifest(self):
        try:
            with self._open() as f:
                return self._inspect(f)
        except (OSError, ValueError, struct.error) as exc:
            raise UpdateUnavailable() from exc

    def open_download(self, digest):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise UpdateUnavailable()
        f = None
        try:
            f = self._open()
            info = self._inspect(f)
            if info["sha256"] != digest:
                raise UpdateUnavailable()
            f.seek(0)
            return f, info
        except Exception as exc:
            if f:
                f.close()
            if isinstance(exc, (OSError, ValueError, struct.error)):
                raise UpdateUnavailable() from exc
            raise
