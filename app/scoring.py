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
    """([(member_id, name, total)], [(member_id, name, total)]) sorted desc.

    Returns (real_table, test_table): test members (is_test=1) are excluded
    from the real standings and returned separately so the homepage can show
    them in a small "excluded" section.
    """
    members = con.execute(
        "SELECT id, name, COALESCE(is_test, 0) AS is_test FROM members ORDER BY name"
    ).fetchall()
    if through_week is None:
        wrow = con.execute(
            "SELECT MAX(week) AS w FROM weekly_stats WHERE season = ?", (season,)
        ).fetchone()
        through_week = wrow["w"] or 0
    table, test_table = [], []
    for m in members:
        total = 0.0
        for week in range(1, through_week + 1):
            pts, _ = member_week_points(con, m["id"], season, week)
            total += pts
        (test_table if m["is_test"] else table).append(
            (m["id"], m["name"], round(total, 2)))
    table.sort(key=lambda r: r[2], reverse=True)
    test_table.sort(key=lambda r: r[2], reverse=True)
    return table, test_table


def projections(con, season: int, through_week: int):
    """Per-player projected weekly Pick5 points.

    Per-game averages over completed weeks (1..through_week) from
    weekly_stats, run through the live scoring_config. Returns a list of
    dicts; each has per-game averages, season totals, and proj_pts.
    Only defensive players (DB/DL/LB) with at least one game are included.
    """
    cfg = scoring_map(con)
    rows = con.execute(
        """
        SELECT ws.player_id, p.name, p.team, p.position, p.position_group,
               p.status AS pstatus,
               COUNT(*) AS gp,
               AVG(ws.tackles) AS tkl, AVG(ws.sacks) AS sacks,
               AVG(ws.interceptions) AS ints, AVG(ws.forced_fumbles) AS ff,
               AVG(ws.fumble_recoveries) AS fr, AVG(ws.safeties) AS saf,
               AVG(ws.def_tds) AS tds,
               SUM(ws.tackles) AS tkl_t, SUM(ws.sacks) AS sacks_t,
               SUM(ws.interceptions) AS ints_t, SUM(ws.forced_fumbles) AS ff_t,
               SUM(ws.fumble_recoveries) AS fr_t, SUM(ws.safeties) AS saf_t,
               SUM(ws.def_tds) AS tds_t
        FROM weekly_stats ws
        JOIN players p ON p.player_id = ws.player_id
        WHERE ws.season = ? AND ws.week <= ?
          AND p.position_group IN ('DB', 'DL', 'LB')
        GROUP BY ws.player_id
        """,
        (season, through_week),
    ).fetchall()

    out = []
    for r in rows:
        avg = {k: (r[k] or 0) for k in
               ("tkl", "sacks", "ints", "ff", "fr", "saf", "tds")}
        tot = {k: (r[k + "_t"] or 0) for k in
               ("tkl", "sacks", "ints", "ff", "fr", "saf", "tds")}
        proj = (avg["tkl"] * cfg.get("tackles", 0)
                + avg["sacks"] * cfg.get("sacks", 0)
                + avg["ints"] * cfg.get("interceptions", 0)
                + avg["ff"] * cfg.get("forced_fumbles", 0)
                + avg["fr"] * cfg.get("fumble_recoveries", 0)
                + avg["saf"] * cfg.get("safeties", 0)
                + avg["tds"] * cfg.get("def_tds", 0))
        season_pts = (tot["tkl"] * cfg.get("tackles", 0)
                      + tot["sacks"] * cfg.get("sacks", 0)
                      + tot["ints"] * cfg.get("interceptions", 0)
                      + tot["ff"] * cfg.get("forced_fumbles", 0)
                      + tot["fr"] * cfg.get("fumble_recoveries", 0)
                      + tot["saf"] * cfg.get("safeties", 0)
                      + tot["tds"] * cfg.get("def_tds", 0))
        out.append({
            "player_id": r["player_id"], "name": r["name"],
            "team": r["team"] or "FA", "position": r["position"] or "",
            "position_group": r["position_group"] or "",
            "status": r["pstatus"] or "",
            "gp": r["gp"], "avg": avg, "proj_pts": round(proj, 2),
            "season_pts": round(season_pts, 2),
        })
    out.sort(key=lambda d: d["proj_pts"], reverse=True)
    return out
