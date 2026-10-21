"""Auto-pick method test: CPU Random vs CPU Smart (staging only).

Two test members play out the rest of the season with computer-made picks
so we can measure how well the computer picks before auto-picks become a
member feature / production add-on:

  - "CPU Random": 5 defenders drawn at random from the projections pool
    (every defender with at least one game this season -- the same pool
    members pick from). No filtering: hurt, inactive and bye-week players
    are all eligible. The draw is seeded per week so it is reproducible.
  - "CPU Smart": top 5 by matchup-adjusted projection (app/matchup.py),
    skipping bye-week and non-active players -- the computer analyzes the
    matchups and plays the best available defenders.

Generation rides the poller: on Thursdays from 9:00 AM ET until the week's
first kickoff, any poller run fills the CPU members' picks if they don't
have 5 yet (idempotent). Staging only: guarded on the RAVIXFS_DB path so
this can never create members or picks in the production database.
"""

import os
import random
import secrets
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from . import db as _db
from . import matchup as _matchup
from . import scoring as _scoring

ET = ZoneInfo("America/New_York")
CPU_MEMBERS = (("CPU Random", "random"), ("CPU Smart", "smart"))
PICKS_PER_WEEK = 5
WINDOW_START = dtime(9, 0)  # Thursday 9:00 AM ET


def _staging_db() -> bool:
    return "staging" in os.environ.get("RAVIXFS_DB", "")


def ensure_cpu_members(con) -> dict:
    """Create the two CPU test members if missing; return {name: id}."""
    ids = {}
    for name, _method in CPU_MEMBERS:
        row = con.execute(
            "SELECT id FROM members WHERE name = ?", (name,)).fetchone()
        if row:
            ids[name] = row["id"]
        else:
            cur = con.execute(
                "INSERT INTO members (name, pick_token, is_test) "
                "VALUES (?, ?, 1)",
                (name, secrets.token_urlsafe(24)))
            ids[name] = cur.lastrowid
    return ids


def _first_kickoff(con, season: int, week: int):
    row = con.execute(
        "SELECT MIN(kickoff_utc) AS ko FROM games "
        "WHERE season = ? AND week = ?", (season, week)).fetchone()
    if not row or not row["ko"]:
        return None
    ko = datetime.fromisoformat(row["ko"].replace("Z", "+00:00"))
    return ko if ko.tzinfo else ko.replace(tzinfo=timezone.utc)


def generation_due(con, season: int, week: int, now_et: datetime) -> bool:
    """Thursday 9:00 AM ET until the week's first kickoff."""
    if now_et.weekday() != 3 or now_et.time() < WINDOW_START:
        return False
    ko = _first_kickoff(con, season, week)
    if ko is not None and now_et.astimezone(timezone.utc) >= ko:
        return False
    return True


def random_picks(con, season: int, week: int, n: int = PICKS_PER_WEEK):
    """Uniform random draw from the projections pool. No filtering."""
    rows = _scoring.projections(con, season, week - 1)
    pool = sorted(r["player_id"] for r in rows)
    rng = random.Random(f"cpu-random-{season}-w{week}")
    return rng.sample(pool, min(n, len(pool)))


def smart_picks(con, season: int, week: int, n: int = PICKS_PER_WEEK):
    """Top-n by matchup-adjusted projection; skips bye + non-active."""
    rows = _scoring.projections(con, season, week - 1)
    _matchup.annotate_rows(con, season, week, week - 1, rows,
                           _scoring.scoring_map(con))
    elig = [r for r in rows
            if r.get("opponent")
            and (not r["status"] or r["status"].upper() == "ACT")]
    elig.sort(key=lambda r: (-r["adj_pts"], -r["proj_pts"], r["name"]))
    return [r["player_id"] for r in elig[:n]]


def run_cpu_picks(con, season: int, now_et: datetime = None) -> str:
    """Fill the CPU members' picks for the current pick week, if due."""
    if not _staging_db():
        return "cpu picks skipped: not the staging database"
    now_et = now_et or datetime.now(ET)
    week = _db.current_pick_week(con, season)
    if not generation_due(con, season, week, now_et):
        return f"cpu picks: not due (week {week})"
    ids = ensure_cpu_members(con)
    stamp = datetime.now(timezone.utc).isoformat()
    filled = []
    for name, method in CPU_MEMBERS:
        mid = ids[name]
        have = {r["player_id"] for r in con.execute(
            "SELECT player_id FROM weekly_picks "
            "WHERE member_id = ? AND season = ? AND week = ?",
            (mid, season, week))}
        if len(have) >= PICKS_PER_WEEK:
            continue
        picks = (random_picks(con, season, week) if method == "random"
                 else smart_picks(con, season, week))
        for pid in picks:
            if len(have) >= PICKS_PER_WEEK:
                break
            if pid in have:
                continue
            con.execute(
                "INSERT OR IGNORE INTO weekly_picks "
                "(member_id, player_id, season, week, submitted_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (mid, pid, season, week, stamp))
            have.add(pid)
        filled.append(f"{name} ({len(have)} picks)")
    con.commit()
    return f"cpu picks week {week}: " + (", ".join(filled) or "already full")
