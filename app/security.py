"""Password hashing, JWTs and the auth rate limiter.

HASHING.  argon2id through argon2-cffi, which is what the spec names.  bcrypt is the
fallback and is only reached if argon2-cffi is not importable (a NAS the operator built
the image on without a C toolchain, say); a hash carries its own algorithm in its prefix
(`$argon2id$` / `$2b$`), so a server that later gains argon2 keeps verifying the old
bcrypt hashes and silently upgrades them on the next successful login.

TOKENS.  HS256, `sub` = user id, 30-day expiry by default.  There is no refresh and no
revocation list: the client stores the token in `acww-online.ini` and logs in again when
it expires.  Rotating `ACWW_SERVER_SECRET` invalidates every token at once, which is the
operator's blunt revoke.

RATE LIMIT.  10 attempts per minute per IP over the two auth endpoints, a plain in-memory
sliding window.  It is per PROCESS, which is correct here because the deployment is one
container; it is not a defence against a botnet, it is a defence against someone guessing
the owner's password from the open internet through the NAS's reverse proxy.
"""

from __future__ import annotations

import datetime as _dt
import secrets
import threading
import time
from collections import defaultdict, deque

import jwt

try:  # pragma: no cover - the import path taken depends on the image
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

    _ARGON2 = PasswordHasher()
    HASHER = "argon2id"
except Exception:  # pragma: no cover
    _ARGON2 = None
    HASHER = "bcrypt"

try:  # pragma: no cover
    import bcrypt as _bcrypt
except Exception:  # pragma: no cover
    _bcrypt = None


class PasswordBackendMissing(RuntimeError):
    pass


def hash_password(password: str) -> str:
    if _ARGON2 is not None:
        return _ARGON2.hash(password)
    if _bcrypt is not None:
        return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("ascii")
    raise PasswordBackendMissing(
        "neither argon2-cffi nor bcrypt is installed; refusing to store a password"
    )


def verify_password(stored: str, password: str) -> bool:
    """True when the password matches. Never raises on a bad password or a broken hash."""
    if stored.startswith("$argon2"):
        if _ARGON2 is None:
            return False
        try:
            _ARGON2.verify(stored, password)
            return True
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        except Exception:
            return False
    if stored.startswith("$2"):
        if _bcrypt is None:
            return False
        try:
            return _bcrypt.checkpw(password.encode("utf-8"), stored.encode("ascii"))
        except Exception:
            return False
    return False


def needs_rehash(stored: str) -> bool:
    """A bcrypt hash on a server that now has argon2 should be upgraded at next login."""
    if _ARGON2 is None:
        return False
    if not stored.startswith("$argon2"):
        return True
    try:
        return bool(_ARGON2.check_needs_rehash(stored))
    except Exception:
        return False


# ---------------------------------------------------------------------- tokens

def make_token(secret: str, user_id: int, username: str, days: int) -> str:
    now = _dt.datetime.now(_dt.timezone.utc)
    payload = {
        "sub": str(user_id),
        "usr": username,
        "iat": int(now.timestamp()),
        "exp": int((now + _dt.timedelta(days=days)).timestamp()),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def read_token(secret: str, token: str) -> dict | None:
    """Decoded claims, or None. HS256 is pinned so an `alg: none` token cannot pass."""
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except Exception:
        return None


# ----------------------------------------------------------------- rate limit

class RateLimiter:
    def __init__(self, limit: int, window: int) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)
