"""Read and write any player's save through the admin routes (ADMIN172).

    ACWW_ADMIN_TOKEN=<token> python server/tools/admin_save.py --url https://host users
    ACWW_ADMIN_TOKEN=<token> python server/tools/admin_save.py --url https://host get NAME --out NAME.sav [--version N]
    ACWW_ADMIN_TOKEN=<token> python server/tools/admin_save.py --url https://host put NAME FIXED.sav [--if-match N]

The token is read from the environment only -- never from the command line, where it would
land in shell history and process listings -- and is never printed. `put` writes a NEW
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
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


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
    a = ap.parse_args(argv)

    token = os.environ.get("ACWW_ADMIN_TOKEN", "").strip()
    if not token:
        print("admin_save: set ACWW_ADMIN_TOKEN in the environment", file=sys.stderr)
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
              % (a.out, len(body), hdr.get("ETag", "?"), hashlib.sha256(body).hexdigest()))
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
