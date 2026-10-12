"""Tank01 trial comparison: Tank01 snapshots vs nflverse weekly_stats.

Reads tank01_snapshots for the trial game and compares per-player defensive
stats against the nflverse-sourced weekly_stats already in the DB.

Usage (on the server):  ./venv/bin/python poller/tank01_compare.py
Prints a per-player freshness + accuracy report for the trial game.
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(__file__))
from tank01_trial import GAME_ID, FIELD_MAP  # noqa: E402

DB_PATH = os.environ.get("RAVIXFS_DB", "/opt/ravixfs/ravixfs.db")
ET = ZoneInfo("America/New_York")
TRIAL_TEAMS = ("BUF", "LAR")
SEASON, WEEK = 2026, 5


def _norm(name: str) -> str:
    return re.sub(r"[^a-z ]", "", (name or "").lower()).strip()


def _points(stats: dict, scoring: dict) -> float:
    return round(sum(stats.get(k, 0) * scoring.get(k, 0) for k in stats), 2)


def main() -> None:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    scoring = {r["stat_key"]: r["points"]
               for r in con.execute("SELECT stat_key, points FROM scoring_config")}

    # ---- Tank01 side: first-seen time + final stats per player ----
    snaps = con.execute(
        "SELECT fetched_at, payload FROM tank01_snapshots "
        "WHERE game_id = ? ORDER BY fetched_at",
        (GAME_ID,),
    ).fetchall()
    print(f"snapshots: {len(snaps)}", end="")
    if snaps:
        print(f"  ({snaps[0]['fetched_at']} -> {snaps[-1]['fetched_at']})")
    else:
        print("  (no snapshots yet)")
        return

    first_seen, final_stats, names = {}, {}, {}
    for s in snaps:
        try:
            body = json.loads(s["payload"])
            body = body.get("body") if isinstance(body.get("body"), dict) else body
            pstats = body.get("playerStats") or {}
        except Exception:
            continue
        for pid, p in pstats.items():
            if not isinstance(p, dict):
                continue
            d = p.get("Defense")
            if not d:
                continue
            nm = _norm(p.get("longName") or p.get("name") or "")
            if not nm:
                continue
            names[nm] = p.get("longName") or nm
            stats = {FIELD_MAP[k]: round(float(v), 2)
                     for k, v in d.items() if k in FIELD_MAP}
            if any(v for v in stats.values()) and nm not in first_seen:
                first_seen[nm] = s["fetched_at"]
            final_stats[nm] = stats

    # ---- nflverse side: weekly_stats for the trial teams ----
    nv = {}
    for r in con.execute(
        "SELECT p.name, w.tackles, w.sacks, w.interceptions, w.forced_fumbles,"
        " w.fumble_recoveries, w.safeties, w.def_tds FROM weekly_stats w "
        "JOIN players p ON p.player_id = w.player_id "
        "WHERE w.season = ? AND w.week = ? AND p.team IN (?, ?)",
        (SEASON, WEEK, *TRIAL_TEAMS),
    ):
        nm = _norm(r["name"])
        nv[nm] = {
            "name": r["name"],
            "tackles": r["tackles"] or 0, "sacks": r["sacks"] or 0,
            "interceptions": r["interceptions"] or 0,
            "forced_fumbles": r["forced_fumbles"] or 0,
            "fumble_recoveries": r["fumble_recoveries"] or 0,
            "safeties": r["safeties"] or 0, "def_tds": r["def_tds"] or 0,
        }

    # ---- report ----
    print(f"\n{'player':28} {'tank01 1st seen (ET)':22} {'tank01':>7} {'nflverse':>8}  match")
    mism = 0
    for nm in sorted(set(final_stats) | set(nv)):
        t_stats = final_stats.get(nm, {})
        n_stats = nv.get(nm)
        t_pts = _points(t_stats, scoring)
        n_pts = _points({k: n_stats[k] for k in t_stats} if n_stats else {}, scoring) \
            if n_stats else None
        seen = first_seen.get(nm, "")
        if seen:
            try:
                dt = datetime.fromisoformat(seen)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                seen = dt.astimezone(ET).strftime("%-I:%M %p")
            except Exception:
                pass
        label = names.get(nm) or (n_stats["name"] if n_stats else nm)
        if n_stats is None:
            row_match = "nflverse MISSING"
            mism += 1
        elif abs(t_pts - n_pts) < 0.005:
            row_match = "yes"
        else:
            row_match = "NO"
            mism += 1
        nflv = f"{n_pts:.1f}" if n_pts is not None else "  --"
        print(f"{label[:28]:28} {seen or '--':22} {t_pts:7.1f} {nflv:>8}  {row_match}")
    only_nv = sorted(set(nv) - set(final_stats))
    if only_nv:
        print(f"\nin nflverse but never in Tank01: "
              + ", ".join(nv[n]['name'] for n in only_nv))
    print(f"\nmismatches: {mism}")
    con.close()


if __name__ == "__main__":
    main()
