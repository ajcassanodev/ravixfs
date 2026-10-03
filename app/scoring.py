"""Scoring engine. Points = raw weekly_stats x scoring_config, computed live.

Nothing here writes points to the database. Editing scoring_config (via the
admin page) recalculates every past week automatically.
"""

STAT_KEYS = (
    "tackles",
    "sacks",
    "interceptions",
    "forced_fumbles",
    "fumble_recoveries",
    "safeties",
    "def_tds",
)


def scoring_map(con) -> dict:
    return {r["stat_key"]: r["points"] for r in con.execute("SELECT * FROM scoring_config")}


def player_week_points(con, player_id: str, season: int, week: int):
    """(points, {stat: value}) for one player-week. Missing stats row -> 0."""
    row = con.execute(
        "SELECT * FROM weekly_stats WHERE player_id = ? AND season = ? AND week = ?",
        (player_id, season, week),
    ).fetchone()
    cfg = scoring_map(con)
    if not row:
        return 0.0, {k: 0 for k in STAT_KEYS}
    stats = {k: (row[k] or 0) for k in STAT_KEYS}
    pts = sum(stats[k] * cfg.get(k, 0) for k in STAT_KEYS)
    return round(pts, 2), stats


def member_week_points(con, member_id: int, season: int, week: int):
    """(total, [(player_id, name, team, source, points, stats)]) for a member-week."""
    from . import db as _db
    total = 0.0
    lines = []
    locked = _db.member_locked_player(con, member_id, season)
    if locked:
        pts, stats = player_week_points(con, locked["player_id"], season, week)
        total += pts
        lines.append((locked["player_id"], locked["name"], locked["team"],
                      "locked", pts, stats))
    picks = con.execute(
        "SELECT wp.player_id, p.name, p.team FROM weekly_picks wp "
        "JOIN players p ON p.player_id = wp.player_id "
        "WHERE wp.member_id = ? AND wp.season = ? AND wp.week = ?",
        (member_id, season, week),
    ).fetchall()
    for pk in picks:
        pts, stats = player_week_points(con, pk["player_id"], season, week)
        total += pts
        lines.append((pk["player_id"], pk["name"], pk["team"], "pick", pts, stats))
    return round(total, 2), lines


def standings(con, season: int, through_week: int | None = None):
    """[(member_id, name, total)] sorted desc. Sums every completed week."""
    members = con.execute("SELECT id, name FROM members ORDER BY name").fetchall()
    if through_week is None:
        wrow = con.execute(
            "SELECT MAX(week) AS w FROM weekly_stats WHERE season = ?", (season,)
        ).fetchone()
        through_week = wrow["w"] or 0
    table = []
    for m in members:
        total = 0.0
        for week in range(1, through_week + 1):
            pts, _ = member_week_points(con, m["id"], season, week)
            total += pts
        table.append((m["id"], m["name"], round(total, 2)))
    table.sort(key=lambda r: r[2], reverse=True)
    return table
