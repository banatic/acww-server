"""NICKFIX172 / NAME175 on a stored card image: the villager memory record a client wrote
counters into (docs/log/name-175.md in the client repository).

A native body of the client (func_0207dac8, before NAME175) stored a villager's per-index
counters -- clamped to 0..10 -- at villager + 0x19 + idx instead of villager + 0x734 + 0x19 + idx:
bytes +0x19..+0x20 of the villager's memory RECORD 0. That window holds the name's units 4..5,
two bytes after it, and the nickname's units 0..1. A player was called "민@" (the nickname's
0x31 clamped to 0x0a), and later the NAME itself changed, so villagers greeted the player as a
stranger and made a second record for them.

The client repairs it in RAM when it loads a save (port/platform/nickfix.c); this is the same
rule for the 256 KB image, for a player who has not started the repaired client yet. Record 0
only, both banks, re-summed:

  1. name: from the town's player whose town id and player id record 0 names, when the two
     differ only in the window (+0x19..+0x1b) by values 0..10; for another town's player, from
     a DEFAULT nickname's units 4..5 when every untouched byte of the two agrees;
  2. +0x1d (0 in every healthy record) back to 0 when the name was repaired;
  3. a nickname damaged in the window: back to the name. A custom nickname is reset only when
     the name was damaged too. A custom nickname's unit 1 holding 1..9 was its terminator,
     counted up, and goes back to 0;
  4. when the name was repaired, a later record with the same identity block is the stranger
     record the bug made: cleared to the empty state (+0x1c = 2, +0x6a = 0xfff1).
"""

from __future__ import annotations

from .savecheck import BANK2_OFF, BANK_SIZE, CHECKSUM_OFF, FLASH_SIZE, compute_checksum

BANK_OFFS = (0, BANK2_OFF)
VILLAGERS = 0x9284
STRIDE = 0x7EC
RECORD = 0x80
NAME_N = 6
PLAYERS = 0x14
PLAYER_SIZE = 0x249C
P_IDENT = 0x247E
P_NAME = 0x248E
IDENT = 0x1C
NAME = 0x10
NICK = 0x1E
W_LO, W_HI = 0x19, 0x20


def _u16(b: bytes | bytearray, o: int) -> int:
    return b[o] | (b[o + 1] << 8)


def _name(b: bytes | bytearray, o: int) -> str:
    out = []
    for i in range(NAME_N):
        u = _u16(b, o + 2 * i)
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


def _window(i: int) -> bool:
    return W_LO <= i <= W_HI


def _player_of(b: bytearray, bank: int, rec: int) -> int | None:
    for p in range(4):
        idp = bank + PLAYERS + p * PLAYER_SIZE + P_IDENT
        if b[idp + 0x0E] == 0 and b[idp + 0x0F] == 0:
            continue
        if b[idp:idp + 2] == b[rec:rec + 2] and b[idp + 0x0E:idp + 0x10] == b[rec + 0x0E:rec + 0x10]:
            return idp
    return None


def _repair_name(b: bytearray, bank: int, rec: int) -> str | None:
    idp = _player_of(b, bank, rec)
    if idp is not None:
        diff = [i for i in range(IDENT) if b[rec + i] != b[idp + i]]
        if not diff or any(not _window(i) or b[rec + i] > 10 for i in diff):
            return None
        b[rec + W_LO:rec + IDENT] = b[idp + W_LO:idp + IDENT]
        return "name restored from the player"
    if b[rec + NAME + 4:rec + NAME + 8] != b[rec + NICK + 4:rec + NICK + 8]:
        return None
    if b[rec + NAME + 8] != b[rec + NICK + 8] or b[rec + NAME + 3] != b[rec + NICK + 3]:
        return None
    diff = [i for i in range(9, 12) if b[rec + NAME + i] != b[rec + NICK + i]]
    if not diff or any(b[rec + NAME + i] > 10 for i in diff):
        return None
    b[rec + NAME + 9:rec + NAME + 12] = b[rec + NICK + 9:rec + NICK + 12]
    return "name restored from its default nickname"


def _repair_nick(b: bytearray, rec: int, damaged: bool) -> str | None:
    bad = other = False
    for i in range(12):
        if b[rec + NICK + i] == b[rec + NAME + i]:
            continue
        if _window(NICK + i) and b[rec + NICK + i] <= 10:
            bad = True
        else:
            other = True
    if bad and other and not damaged and b[rec + NICK + 3] == 0 and 1 <= b[rec + NICK + 2] <= 9:
        b[rec + NICK + 2] = 0
        return "a counted-up nickname terminator restored"
    if not bad or (other and not damaged):
        return None
    b[rec + NICK:rec + NICK + 12] = b[rec + NAME:rec + NAME + 12]
    return "damaged custom nickname reset to the name" if other else "nickname restored from the name"


def _clear_duplicates(b: bytearray, rec: int) -> list[int]:
    cleared = []
    for r in range(1, 8):
        o = rec + r * RECORD
        if b[o:o + IDENT] != b[rec:rec + IDENT]:
            continue
        b[o:o + RECORD] = bytes(RECORD)
        b[o + 0x1C] = 2
        b[o + 0x6A] = 0xF1
        b[o + 0x6B] = 0xFF
        cleared.append(r)
    return cleared


def repair(image: bytes) -> tuple[bytes, list[dict]]:
    """(repaired image, the repairs made). The image is returned unchanged with [] if clean."""
    if len(image) != FLASH_SIZE:
        raise ValueError("a card image is %d bytes" % FLASH_SIZE)
    b = bytearray(image)
    found: list[dict] = []
    for bank_no, bank in enumerate(BANK_OFFS):
        before = bytes(b[bank:bank + BANK_SIZE])
        for v in range(8):
            rec = bank + VILLAGERS + v * STRIDE
            if b[rec + 0x7AF] == 0xFF:                      # empty villager slot
                continue
            if _u16(b, rec + NAME) in (0, 0xFFFF):          # empty record
                continue
            old = bytes(b[rec:rec + RECORD])
            what = []
            named = _repair_name(b, bank, rec)
            if named:
                what.append(named)
                if 1 <= b[rec + 0x1D] <= 10:
                    b[rec + 0x1D] = 0
            nick = _repair_nick(b, rec, bool(named))
            if nick:
                what.append(nick)
            dups = _clear_duplicates(b, rec) if named else []
            if dups:
                what.append("duplicate record(s) %s cleared" % ", ".join(map(str, dups)))
            if bank_no == 0:
                for k in range(0, RECORD, 2):
                    was, now = _u16(old, k), _u16(b, rec + k)
                    if was != now:
                        found.append({"villager": v, "offset": k, "was": was, "now": now,
                                      "name": _name(b, rec + NAME), "what": "; ".join(what)})
                for r in dups:
                    found.append({"villager": v, "offset": r * RECORD, "was": 0, "now": 0,
                                  "name": _name(b, rec + NAME), "what": "duplicate record %d cleared" % r})
        if bytes(b[bank:bank + BANK_SIZE]) != before:
            ck = compute_checksum(bytes(b[bank:bank + BANK_SIZE]))
            b[bank + CHECKSUM_OFF] = ck & 0xFF
            b[bank + CHECKSUM_OFF + 1] = (ck >> 8) & 0xFF
    return bytes(b), found
