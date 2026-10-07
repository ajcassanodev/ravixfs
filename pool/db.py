"""SQLite helpers for the Ravix football pool."""
import os
import sqlite3
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("POOL_DB", os.path.join(os.path.dirname(__file__), "..", "pool.db"))
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    con = connect()
    with open(SCHEMA_PATH) as f:
        con.executescript(f.read())
    con.commit()
    con.close()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def current_season(con) -> int:
    row = con.execute("SELECT MAX(season) AS s FROM pool_games").fetchone()
    return row["s"] if row and row["s"] else 2026


def current_pick_week(con, season: int) -> int:
    """Earliest week whose last kickoff is still in the future (+4h grace).

    The pick window for a week opens Tuesday (this rolls over on Tue-Wed);
    each game's picks lock individually at that game's kickoff.
    """
    rows = con.execute(
        "SELECT week, MAX(kickoff_utc) AS last FROM pool_games "
        "WHERE season = ? GROUP BY week ORDER BY week", (season,)
    ).fetchall()
    for r in rows:
        last = _as_utc(datetime.fromisoformat(r["last"]))
        if last + timedelta(hours=4) > datetime.now(timezone.utc):
            return r["week"]
    return rows[-1]["week"] if rows else 1


def game_winner(g) -> str | None:
    """Winning team abbrev for a final game, or None (not final / tie)."""
    if not g["is_final"]:
        return None
    a, h = g["away_score"], g["home_score"]
    if a is None or h is None:
        return None
    if a == h:
        return None  # tie: no winner, nobody scores (flagged for Adam)
    return g["home_team"] if h > a else g["away_team"]


def member_week_score(con, member_id: int, season: int, week: int) -> tuple[int, int]:
    """(correct, decided) for included games of the week."""
    games = con.execute(
        "SELECT * FROM pool_games WHERE season = ? AND week = ? AND included = 1",
        (season, week),
    ).fetchall()
    picks = {
        (r["away_team"], r["home_team"]): r["picked_team"]
        for r in con.execute(
            "SELECT away_team, home_team, picked_team FROM pool_picks "
            "WHERE member_id = ? AND season = ? AND week = ?",
            (member_id, season, week),
        ).fetchall()
    }
    correct = decided = 0
    for g in games:
        w = game_winner(g)
        if w is None:
            continue
        decided += 1
        if picks.get((g["away_team"], g["home_team"])) == w:
            correct += 1
    return correct, decided


def season_standings(con, season: int, include_test: bool = False):
    """[(member_id, name, points, is_test)] sorted by points desc, name."""
    from shared import identity as _identity
    s = _identity.connect_shared()
    if s is None:
        return []
    try:
        members = _identity.game_members(s, "pool", include_test=include_test)
        table = []
        for m in members:
            total = 0
            weeks = con.execute(
                "SELECT DISTINCT week FROM pool_games WHERE season = ?",
                (season,),
            ).fetchall()
            for wr in weeks:
                c, _ = member_week_score(con, m["id"], season, wr["week"])
                total += c
            table.append((m["id"], m["name"], total, bool(m["is_test"])))
    finally:
        s.close()
    table.sort(key=lambda t: (-t[2], t[1].lower()))
    return table
