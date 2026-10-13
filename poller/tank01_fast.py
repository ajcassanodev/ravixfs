#!/usr/bin/env python3
"""Tank01 1-minute latency test: oneshot, fired by a 1-min systemd timer.

Polls getNFLBoxScore every minute for the game(s) named in TANK01_FAST_GAMES
(comma-separated Tank01 game IDs, e.g. "20261015_SEA@DEN") while each is live.
Shares tank01_live's parsing, player matching, storage, and monthly budget
guard -- the 15-min live poller keeps running alongside; both upsert the same
live_stats rows.

A game counts as live from 15 min before its scheduled kickoff until 4.5h
after. Outside that (or with no games configured / no key / budget spent)
this is a cheap no-op: one DB read, no API calls.

Usage: ./venv/bin/python poller/tank01_fast.py
"""
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tank01_live as tl

ET = ZoneInfo("America/New_York")
DB_PATH = os.environ.get("RAVIXFS_DB", "/opt/ravixfs/ravixfs.db")
PRE_GAME = timedelta(minutes=15)


def _parse_game_id(game_id: str):
    m = re.fullmatch(r"(\d{8})_([A-Z]{2,3})@([A-Z]{2,3})", game_id.strip())
    return m.groups() if m else (None, None, None)


def _test_games(con, game_ids, now):
    """[(season, week, game_id)] for configured games inside the live window."""
    out = []
    for gid in game_ids:
        date_s, away, home = _parse_game_id(gid)
        if not date_s:
            print(f"tank01 fast: ignoring malformed game id {gid!r}")
            continue
        rows = con.execute(
            "SELECT season, week, kickoff_utc FROM games "
            "WHERE away_team = ? AND home_team = ?", (away, home)).fetchall()
        for r in rows:
            try:
                ko = datetime.fromisoformat(r["kickoff_utc"])
            except (TypeError, ValueError):
                continue
            if ko.tzinfo is None:
                ko = ko.replace(tzinfo=timezone.utc)
            if ko.astimezone(ET).strftime("%Y%m%d") != date_s:
                continue
            if ko - PRE_GAME <= now <= ko + tl.LIVE_WINDOW:
                out.append((r["season"], r["week"], gid))
    return out


def main() -> None:
    key = os.environ.get("TANK01_API_KEY", "").strip()
    game_ids = [g for g in os.environ.get("TANK01_FAST_GAMES", "").split(",") if g.strip()]
    now = datetime.now(timezone.utc)
    if not key:
        print("tank01 fast: no API key, skipping")
        return
    if not game_ids:
        print("tank01 fast: TANK01_FAST_GAMES not set, skipping")
        return
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    month = tl._month_key(now)
    if not tl._budget_ok(con, month):
        print(f"tank01 fast: monthly budget reached ({tl.MONTHLY_BUDGET}), skipping")
        con.close()
        return
    games = _test_games(con, game_ids, now)
    if not games:
        print("tank01 fast: no test game live, skipping")
        con.close()
        return
    n_calls, n_players, n_skipped = tl._poll_games(con, games, key, now)
    tl._record_calls(con, month, n_calls)
    con.commit()
    con.close()
    print(f"tank01 fast: {n_calls} call(s), {n_players} player row(s) "
          f"upserted ({n_skipped} unmatched)")


if __name__ == "__main__":
    main()
