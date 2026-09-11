"""One-line JSON logs on stdout.

Container Manager shows a container's stdout and nothing else, so that is where the log
goes -- no files to rotate, no second place to look.  `event(...)` takes a name and
keyword fields; the caller is responsible for never passing a secret.  Two things are
NEVER logged anywhere in this service: a password or token (only the token's `jti` and the
user id), and a byte of a save (only its length and sha256).
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
import threading

_lock = threading.Lock()

# Field names that must never appear in a log line, whatever a caller passes.  This is a
# belt-and-braces check: no call site passes them, and if one ever does the value is
# replaced rather than the line dropped, so the event is still visible.
_FORBIDDEN = {"password", "token", "secret", "hash", "password_hash", "body", "bytes", "data"}


def event(name: str, level: str = "info", **fields: object) -> None:
    clean = {k: ("<redacted>" if k in _FORBIDDEN else v) for k, v in fields.items()}
    line = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds"),
        "level": level,
        "event": name,
    }
    line.update(clean)
    with _lock:
        sys.stdout.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
