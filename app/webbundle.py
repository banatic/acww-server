"""WEB05: the browser build, delivered by the operator and served from `<data>/web/`.

WHY AN UPLOAD AND NOT THE AUTO-UPDATE. `tools/nas-auto-update.sh` deploys this directory from
the PUBLIC `banatic/acww-server` repository, and the browser build is the game: it cannot go
there. So the operator's PC builds it (`port/wasm/web/bundle.py`) and sends it here as one zip
with the admin token (`bundle.py --publish`), the way `data/updates/acww.exe` is the operator's
file and not the repository's.

THE SWAP. The zip is checked whole before anything on disk changes, unpacked into
`web.incoming/`, and then two renames make it live: the current `web/` becomes `web.prev/` (one
step of undo) and `web.incoming/` becomes `web/`. A page load between the renames sees a 404
for a moment, never half of one build and half of another.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import threading
import zipfile
from pathlib import Path, PurePosixPath

MAX_UPLOAD = 64 * 1024 * 1024      # the zip; today's bundle is ~2 MB compressed
MAX_UNPACKED = 256 * 1024 * 1024   # its contents, so a zip bomb is refused before unpacking
MAX_FILES = 512
REQUIRED = ("index.html", "config.json")

_lock = threading.Lock()


class WebBundleRejected(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _members(z: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    out = []
    total = 0
    infos = [i for i in z.infolist() if not i.is_dir()]
    if len(infos) > MAX_FILES:
        raise WebBundleRejected("at most %d files in a bundle" % MAX_FILES)
    for info in infos:
        name = info.filename
        rel = PurePosixPath(name)
        if ("\\" in name or ":" in name or rel.is_absolute() or not rel.parts
                or any(p in ("", ".", "..") for p in rel.parts)):
            raise WebBundleRejected("unsafe path in the bundle: %r" % name)
        total += info.file_size
        if total > MAX_UNPACKED:
            raise WebBundleRejected("the bundle unpacks to more than %d bytes" % MAX_UNPACKED)
        out.append((info, rel))
    names = {str(rel) for _, rel in out}
    missing = [r for r in REQUIRED if r not in names]
    if missing:
        raise WebBundleRejected("not a web bundle: %s missing" % ", ".join(missing))
    return out


def install(data_dir: Path, blob: bytes) -> dict:
    """Check, unpack and swap in `blob` (a zip). Returns what was installed."""
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise WebBundleRejected("the body is not a zip file")
    with z, _lock:
        members = _members(z)
        live, incoming, prev = data_dir / "web", data_dir / "web.incoming", data_dir / "web.prev"
        shutil.rmtree(incoming, ignore_errors=True)
        incoming.mkdir(parents=True)
        written = 0
        try:
            for info, rel in members:
                target = incoming.joinpath(*rel.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as dst:
                    # Counted again while writing: a member's declared size is the zip's claim.
                    while chunk := src.read(1 << 20):
                        written += len(chunk)
                        if written > MAX_UNPACKED:
                            raise WebBundleRejected("the bundle unpacks to more than %d bytes"
                                                    % MAX_UNPACKED)
                        dst.write(chunk)
        except (WebBundleRejected, zipfile.BadZipFile, OSError) as bad:
            shutil.rmtree(incoming, ignore_errors=True)
            if isinstance(bad, WebBundleRejected):
                raise
            raise WebBundleRejected("the bundle could not be unpacked: %s" % bad)
        shutil.rmtree(prev, ignore_errors=True)
        if live.exists():
            live.rename(prev)
        incoming.rename(live)
    return {"files": len(members), "bytes": written,
            "sha256": hashlib.sha256(blob).hexdigest()}
