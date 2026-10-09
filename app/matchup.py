"""Matchup intelligence: adjust defensive projections by opponent offense.

Data: poller aggregates per-team per-week offensive stats from nflverse
stats_player_week into the offense_stats table (ints_thrown, sacks_allowed,
fumbles_lost, plays).

Formula (kept simple and defensible):
  For each opponent-tendency stat we compute the opponent's per-game average
  over completed weeks and divide by the league average:

      factor = opp_avg / league_avg        (league_avg == 0 -> 1.0)

  Each factor is clamped to [0.7, 1.3] so one fluky week can't move a
  projection more than 30%. The clamp applies per component, so the total
  adjusted projection also stays within roughly +-30%.

  Component mapping (only components the player actually produces, i.e.
  per-game average > 0, can move):
    - tackles            x plays_factor      (all defenders; more snaps = more tackles)
    - sacks              x sacks_factor      (DL, LB only -- pass rushers)
    - interceptions      x ints_factor       (DB only)
    - forced_fumbles,
      fumble_recoveries  x fumbles_factor    (all defenders)
    - safeties, def_tds  unadjusted          (too rare to model)

  The adjusted projection = sum of adjusted components through the live
  scoring_config.

Badges: for each player we surface the single strongest applicable factor as
an explainable badge naming the opponent and the driving stat, e.g.
"great matchup vs NYJ (2.8 INTs thrown/gm)". Badges appear only when the
factor is at least 10% favorable (>= 1.10, "good") or 10% unfavorable
(<= 0.90, "tough"). Bye weeks (no opponent) get no adjustment and no badge.
"""

CLAMP_LO, CLAMP_HI = 0.7, 1.3
BADGE_GOOD = 1.10
BADGE_BAD = 0.90

# stat key -> (offense_stats column, display label, display unit)
TENDENCIES = {
    "ints": ("ints_thrown", "INTs thrown", "/gm"),
    "sacks": ("sacks_allowed", "sacks allowed", "/gm"),
    "fumbles": ("fumbles_lost", "fumbles lost", "/gm"),
    "plays": ("plays", "offensive plays", "/gm"),
}


def _clamp(f: float) -> float:
    return max(CLAMP_LO, min(CLAMP_HI, f))


def team_offense_avgs(con, season: int, through_week: int):
    """Per-team per-game averages + league averages from offense_stats.

    Returns (teams, league) where teams[team] = {ints, sacks, fumbles, plays}
    and league = the same shape averaged across teams. Empty dicts when no
    data is available yet.
    """
    # The poller creates this table on its next run; the app must not 500
    # if it hasn't been created yet.
    con.execute(
        "CREATE TABLE IF NOT EXISTS offense_stats ("
        "season INTEGER NOT NULL, week INTEGER NOT NULL, team TEXT NOT NULL, "
        "ints_thrown REAL NOT NULL DEFAULT 0, sacks_allowed REAL NOT NULL DEFAULT 0, "
        "fumbles_lost REAL NOT NULL DEFAULT 0, plays REAL NOT NULL DEFAULT 0, "
        "PRIMARY KEY (season, week, team))"
    )
    rows = con.execute(
        "SELECT team, AVG(ints_thrown), AVG(sacks_allowed), "
        "AVG(fumbles_lost), AVG(plays) FROM offense_stats "
        "WHERE season = ? AND week <= ? GROUP BY team",
        (season, through_week),
    ).fetchall()
    teams = {}
    for r in rows:
        if not r["team"]:
            continue
        teams[r["team"]] = {
            "ints": r[1] or 0.0, "sacks": r[2] or 0.0,
            "fumbles": r[3] or 0.0, "plays": r[4] or 0.0,
        }
    league = {}
    if teams:
        for k in ("ints", "sacks", "fumbles", "plays"):
            league[k] = sum(t[k] for t in teams.values()) / len(teams)
    return teams, league


def opponent_for(con, season: int, week: int, team: str):
    """Opponent abbrev for team's game that week, or None on bye/FA."""
    if not team or team == "FA":
        return None
    row = con.execute(
        "SELECT away_team, home_team FROM games "
        "WHERE season = ? AND week = ? AND (away_team = ? OR home_team = ?)",
        (season, week, team, team),
    ).fetchone()
    if not row:
        return None
    return row["home_team"] if row["away_team"] == team else row["away_team"]


def _factors(opp: dict, league: dict):
    """Clamped opponent-vs-league factors per tendency."""
    out = {}
    for k in TENDENCIES:
        lg = league.get(k, 0) or 0
        out[k] = _clamp(opp.get(k, 0) / lg) if lg > 0 else 1.0
    return out


def annotate_rows(con, season: int, pick_week: int, through_week: int, rows,
                  cfg: dict):
    """Add matchup-adjusted projection + badge to each projections row.

    Each row needs: player_id, team, position_group, avg (tkl/sacks/ints/ff/fr),
    proj_pts. Adds: adj_pts, badge (None or {"text", "kind"}), opponent.
    Mutates rows in place.
    """
    teams, league = team_offense_avgs(con, season, through_week)
    opp_cache = {}
    for r in rows:
        team = r.get("team") or ""
        if team not in opp_cache:
            opp_cache[team] = opponent_for(con, season, pick_week, team)
        opp = opp_cache[team]
        r["opponent"] = opp
        r["adj_pts"] = r["proj_pts"]
        r["badge"] = None
        if not opp or opp not in teams or not league:
            continue
        f = _factors(teams[opp], league)
        avg = r.get("avg", {})
        group = r.get("position_group", "")
        # component points from per-game averages through scoring config
        comp = {
            "tkl": avg.get("tkl", 0) * cfg.get("tackles", 0),
            "sacks": avg.get("sacks", 0) * cfg.get("sacks", 0),
            "ints": avg.get("ints", 0) * cfg.get("interceptions", 0),
            "ff": avg.get("ff", 0) * cfg.get("forced_fumbles", 0),
            "fr": avg.get("fr", 0) * cfg.get("fumble_recoveries", 0),
        }
        adj = 0.0
        drivers = []  # (factor, tendency_key, component_points)
        # tackles: every defender, scaled by opponent play volume
        if comp["tkl"] > 0:
            adj += comp["tkl"] * f["plays"]
            drivers.append((f["plays"], "plays", comp["tkl"]))
        else:
            adj += comp["tkl"]
        # sacks: pass rushers only
        if group in ("DL", "LB"):
            if comp["sacks"] > 0:
                adj += comp["sacks"] * f["sacks"]
                drivers.append((f["sacks"], "sacks", comp["sacks"]))
            else:
                adj += comp["sacks"]
        else:
            adj += comp["sacks"]
        # interceptions: defensive backs only
        if group == "DB":
            if comp["ints"] > 0:
                adj += comp["ints"] * f["ints"]
                drivers.append((f["ints"], "ints", comp["ints"]))
            else:
                adj += comp["ints"]
        else:
            adj += comp["ints"]
        # fumbles: every defender
        for k in ("ff", "fr"):
            if comp[k] > 0:
                adj += comp[k] * f["fumbles"]
                drivers.append((f["fumbles"], "fumbles", comp[k]))
            else:
                adj += comp[k]
        # safeties/def_tds: unadjusted (proj_pts already includes them)
        rest = r["proj_pts"] - sum(comp.values())
        adj += rest
        r["adj_pts"] = round(adj, 2)
        # badge: strongest applicable driver
        if drivers:
            drivers.sort(key=lambda d: abs(d[0] - 1.0), reverse=True)
            bf, bkey, _ = drivers[0]
            _, label, unit = TENDENCIES[bkey]
            val = teams[opp][bkey]
            if bf >= BADGE_GOOD:
                r["badge"] = {
                    "kind": "good",
                    "text": f"🎯 great matchup vs {opp} ({val:.1f} {label}{unit})",
                }
            elif bf <= BADGE_BAD:
                r["badge"] = {
                    "kind": "bad",
                    "text": f"📉 tough matchup vs {opp} ({val:.1f} {label}{unit})",
                }
    return rows
