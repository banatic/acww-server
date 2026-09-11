"""The ROM's own acceptance test for a card image, ported from `port/tools/savetool.py`.

WHY THIS IS ON THE SERVER AT ALL.  The service stores the cartridge's 256 KB flash image
and hands it back to any PC the owner plays on.  A corrupt image that the server accepted
is a town that will not load -- and the owner would discover that only at the title
screen, with the good copy already overwritten.  So the upload is refused here, with the
reason, and the local file stays authoritative.

THE TEST, and it is the ROM's, not an editor's.  `func_020a1a40` (transcribed in
`port/shim/game/savepoll.c`) decides whether a bank is a save, and it does exactly two
things with the buffer it read:

    t = func_02050920(buffer, g_020d1c08[slot]);   /* 16-bit wrapping word sum, WHOLE bank */
    if (func_0209f180(buffer) == 0) return 4;      /* "not a save" */
    return t == 0 ? 0 : 4;                         /* "a save" only when the sum is zero */

`func_02050920` is a plain 16-bit wrapping sum of length/2 little-endian words, no skip and
no seed, and `g_020d1c08[0]` is 0x173fc, so the summed range is the whole bank INCLUDING the
two bytes at +0x173fa that sit after the checksum word.  `func_0209f180` is two byte tests:
`bank[0] == 0x32` (Korea's gamecode low byte) and, through `func_0209fb3c`,
`bank[0x173fa] == 2`.

The bank verifies when the sum comes to zero because the stored word at +0x173f8 is the
two's complement of the sum over every OTHER word -- which is what `savetool.py fix`, three
independent public editors, and the game itself write.  The three tests are independent: a
bank can carry a perfect checksum and still be rejected for the flag, so each is reported
separately and the refusal names the one that failed.

Nothing here needs the ROM, and no game asset is shipped with it -- these are five
constants and an addition.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass

FLASH_SIZE = 0x40000
BANK_SIZE = 0x173FC
BANK1_OFF = 0x00000
BANK2_OFF = 0x173FC
CHECKSUM_OFF = 0x173F8
CHECKSUM_WORD = CHECKSUM_OFF // 2       # 0xb9fc
BANK_WORDS = BANK_SIZE // 2             # 0xb9fe

ROM_GAMECODE_WANT = 0x32                # func_0209f180's first test, bank[0]
ROM_FLAG_OFF = 0x173FA                  # func_0209fb3c reads (bank + 0x173f8)[2]
ROM_FLAG_WANT = 2

BANKS = ((BANK1_OFF, "bank 1"), (BANK2_OFF, "bank 2"))


class SaveRejected(ValueError):
    """The image would not load on the cartridge. `.reason` is for the 400 body."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def word_sum(bank: bytes) -> int:
    """16-bit wrapping sum of every little-endian u16 word in the bank, nothing skipped."""
    mv = memoryview(bank).cast("H")
    total = sum(mv)
    if sys.byteorder == "big":
        # memoryview.cast is NATIVE endian; the cartridge's words are little-endian.
        total = sum(((w >> 8) | ((w & 0xFF) << 8)) for w in mv)
    return total & 0xFFFF


def word_sum_skipping_checksum(bank: bytes) -> int:
    return (word_sum(bank) - _u16(bank, CHECKSUM_OFF)) & 0xFFFF


def compute_checksum(bank: bytes) -> int:
    """The value that belongs at +0x173f8."""
    return (-word_sum_skipping_checksum(bank)) & 0xFFFF


def residual(bank: bytes) -> int:
    """The sum over ALL words. Zero exactly when the bank verifies."""
    return word_sum(bank)


def _u16(buf: bytes, off: int) -> int:
    return buf[off] | (buf[off + 1] << 8)


@dataclass
class BankVerdict:
    label: str
    gamecode_ok: bool
    flag_ok: bool
    checksum_ok: bool

    @property
    def accepted(self) -> bool:
        return self.gamecode_ok and self.flag_ok and self.checksum_ok

    def why(self) -> str:
        bad = []
        if not self.gamecode_ok:
            bad.append("gamecode byte +0x0000 is not 0x32 (func_0209f180)")
        if not self.flag_ok:
            bad.append("flag byte +0x173fa is not 2 (func_0209fb3c)")
        if not self.checksum_ok:
            bad.append("the 16-bit word sum over the bank is not zero (func_02050920)")
        return "%s: %s" % (self.label, "; ".join(bad))


def inspect_bank(bank: bytes, label: str) -> BankVerdict:
    return BankVerdict(
        label=label,
        gamecode_ok=bank[0] == ROM_GAMECODE_WANT,
        flag_ok=bank[ROM_FLAG_OFF] == ROM_FLAG_WANT,
        checksum_ok=residual(bank) == 0,
    )


def validate_card_image(data: bytes) -> list[BankVerdict]:
    """Raise SaveRejected unless the ROM would load this image. Returns both verdicts.

    Both banks must pass.  The game mirrors bank 1 into bank 2 after every save
    (`Sav::Finish`, and the port's own writer does the same), so a real image always has
    two good banks; one good bank means the write was interrupted, and storing that as the
    owner's cloud copy would hand them a half-written town on the next PC.
    """
    if len(data) != FLASH_SIZE:
        raise SaveRejected(
            "a card image is exactly %d bytes (0x%x); got %d"
            % (FLASH_SIZE, FLASH_SIZE, len(data))
        )
    verdicts = [inspect_bank(data[off:off + BANK_SIZE], label) for off, label in BANKS]
    bad = [v for v in verdicts if not v.accepted]
    if bad:
        raise SaveRejected(
            "the ROM would refuse this image (func_020a1a40) -- "
            + " | ".join(v.why() for v in bad)
        )
    return verdicts


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
