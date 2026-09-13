"""Image-baked revision; runtime environment changes must not relabel an old image."""

from pathlib import Path
import re

COMMIT_FILE = Path(__file__).with_name("build_commit.txt")


def build_commit() -> str:
    try:
        value = COMMIT_FILE.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return "unknown"
    return value.lower() if re.fullmatch(r"[0-9a-fA-F]{40}", value) else "unknown"
