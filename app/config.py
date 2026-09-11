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
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

# The one number the ROM decides: `port/shim/fs/cardreq.c` maps the save as a 256 KB file
# because func_02050b78 identifies the chip as 1 << 0x12 bytes.  See app/savecheck.py.
CARD_IMAGE_SIZE = 0x40000

SERVICE_VERSION = "1.0.0"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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
    trust_proxy: bool = False          # read X-Forwarded-For for the rate limiter's key

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
