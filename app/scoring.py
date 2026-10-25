"""Scoring engine. Points = raw weekly_stats x scoring_config, computed live.

Nothing here writes points to the database. Editing scoring_config (via the
admin page) recalculates every past week automatically.
"""
import json

STAT_KEYS = (
    "tackles",
    "sacks",
    "interceptions",
    "forced_fumbles",
    "fumble_recoveries",
    "safeties",
    "def_tds",
)

# Weekly picks per member. No draft, no locked players: every week each
# member picks this many fresh players from the open pool.
WEEKLY_PICKS = 5

# Live substitutions per member per week (Week 7 rule).
MAX_SUBS = 2


def scoring_map(con) -> dict:
    return {r["stat_key"]: r["points"] for r in con.execute("SELECT * FROM scoring_config")}


def player_week_points(con, player_id: str, season: int, week: int):
    """(points, {stat: value}) for one player-week. Missing stats row -> 0.

    Source precedence: nflverse weekly_stats wins whenever a final row exists;
    Tank01 live_stats fills the gap while games are in progress (or before
    the batch feed publishes). Everything downstream (ticker, standings,
    member pages) picks up live numbers automatically.
    """
    row = con.execute(
        "SELECT * FROM weekly_stats WHERE player_id = ? AND season = ? AND week = ?",
        (player_id, season, week),
    ).fetchone()
    if row is None:
        try:
            row = con.execute(
                "SELECT * FROM live_stats WHERE player_id = ? AND season = ? AND week = ?",
                (player_id, season, week),
            ).fetchone()
        except Exception:
            row = None  # live_stats table predates this deploy
    cfg = scoring_map(con)
    if not row:
        return 0.0, {k: 0 for k in STAT_KEYS}
    stats = {k: (row[k] or 0) for k in STAT_KEYS}
    pts = sum(stats[k] * cfg.get(k, 0) for k in STAT_KEYS)
    return round(pts, 2), stats


def week_subs(con, member_id: int, season: int, week: int):
    """The member's substitution rows for a week, in the order they were made."""
    try:
        return con.execute(
            "SELECT * FROM weekly_subs WHERE member_id = ? AND season = ? "
            "AND week = ? ORDER BY id", (member_id, season, week)).fetchall()
    except Exception:
        return []  # weekly_subs table predates this deploy


def _stats_points(stats: dict, cfg: dict) -> float:
    return sum((stats.get(k, 0) or 0) * cfg.get(k, 0) for k in STAT_KEYS)


def member_week_stints(con, member_id: int, season: int, week: int):
    """Stints for a member-week: the member's picks plus live subs.

    Each stint is a dict: {player_id, name, team, source ('pick'|'sub'),
    points, stats (the stint's own stat line), active, sub (the
    weekly_subs row that closed the stint, or None), opened_by (the
    weekly_subs row that opened it, or None)}.

    A stint scores (end_stats - start_stats) x scoring_config: start is 0
    for an original pick or the in_stats snapshot for a sub-in; end is the
    out_stats snapshot for a subbed-out player or the player's current
    cumulative stats while the stint is still active. So a subbed-out
    player keeps exactly what he had banked at sub time, and a
    replacement scores only from the moment he enters. Stints are
    returned in lineup order: each replacement directly follows the
    player he replaced.
    """
    cfg = scoring_map(con)
    picks = con.execute(
        "SELECT wp.player_id, p.name, p.team FROM weekly_picks wp "
        "JOIN players p ON p.player_id = wp.player_id "
        "WHERE wp.member_id = ? AND wp.season = ? AND wp.week = ?",
        (member_id, season, week),
    ).fetchall()
    subs = week_subs(con, member_id, season, week)

    by_pid, roots = {}, []
    for pk in picks:
        st = {"player_id": pk["player_id"], "name": pk["name"],
              "team": pk["team"], "source": "pick",
              "start": {k: 0 for k in STAT_KEYS}, "end": None,
              "active": True, "sub": None, "opened_by": None}
        by_pid[pk["player_id"]] = st
        roots.append(st)
    opened = {}  # sub row id -> stint it opened
    for s in subs:
        out = by_pid.get(s["out_player_id"])
        if out is not None and out["active"]:
            out["end"] = json.loads(s["out_stats"])
            out["active"] = False
            out["sub"] = s
        prow = con.execute(
            "SELECT name, team FROM players WHERE player_id = ?",
            (s["in_player_id"],)).fetchone()
        nst = {"player_id": s["in_player_id"],
               "name": prow["name"] if prow else s["in_player_id"],
               "team": prow["team"] if prow else "",
               "source": "sub", "start": json.loads(s["in_stats"]),
               "end": None, "active": True, "sub": None, "opened_by": s}
        by_pid[s["in_player_id"]] = nst
        opened[s["id"]] = nst

    ordered = []

    def _emit(st):
        ordered.append(st)
        if st["sub"] is not None:
            nxt = opened.get(st["sub"]["id"])
            if nxt is not None:
                _emit(nxt)

    for r in roots:
        _emit(r)

    for st in ordered:
        if st["end"] is not None:
            end = st["end"]
        else:
            _, end = player_week_points(con, st["player_id"], season, week)
        delta = {k: (end.get(k, 0) or 0) - (st["start"].get(k, 0) or 0)
                 for k in STAT_KEYS}
        st["stats"] = delta
        st["points"] = round(_stats_points(delta, cfg), 2)
    return ordered


def stint_counts_event(stint, stat_key: str, new_total: float) -> bool:
    """Whether a scoring event falls inside a stint's counted window.

    Events carry cumulative totals, so the stint's own snapshots are the
    bounds: a play counts only if it pushed the total past the entry
    snapshot and no further than the sub-out snapshot (open-ended while
    the stint is still active). This is the rule the scoring summary
    and the popup YOUR PICK badge share, so both agree with the points.
    """
    start = stint["start"].get(stat_key, 0) or 0
    end = stint["end"].get(stat_key, 0) if stint["end"] is not None else None
    return new_total > start and (end is None or new_total <= end)


def sub_outcomes(con, member_id: int, season: int, week: int):
    """Per-sub outcome lines for a member-week, in the order subs were made.

    Each: {subbed_at, game_label, out_name, in_name, banked, earned, net}.
    banked = points the subbed-out player kept (his closed stint);
    earned = points the replacement scored from the moment he entered
    (his stint); net = earned minus what the subbed-out player scored
    after the sub moment — positive means the sub gained points. While
    games are still in progress all three are "so far" numbers.
    """
    cfg = scoring_map(con)
    stints = member_week_stints(con, member_id, season, week)
    by_pid = {s["player_id"]: s for s in stints}
    out = []
    for s in week_subs(con, member_id, season, week):
        ost = by_pid.get(s["out_player_id"])
        ist = by_pid.get(s["in_player_id"])
        out_final, _ = player_week_points(con, s["out_player_id"],
                                          season, week)
        snap_pts = _stats_points(json.loads(s["out_stats"]), cfg)
        banked = ost["points"] if ost else round(snap_pts, 2)
        earned = ist["points"] if ist else 0.0
        out.append({
            "subbed_at": s["subbed_at"], "game_label": s["game_label"],
            "out_player_id": s["out_player_id"],
            "in_player_id": s["in_player_id"],
            "out_name": ost["name"] if ost else s["out_player_id"],
            "in_name": ist["name"] if ist else s["in_player_id"],
            "banked": banked, "earned": earned,
            "net": round(earned - (out_final - snap_pts), 2)})
    return out


def member_week_points(con, member_id: int, season: int, week: int):
    """(total, [(player_id, name, team, source, points, stats)]) for a member-week.

    No draft: every week each member picks WEEKLY_PICKS fresh players from
    the open pool. (Legacy locked_players rows are ignored.) Live subs
    (Week 7) turn picks into stints — see member_week_stints.
    """
    stints = member_week_stints(con, member_id, season, week)
    total = round(sum(s["points"] for s in stints), 2)
    lines = [(s["player_id"], s["name"], s["team"], s["source"],
              s["points"], s["stats"]) for s in stints]
    return total, lines


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


def status_chip(status):
    """Display chip for a player's roster status (players.status).

    Members should always be able to tell whether a player can actually
    play: returns {"label", "kind"} with kind ok | out | warn. Active
    (or unknown-but-rostered) players read Ready; reserve/inactive/PUP
    read as out; anything else gets a warning chip with a friendly label.
    """
    s = (status or "").upper()
    if s in ("", "ACT"):
        return {"label": "Ready", "kind": "ok"}
    if s == "RES":
        return {"label": "IR", "kind": "out"}
    if s == "INA":
        return {"label": "Inactive", "kind": "out"}
    if s == "PUP":
        return {"label": "PUP", "kind": "out"}
    labels = {"SUS": "Suspended", "DEV": "Practice squad",
              "NFI": "NFI · Out", "EXE": "Exempt"}
    return {"label": labels.get(s, s.title()), "kind": "warn"}


def projections(con, season: int, through_week: int):
    """Per-player projected weekly Prime5 points.

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
