"""Read and write any player's save through the admin routes (ADMIN172).

    ACWW_ADMIN_TOKEN=<token> python server/tools/admin_save.py --url https://host users
    ACWW_ADMIN_TOKEN=<token> python server/tools/admin_save.py --url https://host get NAME --out NAME.sav [--version N]
    ACWW_ADMIN_TOKEN=<token> python server/tools/admin_save.py --url https://host put NAME FIXED.sav [--if-match N]
    ACWW_ADMIN_TOKEN=<token> python server/tools/admin_save.py --url https://host nickfix --player 슈슈 [--dry-run]

The token is read from %USERPROFILE%\.acww-admin-token (a copy of the server's data/admin.key)
or from ACWW_ADMIN_TOKEN -- never from the command line, where it would land in shell history
and process listings -- and is never printed. `put` writes a NEW
version; the old one stays in the history (`get --version`).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


# The admin token lives in this file on the admin PC (a copy of the server's data/admin.key),
# or in ACWW_ADMIN_TOKEN. Never in the repository: server/ is mirrored publicly.
TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".acww-admin-token")


def call(url: str, token: str, method: str = "GET", data: bytes | None = None,
         if_match: str | None = None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Admin-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/octet-stream")
    if if_match:
        req.add_header("If-Match", '"%s"' % if_match)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def nickfix(base: str, token: str, a: argparse.Namespace) -> int:
    """Download each chosen account's latest save, repair it (app/nickfix.py), keep the
    original under --backup, and upload the repair as a new version (If-Match = the version
    read, so a save the player wrote meanwhile is never overwritten)."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from app import nickfix as nf                      # noqa: E402
    st, _, body = call(base + "/v1/admin/users", token)
    if st != 200:
        print("admin_save: %d %s" % (st, body.decode("utf-8", "replace")), file=sys.stderr)
        return 1
    users = [u for u in json.loads(body) if u.get("save")]
    if a.user:
        users = [u for u in users if u["username"] == a.user]
    os.makedirs(a.backup, exist_ok=True)
    matched = repaired = 0
    for u in users:
        url = base + "/v1/admin/users/%s/save" % urllib.parse.quote(u["username"], safe="")
        st, hdr, image = call(url, token)
        if st != 200:
            print("  %s: download failed %d" % (u["username"], st))
            continue
        names = nf.player_names(image)
        if a.player and a.player not in names:
            continue
        matched += 1
        version = hdr.get("etag", "").strip('"W/')
        fixed, found = nf.repair(image)
        label = "%s (players %s, v%s)" % (u["username"], ", ".join(names) or "-", version)
        if not found:
            print("%s: clean" % label)
            continue
        print("%s: %d damaged nickname(s)" % (label, len(found)))
        for f in found:
            print("    villager %d +0x%x  %04x -> %04x  (%s)" % (f["villager"], f["offset"],
                                                           f["was"], f["now"], f["name"]))
        if a.dry_run:
            continue
        if not version.isdigit():
            print("    not uploaded: the server sent no version to guard the write with")
            continue
        keep = os.path.join(a.backup, "%s-v%s.sav" % (u["username"], version))
        with open(keep, "wb") as fh:
            fh.write(image)
        st, _, body = call(url, token, "PUT", fixed, version or None)
        if st != 200:
            print("    upload failed %d %s" % (st, body.decode("utf-8", "replace")))
            continue
        repaired += 1
        print("    uploaded %s  (original kept as %s)" % (body.decode("utf-8", "replace"), keep))
    print("nickfix: %d save(s) matched, %d repaired%s" % (matched, repaired,
                                                       " (dry run)" if a.dry_run else ""))
    return 0 if matched else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="server base URL, e.g. https://acww.example")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("users")
    g = sub.add_parser("get")
    g.add_argument("username")
    g.add_argument("--out", required=True)
    g.add_argument("--version", type=int)
    p = sub.add_parser("put")
    p.add_argument("username")
    p.add_argument("file")
    p.add_argument("--if-match")
    n = sub.add_parser("nickfix", help="repair the 0x0a nickname byte (NICKFIX172) in stored saves")
    who = n.add_mutually_exclusive_group(required=True)
    who.add_argument("--player", help="in-game player name, e.g. 슈슈")
    who.add_argument("--user", help="account username")
    who.add_argument("--all", action="store_true", help="every account")
    n.add_argument("--dry-run", action="store_true", help="report, upload nothing")
    n.add_argument("--backup", default="nickfix-backup", help="directory for the originals")
    a = ap.parse_args(argv)

    token = os.environ.get("ACWW_ADMIN_TOKEN", "").strip()
    if not token and os.path.isfile(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="ascii") as fh:
            token = fh.read().strip()
    if not token:
        print("admin_save: no admin token. Copy the server's data/admin.key into %s "
              "(or set ACWW_ADMIN_TOKEN)" % TOKEN_FILE, file=sys.stderr)
        return 2
    base = a.url.rstrip("/")

    if a.cmd == "users":
        st, _, body = call(base + "/v1/admin/users", token)
        if st != 200:
            print("admin_save: %d %s" % (st, body.decode("utf-8", "replace")), file=sys.stderr)
            return 1
        for u in json.loads(body):
            s = u.get("save") or {}
            print("%5d  %-24s  v%-4s %s" % (u["user_id"], u["username"], s.get("version", "-"),
                                            s.get("updated_utc", "")))
        return 0

    if a.cmd == "nickfix":
        return nickfix(base, token, a)

    name = urllib.parse.quote(a.username, safe="")
    url = base + "/v1/admin/users/%s/save" % name
    if a.cmd == "get":
        if a.version is not None:
            url += "?version=%d" % a.version
        st, hdr, body = call(url, token)
        if st != 200:
            print("admin_save: %d %s" % (st, body.decode("utf-8", "replace")), file=sys.stderr)
            return 1
        with open(a.out, "wb") as f:
            f.write(body)
        print("saved %s: %d bytes, version %s, sha256 %s"
              % (a.out, len(body), hdr.get("etag", "?"), hashlib.sha256(body).hexdigest()))
        return 0

    with open(a.file, "rb") as f:
        data = f.read()
    st, _, body = call(url, token, "PUT", data, a.if_match)
    if st != 200:
        print("admin_save: %d %s" % (st, body.decode("utf-8", "replace")), file=sys.stderr)
        return 1
    print("uploaded: %s" % body.decode("utf-8", "replace"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
