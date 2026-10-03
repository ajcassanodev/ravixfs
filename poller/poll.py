#!/usr/bin/env python3
"""ravixfs data poller: nflverse release CSVs -> SQLite.

Runs every 15 min via systemd timer. Always refreshes games + rosters (small);
pulls weekly player stats only when games are near (kickoff within +/-6h or
any game today), so the offseason timer stays cheap.

Usage: poll.py [--check]   (--check prints what would be done, writes nothing)
"""
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
BASE = "https://github.com/nflverse/nflverse-data/releases/download"
DB_PATH = os.environ.get("RAVIXFS_DB", "/opt/ravixfs/ravixfs.db")
DEF_GROUPS = ("DB", "DL", "LB")

# weekly_stats column -> nflverse column (see DATA_NOTES.md)
STAT_COLS = {
    "tackles": ("def_tackles_solo", "def_tackle_assists"),  # summed; with_assist unreliable
    "sacks": ("def_sacks",),
    "interceptions": ("def_interceptions",),
    "forced_fumbles": ("def_fumbles_forced",),
    "fumble_recoveries": ("fumble_recovery_opp",),
    "safeties": ("def_safeties",),
    "def_tds": ("def_tds", "fumble_recovery_tds"),  # disjoint; summed
}


def fetch_csv(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "ravixfs-poller"})
    with urllib.request.urlopen(req, timeout=120) as r:
        text = r.read().decode("utf-8")
    import csv, io
    return list(csv.DictReader(io.StringIO(text)))


def to_float(v) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def kickoff_utc(gameday: str, gametime: str) -> str:
    # gametime is Eastern
    local = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    return local.astimezone(timezone.utc).isoformat()


def main(check: bool = False):
    now = datetime.now(timezone.utc)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")

    # ---- games (always) ----
    games = fetch_csv(f"{BASE}/schedules/games.csv")
    seasons = sorted({int(g["season"]) for g in games if g["season"]})
    season = seasons[-1]
    n_games = 0
    if not check:
        for g in games:
            if g.get("game_type") != "REG":
                continue
            if not g["gameday"] or not g["gametime"]:
                continue
            try:
                ko = kickoff_utc(g["gameday"], g["gametime"].strip())
            except ValueError:
                continue
            con.execute(
                "INSERT INTO games (season, week, away_team, home_team, kickoff_utc) "
                "VALUES (?,?,?,?,?) ON CONFLICT (season, week, away_team, home_team) "
                "DO UPDATE SET kickoff_utc=excluded.kickoff_utc",
                (int(g["season"]), int(g["week"]), g["away_team"], g["home_team"], ko),
            )
            n_games += 1
    print(f"season={season} games rows: {len(games)}")

    # ---- near games? ----
    kicks = []
    for g in games:
        if g.get("game_type") != "REG" or g["season"] != str(season):
            continue
        if not g["gameday"] or not g["gametime"]:
            continue
        try:
            kicks.append(kickoff_utc(g["gameday"], g["gametime"].strip()))
        except ValueError:
            continue
    near = any(
        abs(datetime.fromisoformat(k) - now) < timedelta(hours=6) or
        datetime.fromisoformat(k).date() == now.date()
        for k in kicks
    )
    print(f"games near now: {near}")
    if not near:
        print("light mode: rosters + games only")

    # ---- rosters (always) ----
    roster = fetch_csv(f"{BASE}/rosters/roster_{season}.csv")
    ts = now.isoformat()
    n_def = 0
    if not check:
        for r in roster:
            if r.get("position") not in DEF_GROUPS:
                continue
            n_def += 1
            # full_name is the display name ("Myles Garrett"); football_name is
            # just the first name ("Myles") -- do not use it alone.
            name = r.get("full_name") or r.get("football_name") or ""
            con.execute(
                "INSERT INTO players (player_id, name, team, position_group, status, source_updated_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT (player_id) DO UPDATE SET team=excluded.team, "
                "position_group=excluded.position_group, status=excluded.status, "
                "source_updated_at=excluded.source_updated_at",
                (r["gsis_id"], name, r.get("team"), r.get("position"),
                 r.get("status"), ts),
            )
    print(f"roster rows: {len(roster)}, defensive: {n_def}")

    # ---- weekly stats (only near games, or forced) ----
    force_full = os.environ.get("RAVIXFS_FORCE_FULL") == "1"
    if near or force_full:
        url = f"{BASE}/stats_player/stats_player_week_{season}.csv"
        try:
            wstats = fetch_csv(url)
        except Exception as e:
            print(f"weekly stats fetch failed: {e}")
            wstats = []
        print(f"weekly stat rows: {len(wstats)}")
        if not check:
            latest = {}  # player_id -> (week, row) for the most recent week seen
            for w in wstats:
                if w.get("position_group") not in DEF_GROUPS:
                    continue
                pid = w["player_id"]
                vals = {}
                for key, cols in STAT_COLS.items():
                    vals[key] = round(sum(to_float(w.get(c)) for c in cols), 2)
                con.execute(
                    "INSERT INTO weekly_stats (player_id, season, week, tackles, sacks, "
                    "interceptions, forced_fumbles, fumble_recoveries, safeties, def_tds) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT (player_id, season, week) DO UPDATE SET "
                    "tackles=excluded.tackles, sacks=excluded.sacks, "
                    "interceptions=excluded.interceptions, forced_fumbles=excluded.forced_fumbles, "
                    "fumble_recoveries=excluded.fumble_recoveries, safeties=excluded.safeties, "
                    "def_tds=excluded.def_tds",
                    (pid, int(w["season"]), int(w["week"]), vals["tackles"], vals["sacks"],
                     vals["interceptions"], vals["forced_fumbles"], vals["fumble_recoveries"],
                     vals["safeties"], vals["def_tds"]),
                )
                wk = int(w["week"])
                if pid not in latest or wk >= latest[pid][0]:
                    latest[pid] = (wk, w)
            # name/position/team from each player's most recent week only
            for pid, (wk, w) in latest.items():
                con.execute(
                    "UPDATE players SET name=?, position=?, team=?, source_updated_at=? "
                    "WHERE player_id=?",
                    (w.get("player_display_name") or w.get("player_name"),
                     w.get("position"), w.get("team"), ts, pid),
                )
    if not check:
        con.commit()
    con.close()
    print("check mode: no writes" if check else "done")


if __name__ == "__main__":
    main(check="--check" in sys.argv)
