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

SERVERFIX108 added two things here.  The limiter's key map is BOUNDED and evicts its oldest
key (F1: invented `X-Forwarded-For` prefixes used to accumulate a bucket each, for ever),
and `Budget` is the token bucket everything else is rationed with (F5).  `is_trusted_proxy`
is the one place that decides whether a forwarded header may be believed at all -- it takes
literal addresses and CIDR networks, and an unparseable spec matches NOTHING rather than
everything, because the failure mode of the other choice is "the operator's typo turned the
limiter off".
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import secrets
import threading
import time
from collections import OrderedDict, deque

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
    """A sliding window per key, with a BOUNDED key map.

    `max_keys` is F1's other half: before the fix an unauthenticated caller could mint a new
    bucket per invented forwarded prefix and nothing ever removed one, so the map was a slow
    memory leak reachable without an account.  Empty buckets are dropped as they are seen and
    the oldest key is evicted when the map is full -- an eviction can only ever GIVE a caller
    a fresh window, never take one away from a caller who is inside the limit, so the worst
    case of the bound is the behaviour the limiter already had.
    """

    def __init__(self, limit: int, window: int, max_keys: int = 4096) -> None:
        self.limit = limit
        self.window = window
        self.max_keys = max(1, int(max_keys))
        self._hits: "OrderedDict[str, deque[float]]" = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            if q is None:
                q = deque()
                self._hits[key] = q
            self._hits.move_to_end(key)
            while q and now - q[0] > self.window:
                q.popleft()
            if not q:
                # An expired bucket is a key nobody is using: forget it rather than keep it.
                for dead in [k for k, v in list(self._hits.items())[:16] if k != key and not v]:
                    self._hits.pop(dead, None)
            while len(self._hits) > self.max_keys:
                oldest, _ = next(iter(self._hits.items()))
                if oldest == key:
                    break
                self._hits.pop(oldest, None)
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def keys(self) -> int:
        with self._lock:
            return len(self._hits)

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


# ------------------------------------------------------------------ trusted proxies

_NET_CACHE: dict[str, object] = {}


def _network_of(spec: str):
    """`10.0.0.0/8`, `192.168.1.4` or nonsense -- the last one matches nothing."""
    if spec in _NET_CACHE:
        return _NET_CACHE[spec]
    try:
        net = ipaddress.ip_network(spec, strict=False)
    except ValueError:
        net = None
    _NET_CACHE[spec] = net
    return net


def is_trusted_proxy(address: str, specs: tuple[str, ...] | list[str]) -> bool:
    """True when `address` is one of the operator's declared proxies.

    Called with the TRANSPORT peer's address, never with a header's contents, which is the
    whole of F1: a forwarded header is evidence only about hops further out than a peer we
    already trust, so the peer has to be checked first and by something the caller cannot
    choose.
    """
    if not specs or not address:
        return False
    try:
        addr = ipaddress.ip_address(address)
    except ValueError:
        return False
    for spec in specs:
        net = _network_of(spec)
        if net is not None and addr in net:
            return True
    return False


# ----------------------------------------------------------------------- budgets

class Budget:
    """A token bucket per key: `burst` tokens, refilled `per_sec` a second.

    F5's shape.  One instance rations ONE kind of thing (lobby messages, HTTP requests,
    relay bytes) and the caller spends whatever the operation costs -- 1 for an operation,
    `len(body)` for a transfer -- so a budget's units are the caller's, not this class's.
    A bucket that is full is forgotten on the next sweep, so the map is bounded by the
    number of keys ACTIVELY spending rather than by the number ever seen.
    """

    def __init__(self, burst: float, per_sec: float, max_keys: int = 4096) -> None:
        self.burst = float(burst)
        self.per_sec = float(per_sec)
        self.max_keys = max(1, int(max_keys))
        self._level: "OrderedDict[str, tuple[float, float]]" = OrderedDict()
        self._lock = threading.Lock()

    def _tokens(self, key: str, now: float) -> float:
        have, when = self._level.get(key, (self.burst, now))
        if self.per_sec > 0.0:
            have = min(self.burst, have + (now - when) * self.per_sec)
        return have

    def spend(self, key: str, cost: float = 1.0) -> bool:
        """True when the spend fitted. A refusal costs nothing, so a refused caller is not
        pushed further into debt by retrying -- it simply waits for the refill."""
        if self.burst <= 0.0:
            return False
        now = time.monotonic()
        with self._lock:
            have = self._tokens(key, now)
            if have < cost:
                self._level[key] = (have, now)
                self._level.move_to_end(key)
                return False
            self._level[key] = (have - cost, now)
            self._level.move_to_end(key)
            for dead in [k for k, (lv, wh) in list(self._level.items())[:16]
                         if k != key and lv >= self.burst]:
                self._level.pop(dead, None)
            while len(self._level) > self.max_keys:
                oldest, _ = next(iter(self._level.items()))
                if oldest == key:
                    break
                self._level.pop(oldest, None)
            return True

    def reset(self) -> None:
        with self._lock:
            self._level.clear()
