"""One-line JSON logs on stdout.

Container Manager shows a container's stdout and nothing else, so that is where the log
goes -- no files to rotate, no second place to look.  `event(...)` takes a name and
keyword fields; the caller is responsible for never passing a secret.  Two things are
NEVER logged anywhere in this service: a password or token (only the token's `jti` and the
user id), and a byte of a save (only its length and sha256).

SERVERFIX108 / F2.  "NEVER logged anywhere in this service" was true of THIS logger and
false of the process.  uvicorn formats a websocket handshake as
`'%s - "WebSocket %s" [accepted]'` with the path AND ITS QUERY STRING, on the
`uvicorn.error` logger at INFO -- outside `_FORBIDDEN`, outside `--no-access-log`, and with
a reusable 30-day bearer token in it whenever a client authenticates in the URL.  Anyone who
later reads a log export, a backup or a shipped log stream holds that credential.
`install_query_redaction()` puts a filter on `uvicorn.access` and `uvicorn.error` that
rewrites every argument's query string to `?<redacted>` before the record is formatted, so
the path is still there to debug with and the secret is not.  It is a SECOND line of defence:
the client now authenticates in an `Authorization` header (port/platform/online.c), and the
query form survives only behind that file's compatibility flag for one release.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
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


# --------------------------------------------------------- F2: uvicorn's own records

REDACTED_QUERY = "?<redacted>"

# The loggers uvicorn formats a request path on. `uvicorn.access` is the access line and is
# off in the fixtures; `uvicorn.error` is where the websocket handshake record lives, which
# is why `--no-access-log` alone did not close this (measured by server-audit-1).
_UVICORN_LOGGERS = ("uvicorn.access", "uvicorn.error", "uvicorn.asgi", "uvicorn")


def _scrub(value: object) -> object:
    if isinstance(value, str) and "?" in value:
        return value.split("?", 1)[0] + REDACTED_QUERY
    return value


class QueryRedactingFilter(logging.Filter):
    """Rewrite `path?query` to `path?<redacted>` in a record's message and arguments.

    A filter rather than a formatter: uvicorn's own formatters are chosen by its config (and
    by whatever the operator passes on the command line), the arguments are strings at this
    point either way, and a filter attached to the logger applies to every handler including
    ones added later.  It NEVER drops a record -- an access line that vanished would be worse
    than one whose query is a placeholder.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_scrub(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _scrub(v) for k, v in record.args.items()}
        if isinstance(record.msg, str) and record.args in ((), None) and "?" in record.msg:
            record.msg = _scrub(record.msg)
        return True


_installed = False


def install_query_redaction() -> None:
    """Idempotent, and safe to call before uvicorn has configured its loggers: a filter on a
    logger object survives `dictConfig`'s handler replacement, which is what uvicorn does to
    its own loggers at startup."""
    global _installed
    if _installed:
        return
    _installed = True
    for name in _UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, QueryRedactingFilter) for f in logger.filters):
            logger.addFilter(QueryRedactingFilter())
