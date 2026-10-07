"""Settings for the ACWW online service.

Everything the operator can turn is an environment variable, because the deployment
target is Synology Container Manager, where a compose file's `environment:` block is the
only knob a non-programmer has.  `Settings.from_env()` is what `main.app` uses; the tests
build a `Settings` directly so they can run several isolated servers in one process.

THE SECRET.  `ACWW_SERVER_SECRET` signs the JWTs.  If it is unset we generate 32 random
bytes on first run and persist them as `<data>/secret.key`, so restarting the container
does not invalidate every stored token -- and we say so in the log, once, loudly, because
a secret the operator did not choose is a fact they need to know before they take a
backup.  The file is written 0600 and its CONTENT is never logged.

SERVERFIX108, THE BOUNDS.  Everything below `trusted_proxies` is a CEILING rather than a
feature: a size checked before an allocation, a budget refilled at a rate the game's own
traffic never reaches, a cap on how many sockets and rooms one process will hold.  They are
environment variables for the same reason the rest are -- the operator's only knob is a
compose file -- but the defaults are the supported deployment and an operator who has to
change one should say why in their own notes.  `ACWW_TRUSTED_PROXIES` defaults to EMPTY,
which means `X-Forwarded-For` is not read at all; see `_client_key` in app/main.py.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

# The one number the ROM decides: `port/shim/fs/cardreq.c` maps the save as a 256 KB file
# because func_02050b78 identifies the chip as 1 << 0x12 bytes.  See app/savecheck.py.
CARD_IMAGE_SIZE = 0x40000

SERVICE_VERSION = "1.2.0"   # 1.1.0: NOTICE155 chat notices; 1.2.0: merchant id on shop notices


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str) -> tuple[str, ...]:
    """A comma- or space-separated list of addresses/networks, in order, no duplicates."""
    raw = os.environ.get(name) or ""
    out: list[str] = []
    for piece in raw.replace(",", " ").split():
        if piece not in out:
            out.append(piece)
    return tuple(out)


@dataclass
class Settings:
    data_dir: Path
    secret: str
    secret_was_generated: bool = False
    allow_register: bool = True
    token_days: int = 30
    auth_rate_limit: int = 10          # attempts ...
    auth_rate_window: int = 60         # ... per this many seconds, per IP
    save_history: int = 20             # versions kept per user
    trust_proxy: bool = False          # LEGACY: on its own it no longer trusts anything

    # F1. The ONLY addresses whose `X-Forwarded-For` is read, as literal addresses or CIDR
    # networks.  Empty (the default) means the header is ignored and the transport peer is
    # the rate limiter's key, so a directly reachable server cannot be talked around.
    trusted_proxies: tuple[str, ...] = ()
    limiter_max_keys: int = 4096       # the limiter's map is bounded and evicts the oldest

    # F3. Sizes checked BEFORE an allocation or a hash.
    max_auth_body: int = 4096          # bytes of JSON a login/register may send
    max_username: int = 24             # matches USERNAME_RE, checked before hashing
    max_password: int = 256            # argon2 on a megabyte is the denial of service
    max_lobby_message: int = 8192      # one lobby JSON text frame
    ws_max_message: int = 65536        # uvicorn's --ws-max-size, the protocol ceiling
    relay_frame_max: int = 4096        # online-spec.md's frame ceiling, enforced here too

    # F4. A directed invitation is pending for this long and no longer.
    invite_ttl: float = 60.0

    # F5. Token buckets: `burst` is the bucket, `per_sec` the refill.  The defaults are far
    # above what the game generates (a save is a handful of PUTs a day; the relay carries at
    # most one WM frame per side per 1/60 s) and far below what a flood needs.
    http_ops_burst: int = 120
    http_ops_per_sec: float = 10.0
    http_bytes_burst: int = 16 * 1024 * 1024
    http_bytes_per_sec: float = 2.0 * 1024 * 1024
    lobby_ops_burst: int = 60
    lobby_ops_per_sec: float = 10.0
    relay_ops_burst: int = 1200
    relay_ops_per_sec: float = 300.0
    relay_bytes_burst: int = 4 * 1024 * 1024
    relay_bytes_per_sec: float = 512.0 * 1024
    max_lobby_sockets: int = 32        # sockets this process will hold at once
    max_rooms: int = 16                # live relay rooms at once
    chat_enabled: bool = True
    max_chat_sockets: int = 128

    @property
    def db_path(self) -> Path:
        return self.data_dir / "acww.sqlite"

    @property
    def saves_dir(self) -> Path:
        return self.data_dir / "saves"

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("ACWW_DATA_DIR", "/data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        secret, generated = _resolve_secret(data_dir)
        return cls(
            data_dir=data_dir,
            secret=secret,
            secret_was_generated=generated,
            allow_register=_env_bool("ACWW_ALLOW_REGISTER", True),
            token_days=_env_int("ACWW_TOKEN_DAYS", 30),
            auth_rate_limit=_env_int("ACWW_AUTH_RATE_LIMIT", 10),
            auth_rate_window=_env_int("ACWW_AUTH_RATE_WINDOW", 60),
            save_history=_env_int("ACWW_SAVE_HISTORY", 20),
            trust_proxy=_env_bool("ACWW_TRUST_PROXY", False),
            trusted_proxies=_env_list("ACWW_TRUSTED_PROXIES"),
            limiter_max_keys=_env_int("ACWW_LIMITER_MAX_KEYS", 4096),
            max_auth_body=_env_int("ACWW_MAX_AUTH_BODY", 4096),
            max_password=_env_int("ACWW_MAX_PASSWORD", 256),
            max_lobby_message=_env_int("ACWW_MAX_LOBBY_MESSAGE", 8192),
            ws_max_message=_env_int("ACWW_WS_MAX_MESSAGE", 65536),
            relay_frame_max=_env_int("ACWW_RELAY_FRAME_MAX", 4096),
            invite_ttl=_env_float("ACWW_INVITE_TTL", 60.0),
            http_ops_burst=_env_int("ACWW_HTTP_OPS_BURST", 120),
            http_ops_per_sec=_env_float("ACWW_HTTP_OPS_PER_SEC", 10.0),
            http_bytes_burst=_env_int("ACWW_HTTP_BYTES_BURST", 16 * 1024 * 1024),
            http_bytes_per_sec=_env_float("ACWW_HTTP_BYTES_PER_SEC", 2.0 * 1024 * 1024),
            lobby_ops_burst=_env_int("ACWW_LOBBY_OPS_BURST", 60),
            lobby_ops_per_sec=_env_float("ACWW_LOBBY_OPS_PER_SEC", 10.0),
            relay_ops_burst=_env_int("ACWW_RELAY_OPS_BURST", 1200),
            relay_ops_per_sec=_env_float("ACWW_RELAY_OPS_PER_SEC", 300.0),
            relay_bytes_burst=_env_int("ACWW_RELAY_BYTES_BURST", 4 * 1024 * 1024),
            relay_bytes_per_sec=_env_float("ACWW_RELAY_BYTES_PER_SEC", 512.0 * 1024),
            max_lobby_sockets=_env_int("ACWW_MAX_LOBBY_SOCKETS", 32),
            max_rooms=_env_int("ACWW_MAX_ROOMS", 16),
            chat_enabled=_env_bool("ACWW_CHAT_ENABLED", True),
            max_chat_sockets=max(1, min(128, _env_int("ACWW_MAX_CHAT_SOCKETS", 128))),
        )


def _resolve_secret(data_dir: Path) -> tuple[str, bool]:
    from_env = os.environ.get("ACWW_SERVER_SECRET", "").strip()
    if from_env:
        return from_env, False
    keyfile = data_dir / "secret.key"
    if keyfile.is_file():
        stored = keyfile.read_text(encoding="ascii").strip()
        if stored:
            return stored, False
    generated = secrets.token_hex(32)
    keyfile.write_text(generated, encoding="ascii")
    try:
        keyfile.chmod(0o600)
    except OSError:
        # Windows and some NAS filesystems do not implement POSIX modes; the file is
        # still inside the private data volume, which is the real boundary.
        pass
    return generated, True
