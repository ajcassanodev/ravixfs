#!/usr/bin/env python3
"""Football pool data poller: nflverse schedules feed -> pool DB.

Same free feed the Pick5 poller uses (nflverse-data GitHub release):
  https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv
It carries kickoff times AND final scores (away_score/home_score, empty until
the game is final), so one fetch covers both the schedule and results.

Upserts pool_games without touching `included`, so the commissioner's
per-week game selection is never overwritten by a poll.

Usage: poll.py [--check]   (--check prints what would be done, writes nothing)
"""
import csv
import io
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pool import db as _db  # noqa: E402

ET = ZoneInfo("America/New_York")
BASE = "https://github.com/nflverse/nflverse-data/releases/download"


def fetch_csv(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "ravixfs-pool-poller"})
    with urllib.request.urlopen(req, timeout=120) as r:
        text = r.read().decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def to_int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def kickoff_utc(gameday: str, gametime: str) -> str:
    local = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    return local.astimezone(timezone.utc).isoformat()


def main(check: bool = False):
    _db.init_db()
    con = _db.connect()
    rows = fetch_csv(f"{BASE}/schedules/games.csv")
    n_upsert = 0
    for g in rows:
        if g.get("game_type") != "REG":
            continue
        if not g.get("gameday") or not g.get("gametime"):
            continue
        try:
            ko = kickoff_utc(g["gameday"], g["gametime"].strip())
        except ValueError:
            continue
        away_score, home_score = to_int(g.get("away_score")), to_int(g.get("home_score"))
        is_final = 1 if (away_score is not None and home_score is not None) else 0
        if not check:
            con.execute(
                "INSERT INTO pool_games (season, week, away_team, home_team, "
                "kickoff_utc, away_score, home_score, is_final) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT (season, week, away_team, home_team) DO UPDATE SET "
                "kickoff_utc=excluded.kickoff_utc, "
                "away_score=excluded.away_score, home_score=excluded.home_score, "
                "is_final=excluded.is_final",
                (int(g["season"]), int(g["week"]), g["away_team"], g["home_team"],
                 ko, away_score, home_score, is_final),
            )
            n_upsert += 1
    if not check:
        con.commit()
    con.close()
    print(f"pool games upserted: {n_upsert}" + (" (check mode: no writes)" if check else ""))


if __name__ == "__main__":
    main(check="--check" in sys.argv)
