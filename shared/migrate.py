#!/usr/bin/env python3
"""One-time backfill: copy members from a legacy per-game DB into the shared
member store, preserving pick tokens so every old pick link keeps working.

Usage (run as the ravixfs user, from the repo checkout):
  SHARED_MEMBER_DB=/opt/ravixfs-shared-staging/shared.db \\
  python -m shared.migrate --from /opt/ravixfs-staging/ravixfs.db --game pick5f

After this runs, per-game apps lazily import any token they haven't seen
(see pool/main.py), so this backfill is a convenience, not a requirement.
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared import identity  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True,
                    help="legacy per-game SQLite DB path")
    ap.add_argument("--game", required=True,
                    help="game_key to grant, e.g. pick5f")
    ap.add_argument("--check", action="store_true",
                    help="print what would happen, write nothing")
    args = ap.parse_args()

    if not identity.shared_db_path():
        print("SHARED_MEMBER_DB is not set", file=sys.stderr)
        sys.exit(2)
    identity.init_shared_db()

    src = sqlite3.connect(args.src)
    src.row_factory = sqlite3.Row
    rows = src.execute(
        "SELECT name, pick_token, COALESCE(is_test, 0) AS is_test FROM members"
    ).fetchall()
    src.close()
    print(f"legacy members: {len(rows)}")

    if args.check:
        for r in rows:
            print(f"  would import: {r['name']} (test={bool(r['is_test'])})")
        print("check mode: no writes")
        return

    con = identity.connect_shared()
    n_new = n_seen = 0
    for r in rows:
        existed = identity.get_member_by_token(con, r["pick_token"]) is not None
        m = identity.import_member(con, r["name"], r["pick_token"],
                                   bool(r["is_test"]))
        identity.ensure_membership(con, m["id"], args.game)
        if existed:
            n_seen += 1
        else:
            n_new += 1
    con.close()
    print(f"imported {n_new} new, {n_seen} already present; "
          f"granted game '{args.game}'")


if __name__ == "__main__":
    main()
