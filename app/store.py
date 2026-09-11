"""SQLite (stdlib only) plus the save files on disk.

`/data/acww.sqlite` holds two tables and nothing else; the lobby and the relay rooms are
in memory, because they are worth nothing after a restart -- a player who was waiting when
the container bounced is not waiting any more.

A save's BYTES are never in the database.  They are `/data/saves/<user_id>/<version>.sav`,
so a backup is a file copy the operator can understand and a corrupt row cannot eat a
town.  The row carries version, sha256, size and the timestamp; the file is the payload.
The last `save_history` versions are kept (20 by default) and older ones are deleted, row
and file together, newest-first -- the point of the history is "I overwrote my town by
accident this morning", not an archive.

One connection, one lock.  The write volume here is a handful of 256 KB uploads a day by
one household; a connection pool would be more moving parts than the problem has.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import datetime as _dt
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_utc   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS save_versions (
    user_id     INTEGER NOT NULL,
    version     INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    size        INTEGER NOT NULL,
    updated_utc TEXT NOT NULL,
    PRIMARY KEY (user_id, version),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_save_user ON save_versions(user_id, version DESC);
"""


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path, saves_dir: Path, history: int = 20) -> None:
        self.db_path = Path(db_path)
        self.saves_dir = Path(saves_dir)
        self.history = history
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.saves_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ users

    def create_user(self, username: str, password_hash: str) -> int | None:
        """The new user's id, or None when the name is taken (the 409)."""
        with self._lock:
            try:
                cur = self._db.execute(
                    "INSERT INTO users (username, password_hash, created_utc) VALUES (?,?,?)",
                    (username, password_hash, utc_now()),
                )
                self._db.commit()
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None

    def user_by_name(self, username: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()

    def user_by_id(self, user_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()

    def set_password_hash(self, user_id: int, password_hash: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id)
            )
            self._db.commit()

    def user_count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])

    def all_usernames(self) -> list[str]:
        with self._lock:
            return [r["username"] for r in
                    self._db.execute("SELECT username FROM users ORDER BY id").fetchall()]

    # ------------------------------------------------------------------ saves

    def _user_dir(self, user_id: int) -> Path:
        d = self.saves_dir / str(user_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_path(self, user_id: int, version: int) -> Path:
        return self.saves_dir / str(user_id) / ("%d.sav" % version)

    def latest_version(self, user_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM save_versions WHERE user_id = ? ORDER BY version DESC LIMIT 1",
                (user_id,),
            ).fetchone()

    def version_row(self, user_id: int, version: int) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM save_versions WHERE user_id = ? AND version = ?",
                (user_id, version),
            ).fetchone()

    def history_rows(self, user_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM save_versions WHERE user_id = ? ORDER BY version DESC",
                (user_id,),
            ).fetchall()

    def put_save(self, user_id: int, data: bytes, sha256: str,
                 expect_version: int | None) -> tuple[int, str] | None:
        """Store a new version. None means the If-Match was stale (the 412).

        The whole thing is under one lock so two PCs flushing the same town at the same
        moment cannot both believe they won: the second one's `expect_version` no longer
        matches and it is told to re-read.  The file is written to a temp name and
        os.replace'd, so a version row never points at a half-written file.
        """
        with self._lock:
            latest = self.latest_version(user_id)
            current = int(latest["version"]) if latest else 0
            if expect_version is not None and expect_version != current:
                return None
            version = current + 1
            path = self._user_dir(user_id) / ("%d.sav" % version)
            tmp = path.with_suffix(".sav.tmp")
            with open(tmp, "wb") as fp:
                fp.write(data)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, path)
            stamp = utc_now()
            self._db.execute(
                "INSERT INTO save_versions (user_id, version, sha256, size, updated_utc)"
                " VALUES (?,?,?,?,?)",
                (user_id, version, sha256, len(data), stamp),
            )
            self._db.commit()
            self._prune(user_id)
            return version, stamp

    def _prune(self, user_id: int) -> int:
        """Drop everything older than the newest `history` versions. Returns how many."""
        rows = self._db.execute(
            "SELECT version FROM save_versions WHERE user_id = ? ORDER BY version DESC",
            (user_id,),
        ).fetchall()
        doomed = [int(r["version"]) for r in rows[self.history:]]
        for v in doomed:
            try:
                self.save_path(user_id, v).unlink()
            except FileNotFoundError:
                pass
            self._db.execute(
                "DELETE FROM save_versions WHERE user_id = ? AND version = ?", (user_id, v)
            )
        if doomed:
            self._db.commit()
        return len(doomed)

    def read_save(self, user_id: int, version: int) -> bytes | None:
        path = self.save_path(user_id, version)
        try:
            with open(path, "rb") as fp:
                return fp.read()
        except FileNotFoundError:
            return None
