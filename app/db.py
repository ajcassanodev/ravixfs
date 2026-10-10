"""SQLite helpers for ravixfs. Points are never stored; see scoring.py."""
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("RAVIXFS_DB", os.path.join(os.path.dirname(__file__), "..", "ravixfs.db"))
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "schema.sql")


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
    # Migrations for DBs created before a column existed (init_db is
    # re-run on every deploy, so this must stay idempotent).
    _migrate(con)
    con.commit()
    con.close()


def _migrate(con) -> None:
    cols = {r["name"] for r in con.execute("PRAGMA table_info(members)")}
    if "is_test" not in cols:
        con.execute("ALTER TABLE members ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0")
    if "phone" not in cols:
        con.execute("ALTER TABLE members ADD COLUMN phone TEXT")
    if "sms_opt_in" not in cols:
        con.execute("ALTER TABLE members ADD COLUMN sms_opt_in INTEGER NOT NULL DEFAULT 0")
    if "email" not in cols:
        con.execute("ALTER TABLE members ADD COLUMN email TEXT")
    if "email_opt_in" not in cols:
        con.execute("ALTER TABLE members ADD COLUMN email_opt_in INTEGER NOT NULL DEFAULT 0")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_season(con) -> int:
    row = con.execute("SELECT MAX(season) AS s FROM games").fetchone()
    return row["s"] if row and row["s"] else 2026


def current_pick_week(con, season: int) -> int:
    """Earliest week whose last kickoff is still in the future (+4h grace).

    During a game week this returns that week (late games still pickable);
    on Tue-Wed it rolls to the next week.
    """
    now = utcnow_iso()
    rows = con.execute(
        "SELECT week, MAX(kickoff_utc) AS last FROM games "
        "WHERE season = ? GROUP BY week ORDER BY week", (season,)
    ).fetchall()
    from datetime import timedelta
    for r in rows:
        last = datetime.fromisoformat(r["last"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last + timedelta(hours=4) > datetime.now(timezone.utc):
            return r["week"]
    return rows[-1]["week"] if rows else 1


def locked_player_ids(con, season: int) -> set:
    rows = con.execute(
        "SELECT player_id FROM locked_players WHERE season = ? AND active = 1",
        (season,),
    ).fetchall()
    return {r["player_id"] for r in rows}


def member_locked_player(con, member_id: int, season: int):
    return con.execute(
        "SELECT lp.*, p.name, p.team, p.position FROM locked_players lp "
        "JOIN players p ON p.player_id = lp.player_id "
        "WHERE lp.member_id = ? AND lp.season = ? AND lp.active = 1",
        (member_id, season),
    ).fetchone()


def player_kickoff(con, player_id: str, season: int, week: int):
    """Kickoff (UTC ISO) of the player's team game that week, or None on bye."""
    row = con.execute(
        "SELECT g.kickoff_utc FROM players p "
        "JOIN games g ON g.season = ? AND g.week = ? "
        "AND (g.home_team = p.team OR g.away_team = p.team) "
        "WHERE p.player_id = ?",
        (season, week, player_id),
    ).fetchone()
    return row["kickoff_utc"] if row else None
