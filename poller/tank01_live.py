"""Tank01 live scoring: in-progress game box scores -> live_stats.

Runs inside the 15-min poller. For every game currently live (kicked off,
within LIVE_WINDOW after kickoff), fetches Tank01's getNFLBoxScore, maps the
per-player defensive stats onto the app's scoring keys, and upserts rows into
live_stats.

Precedence lives in app/scoring.py: nflverse weekly_stats wins whenever a row
exists (final numbers); live_stats only fills the gap while games are in
progress or before the batch feed publishes.

Budget: Tank01's free tier is 1,000 requests/month. Every box-score call is
counted in tank01_usage and polling stops for the month at 950 calls, so a
bug can never burn the quota. Typical week: ~16 games x ~13 polls ≈ 200.

Safe no-op without TANK01_API_KEY, with no live games, or past the budget.
Failures never break the main poll.
"""
import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

API_HOST = "tank01-nfl-live-in-game-real-time-statistics-nfl.p.rapidapi.com"
ET = ZoneInfo("America/New_York")
LIVE_WINDOW = timedelta(hours=4, minutes=30)  # covers OT thrillers
MONTHLY_BUDGET = 950  # free tier is 1,000 req/month; stop before the cap

# Tank01 Defense category -> ravixfs scoring key (validated 21/21 vs nflverse
# in the 2026-10-12 Bills @ Rams trial; safeties are team-level, not per-player).
FIELD_MAP = {
    "totalTackles": "tackles",
    "sacks": "sacks",
    "defensiveInterceptions": "interceptions",
    "forcedFumbles": "forced_fumbles",
    "fumblesRecovered": "fumble_recoveries",
    "defTD": "def_tds",
}

STAT_COLS = ("tackles", "sacks", "interceptions", "forced_fumbles",
             "fumble_recoveries", "safeties", "def_tds")


def _norm(name: str) -> str:
    return re.sub(r"[^a-z ]", "", (name or "").lower()).strip()


def _unwrap(data):
    """RapidAPI wraps the payload as {statusCode, body: {...}}."""
    if isinstance(data, dict) and isinstance(data.get("body"), dict):
        return data["body"]
    return data if isinstance(data, dict) else {}


def _fetch_boxscore(game_id: str, key: str) -> dict:
    url = f"https://{API_HOST}/getNFLBoxScore?gameID={game_id}"
    req = urllib.request.Request(url, headers={
        "x-rapidapi-key": key,
        "x-rapidapi-host": API_HOST,
        "User-Agent": "ravixfs-live/1.0",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return _unwrap(json.loads(resp.read().decode("utf-8")))


def _month_key(now: datetime) -> str:
    return now.strftime("%Y-%m")


def _ensure_tables(con) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS tank01_usage ("
        "month TEXT NOT NULL PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS live_stats ("
        "player_id TEXT NOT NULL, season INTEGER NOT NULL, week INTEGER NOT NULL, "
        "tackles REAL NOT NULL DEFAULT 0, sacks REAL NOT NULL DEFAULT 0, "
        "interceptions REAL NOT NULL DEFAULT 0, forced_fumbles REAL NOT NULL DEFAULT 0, "
        "fumble_recoveries REAL NOT NULL DEFAULT 0, safeties REAL NOT NULL DEFAULT 0, "
        "def_tds REAL NOT NULL DEFAULT 0, "
        "game_id TEXT NOT NULL DEFAULT '', fetched_at TEXT NOT NULL DEFAULT '', "
        "PRIMARY KEY (player_id, season, week))"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS stat_corrections ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "player_id TEXT NOT NULL, season INTEGER NOT NULL, week INTEGER NOT NULL, "
        "stat_key TEXT NOT NULL, old_total REAL NOT NULL, new_total REAL NOT NULL, "
        "points_delta REAL NOT NULL, game_id TEXT NOT NULL DEFAULT '', "
        "detected_at TEXT NOT NULL)"
    )


def _calls_used(con, month: str) -> int:
    _ensure_tables(con)
    row = con.execute(
        "SELECT calls FROM tank01_usage WHERE month = ?", (month,)).fetchone()
    return row["calls"] if row else 0


def _record_calls(con, month: str, n: int) -> None:
    con.execute(
        "INSERT INTO tank01_usage (month, calls) VALUES (?, ?) "
        "ON CONFLICT (month) DO UPDATE SET calls = calls + ?",
        (month, n, n),
    )


def _live_games(con, now: datetime):
    """(season, week, game_id) for games live right now, from our schedule."""
    rows = con.execute(
        "SELECT season, week, away_team, home_team, kickoff_utc FROM games"
    ).fetchall()
    out = []
    for r in rows:
        try:
            ko = datetime.fromisoformat(r["kickoff_utc"])
        except (TypeError, ValueError):
            continue
        if ko.tzinfo is None:
            ko = ko.replace(tzinfo=timezone.utc)
        if ko <= now <= ko + LIVE_WINDOW:
            et_date = ko.astimezone(ET).strftime("%Y%m%d")
            game_id = f"{et_date}_{r['away_team']}@{r['home_team']}"
            out.append((r["season"], r["week"], game_id))
    return out


def _player_index(con):
    """normalized name -> [(player_id, team)]. Our players table is the
    authority; Tank01 names matched it 1:1 in the trial."""
    idx = {}
    for r in con.execute("SELECT player_id, name, team FROM players"):
        nm = _norm(r["name"])
        if nm:
            idx.setdefault(nm, []).append((r["player_id"], r["team"]))
    return idx


# Big-play stats whose drops get announced as corrections; a tackle-only
# drop moves a total by 0.1 and is not worth a correction notice.
CORRECTION_STATS = ("sacks", "interceptions", "forced_fumbles",
                    "fumble_recoveries", "safeties", "def_tds")


def _record_stat_changes(con, pid, season, week, stats, old, cfg,
                         game_id, fetched_at) -> None:
    """Scoring events for stat increases (existing behavior) plus a
    stat_corrections row for a big-play total that DROPPED — an overturn
    or stat-crew correction. Drops used to be silent: the live_stats
    upsert overwrote the total with no record, so a published window
    report kept the old numbers forever. Does not commit."""
    for c in STAT_COLS:
        new_v = stats.get(c, 0)
        old_v = (old[c] if old else 0) or 0
        if new_v > old_v + 1e-9:
            pts = round((new_v - old_v) * cfg.get(c, 0), 2)
            if pts > 0:
                con.execute(
                    "INSERT INTO scoring_events (player_id, season, week, stat_key, "
                    "old_total, new_total, points_delta, game_id, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT (player_id, season, week, stat_key, new_total) "
                    "DO NOTHING",
                    (pid, season, week, c, old_v, new_v, pts,
                     game_id, fetched_at),
                )
        elif new_v < old_v - 1e-9 and c in CORRECTION_STATS:
            pts = round((new_v - old_v) * cfg.get(c, 0), 2)
            con.execute(
                "INSERT INTO stat_corrections (player_id, season, week, stat_key, "
                "old_total, new_total, points_delta, game_id, detected_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, season, week, c, old_v, new_v, pts,
                 game_id, fetched_at),
            )


def _poll_games(con, games, key: str, now: datetime):
    """Fetch box scores for [(season, week, game_id)] and upsert live_stats.

    Shared by the 15-min live poller and the 1-min latency-test path.
    Returns (n_calls, n_players, n_skipped). Does not commit.
    """
    _ensure_tables(con)
    index = _player_index(con)
    cfg = {r["stat_key"]: r["points"]
           for r in con.execute("SELECT stat_key, points FROM scoring_config")}
    fetched_at = now.isoformat()
    n_calls, n_players, n_skipped = 0, 0, 0
    for season, week, game_id in games:
        try:
            data = _fetch_boxscore(game_id, key)
            n_calls += 1
        except Exception as e:
            print(f"tank01: {game_id} fetch failed: {e}")
            continue
        if data.get("error") and not data.get("playerStats"):
            print(f"tank01: {game_id}: {data['error']}")
            continue
        teams = set(game_id.split("_", 1)[1].split("@"))
        pstats = data.get("playerStats") or {}
        for p in pstats.values():
            if not isinstance(p, dict):
                continue
            d = p.get("Defense")
            if not d:
                continue
            nm = _norm(p.get("longName") or "")
            cands = index.get(nm, [])
            # Prefer a same-game-team match; fall back to any team on a
            # unique name (handles mid-week trades).
            pid = None
            in_game = [pid_ for pid_, tm in cands if tm in teams]
            if len(in_game) == 1:
                pid = in_game[0]
            elif not in_game and len(cands) == 1:
                pid = cands[0][0]
            if not pid:
                n_skipped += 1
                continue
            stats = {FIELD_MAP[k]: round(float(v), 2)
                     for k, v in d.items() if k in FIELD_MAP}
            # Scoring events: diff against the stored totals so each stat
            # increase becomes one "what just happened" notification. The
            # UNIQUE(new_total) constraint dedupes the rare same-second race
            # between the 1-min and 15-min pollers.
            old = con.execute(
                "SELECT * FROM live_stats WHERE player_id = ? AND season = ? AND week = ?",
                (pid, season, week),
            ).fetchone()
            _record_stat_changes(con, pid, season, week, stats, old, cfg,
                                 game_id, fetched_at)
            cols = ", ".join(STAT_COLS)
            vals = ", ".join(str(stats.get(c, 0)) for c in STAT_COLS)
            sets = ", ".join(f"{c}=excluded.{c}" for c in STAT_COLS)
            con.execute(
                f"INSERT INTO live_stats (player_id, season, week, {cols}, "
                f"game_id, fetched_at) VALUES (?, ?, ?, {vals}, ?, ?) "
                f"ON CONFLICT (player_id, season, week) DO UPDATE SET {sets}, "
                f"game_id=excluded.game_id, fetched_at=excluded.fetched_at",
                (pid, season, week, game_id, fetched_at),
            )
            n_players += 1
    return n_calls, n_players, n_skipped


def _budget_ok(con, month: str) -> bool:
    return _calls_used(con, month) < MONTHLY_BUDGET


def run_live(con) -> str:
    key = os.environ.get("TANK01_API_KEY", "").strip()
    if not key:
        return "tank01 live: no API key, skipping"
    now = datetime.now(timezone.utc)
    month = _month_key(now)
    if not _budget_ok(con, month):
        return f"tank01 live: monthly budget reached ({MONTHLY_BUDGET}), skipping"
    games = _live_games(con, now)
    if not games:
        return "tank01 live: no live games, skipping"
    n_calls, n_players, n_skipped = _poll_games(con, games, key, now)
    _record_calls(con, month, n_calls)
    con.commit()
    return (f"tank01 live: {n_calls} box-score call(s), {n_players} player "
            f"row(s) upserted ({n_skipped} unmatched)")
