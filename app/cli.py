"""Operator commands, run inside the container.

    docker exec -it acww-online python -m app.cli set-password <user>
    docker exec -it acww-online python -m app.cli list-users
    docker exec -it acww-online python -m app.cli check-save /data/saves/1/3.sav

`set-password` is the recovery path: there is no email on this server and no reset link,
so the person with shell on the NAS is the person who can reset a password.  It reads the
new password from a prompt (never an argument, which would sit in the shell history and in
`docker inspect`'s command line) and re-hashes it with argon2id.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from .config import Settings
from .savecheck import SaveRejected, validate_card_image
from .security import HASHER, hash_password
from .store import Store


def _store() -> Store:
    settings = Settings.from_env()
    return Store(settings.db_path, settings.saves_dir, settings.save_history)


def cmd_set_password(args: argparse.Namespace) -> int:
    store = _store()
    row = store.user_by_name(args.username)
    if row is None:
        sys.stderr.write("no such user: %s\n" % args.username)
        return 1
    first = args.password or getpass.getpass("new password for %s: " % args.username)
    if args.password is None:
        again = getpass.getpass("again: ")
        if first != again:
            sys.stderr.write("the two entries differ; nothing was changed\n")
            return 1
    if len(first) < 8:
        sys.stderr.write("the password must be at least 8 characters\n")
        return 1
    store.set_password_hash(int(row["id"]), hash_password(first))
    print("password for %s (id %d) reset with %s" % (args.username, int(row["id"]), HASHER))
    return 0


def cmd_list_users(_args: argparse.Namespace) -> int:
    for name in _store().all_usernames():
        print(name)
    return 0


def cmd_check_save(args: argparse.Namespace) -> int:
    with open(args.path, "rb") as fp:
        data = fp.read()
    try:
        verdicts = validate_card_image(data)
    except SaveRejected as bad:
        print("REJECTED: %s" % bad.reason)
        return 1
    for v in verdicts:
        print("%s: the ROM would load this bank" % v.label)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cli",
                                 description="ACWW online service operator commands")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("set-password", help="reset one account's password")
    p.add_argument("username")
    p.add_argument("--password", default=None,
                   help="non-interactive; avoid it, the value lands in the shell history")
    p.set_defaults(func=cmd_set_password)

    p = sub.add_parser("list-users", help="every account on this server")
    p.set_defaults(func=cmd_list_users)

    p = sub.add_parser("check-save", help="run the ROM's own acceptance test on a file")
    p.add_argument("path")
    p.set_defaults(func=cmd_check_save)

    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
