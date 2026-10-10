"""NICKFIX172 on a stored card image: the villager nickname byte some play session overwrote
with 0x0a (a player called "민@"; docs/log/hwwatch-172.md in the client repository).

The client repairs it in RAM when it loads a save (port/platform/nickfix.c). This is the same
rule applied to the 256 KB image, for a player who has not started the repaired client yet:
in each villager's first 0x400 bytes (villagers at bank + 0x9284, stride 0x7ec), a 6-unit name
followed 0x0e bytes later by a nickname equal to it EXCEPT that the second unit's low byte is
0x0a gets that unit back from the name. Both banks are repaired and re-summed.
"""

from __future__ import annotations

from .savecheck import BANK2_OFF, BANK_SIZE, CHECKSUM_OFF, FLASH_SIZE, compute_checksum

BANK_OFFS = (0, BANK2_OFF)
VILLAGERS = 0x9284
STRIDE = 0x7EC
SPAN = 0x400
GAP = 0x0E
NAME_N = 6
PLAYERS = 0x14
PLAYER_SIZE = 0x249C
P_NAME = 0x248E


def _u16(b: bytes | bytearray, o: int) -> int:
    return b[o] | (b[o + 1] << 8)


def _name(b: bytes | bytearray, o: int) -> str:
    units = [_u16(b, o + 2 * i) for i in range(NAME_N)]
    out = []
    for u in units:
        if u in (0, 0xFFFF):
            break
        out.append(chr(u))
    return "".join(out)


def player_names(image: bytes) -> list[str]:
    """The four player slots' names in bank 1 (empty slots skipped)."""
    names = []
    for i in range(4):
        n = _name(image, PLAYERS + i * PLAYER_SIZE + P_NAME)
        if n:
            names.append(n)
    return names


def _damaged(b: bytearray, name: int, nick: int) -> bool:
    n0, n1 = _u16(b, name), _u16(b, name + 2)
    k0, k1 = _u16(b, nick), _u16(b, nick + 2)
    if n0 in (0, 0xFFFF) or n1 == 0:
        return False
    if k0 != n0 or (k1 >> 8) != (n1 >> 8):
        return False
    if (k1 & 0xFF) != 0x0A or (n1 & 0xFF) == 0x0A:
        return False
    return all(_u16(b, nick + 2 * i) == _u16(b, name + 2 * i) for i in range(2, NAME_N))


def repair(image: bytes) -> tuple[bytes, list[dict]]:
    """(repaired image, the repairs made). The image is returned unchanged with [] if clean."""
    if len(image) != FLASH_SIZE:
        raise ValueError("a card image is %d bytes" % FLASH_SIZE)
    b = bytearray(image)
    found: list[dict] = []
    for bank_no, bank in enumerate(BANK_OFFS):
        touched = False
        for v in range(8):
            base = bank + VILLAGERS + v * STRIDE
            if b[base + 0x7AF] == 0xFF:            # empty villager slot
                continue
            for o in range(0, SPAN - GAP - NAME_N * 2 + 1, 2):
                name, nick = base + o, base + o + GAP
                if not _damaged(b, name, nick):
                    continue
                was = _u16(b, nick + 2)
                b[nick + 2] = b[name + 2]
                touched = True
                if bank_no == 0:
                    found.append({"villager": v, "offset": o + GAP + 2,
                                  "was": was, "now": _u16(b, nick + 2),
                                  "name": _name(b, name)})
        if touched:
            ck = compute_checksum(bytes(b[bank:bank + BANK_SIZE]))
            b[bank + CHECKSUM_OFF] = ck & 0xFF
            b[bank + CHECKSUM_OFF + 1] = (ck >> 8) & 0xFF
    return bytes(b), found
