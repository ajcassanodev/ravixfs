"""Unified audit log: every point change carries a cause.

One append-only table, audit_events. Writers are best-effort: a logging
failure must never break the member flow or the poller (same precedent as
sub_events). This module never commits — callers own their transactions,
except record_locks, which commits its own inserts as a standalone
poller section.

Design notes (from the Week 7 lesson — the 2.7-vs-2.4 mismatch happened
because two feeds produced two numbers and no row said which feed
produced which):
- Every stat movement records its feed (nflverse | tank01) and both
  totals, including re-climbs and tackle-only drops. The legacy tables
  keep their existing behavior (scoring_events dedupes re-climbs,
  stat_corrections covers big plays only); the audit log is the fuller
  record and never skips.
- Dual pollers (15-min + 1-min Tank01) can observe the same movement
  twice, so stat logging dedupes on the last logged value per
  (player, week, stat): a movement is logged only when it differs from
  the most recent logged value. Drops then re-climbs still log, because
  the drop changed the last value in between.
- Rows are never updated or deleted. A correction is a new row.
"""

import json
from datetime import datetime, timezone

TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS audit_events ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "season INTEGER NOT NULL, week INTEGER NOT NULL, "
    "member_id INTEGER, player_id TEXT, "
    "kind TEXT NOT NULL, cause TEXT NOT NULL DEFAULT '', "
    "stat_key TEXT, old_value REAL, new_value REAL, "
    "points_delta REAL NOT NULL DEFAULT 0, "
    "source_feed TEXT, actor TEXT, detail TEXT NOT NULL DEFAULT '{}', "
    "created_at TEXT NOT NULL)"
)

# Score-affecting kinds, for the admin tripwire's vocabulary.
STAT_KINDS = ("stat_observed", "stat_corrected", "feed_switched")

STAT_NOUNS = {
    "tackles": "tackles",
    "sacks": "sacks",
    "interceptions": "interceptions",
    "forced_fumbles": "forced fumbles",
    "fumble_recoveries": "fumble recoveries",
    "safeties": "safeties",
    "def_tds": "defensive TDs",
}


def ensure_table(con) -> None:
    con.execute(TABLE_SQL)


def log(con, season: int, week: int, kind: str, cause: str,
        member_id=None, player_id=None, stat_key=None,
        old_value=None, new_value=None, points_delta=None,
        source_feed=None, actor=None, detail=None, created_at=None) -> bool:
    """Append one audit row. Best-effort: never raises, never commits.

    Returns True when the row was written.
    """
    try:
        ensure_table(con)
        con.execute(
            "INSERT INTO audit_events (season, week, member_id, player_id, "
            "kind, cause, stat_key, old_value, new_value, points_delta, "
            "source_feed, actor, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (season, week, member_id, player_id, kind, cause or "",
             stat_key, old_value, new_value,
             round(points_delta, 2) if points_delta is not None else 0,
             source_feed, actor,
             json.dumps(detail or {}),
             created_at or datetime.now(timezone.utc).isoformat()),
        )
        return True
    except Exception:
        return False


def last_stat_value(con, player_id: str, season: int, week: int,
                    stat_key: str):
    """new_value of the most recent stat-kind audit row, or None.

    Used to dedupe dual-poller observations: only a movement that
    differs from the last logged value is worth a new row.
    """
    try:
        row = con.execute(
            "SELECT new_value FROM audit_events WHERE player_id = ? "
            "AND season = ? AND week = ? AND stat_key = ? "
            "AND kind IN ('stat_observed','stat_corrected','feed_switched') "
            "ORDER BY id DESC LIMIT 1",
            (player_id, season, week, stat_key)).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def log_stat_change(con, season: int, week: int, kind: str, player_id: str,
                    stat_key: str, old_value, new_value, points_delta,
                    source_feed: str, cause: str, detail=None,
                    created_at=None) -> bool:
    """Log a stat movement unless the last logged value already equals
    new_value (same observation seen twice). Returns True if written."""
    try:
        last = last_stat_value(con, player_id, season, week, stat_key)
        if last is not None and abs(last - new_value) < 1e-9:
            return False
        return log(con, season, week, kind, cause, player_id=player_id,
                   stat_key=stat_key, old_value=old_value,
                   new_value=new_value, points_delta=points_delta,
                   source_feed=source_feed, actor="poller", detail=detail,
                   created_at=created_at)
    except Exception:
        return False


def _fmt_num(v) -> str:
    if v is None:
        return "–"
    f = float(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _fmt_pts(pts) -> str:
    try:
        p = round(float(pts or 0), 1)
    except (TypeError, ValueError):
        p = 0.0
    if p > 0:
        return f"+{p:.1f}"
    if p < 0:
        return f"−{abs(p):.1f}"
    return "0.0"


def format_event(ev, names=None):
    """Plain-language line for one audit row.

    ev: dict-like row (sqlite Row or dict). names: optional
    {player_id: name} for resolving detail references (re-pick outs).
    Returns {"text", "points", "tag"} where tag is one of
    LIVE | FINAL | CORRECTION | SUB | PICK | LOCK | AUTO | ADMIN.
    Pure function — unit-tested.
    """
    try:
        ev = dict(ev)
    except (TypeError, ValueError):
        pass
    names = names or {}
    kind = ev.get("kind", "")
    pid = ev.get("player_id") or ""
    name = names.get(pid, pid) if pid else ""
    detail = ev.get("detail") or {}
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except (TypeError, ValueError):
            detail = {}
    pts = _fmt_pts(ev.get("points_delta"))
    feed = ev.get("source_feed") or ""
    stat = STAT_NOUNS.get(ev.get("stat_key") or "", ev.get("stat_key") or "")
    old, new = _fmt_num(ev.get("old_value")), _fmt_num(ev.get("new_value"))

    if kind == "stat_observed":
        tag = "LIVE" if feed == "tank01" else "FINAL"
        return {"text": f"{name} · {stat} {old} → {new}", "points": pts,
                "tag": tag}
    if kind == "stat_corrected":
        return {"text": f"{name} · {stat} corrected {old} → {new}",
                "points": pts, "tag": "CORRECTION"}
    if kind == "feed_switched":
        return {"text": f"{name} · {stat} {old} → {new} "
                        f"(final overrode live)",
                "points": pts, "tag": "FINAL"}
    if kind == "pick_submitted":
        method = detail.get("cpu_method")
        how = f" (CPU {method})" if method else ""
        return {"text": f"Picked {name}{how}", "points": "0.0",
                "tag": "PICK"}
    if kind == "pick_removed":
        return {"text": f"Removed {name}", "points": "0.0", "tag": "PICK"}
    if kind == "pick_auto_filled":
        method = detail.get("method", "")
        return {"text": f"Auto-filled {name}" + (f" ({method})" if method else ""),
                "points": "0.0", "tag": "AUTO"}
    if kind == "pick_locked":
        return {"text": f"{name} locked at kickoff", "points": "0.0",
                "tag": "LOCK"}
    if kind == "pick_subbed_out":
        inn = names.get(detail.get("in_player_id") or "", "")
        return {"text": f"Sub out: {name}" + (f" → {inn}" if inn else ""),
                "points": "0.0", "tag": "SUB"}
    if kind == "pick_subbed":
        out = names.get(detail.get("out_player_id") or "", "")
        return {"text": f"Sub in: {name}" + (f" for {out}" if out else ""),
                "points": "0.0", "tag": "SUB"}
    if kind == "repick":
        out = names.get(detail.get("out_player_id") or "", "")
        edge = detail.get("edge")
        extra = f" (edge {edge:.1f})" if isinstance(edge, (int, float)) else ""
        return {"text": f"Smart re-pick: {out} → {name}{extra}",
                "points": "0.0", "tag": "SUB"}
    if kind == "admin_action":
        action = detail.get("action", ev.get("cause") or "admin")
        if action == "scoring_change" and detail.get("stat_key") is not None:
            return {"text": f"Scoring change: {detail['stat_key']} = "
                            f"{detail.get('points')} pts",
                    "points": "0.0", "tag": "ADMIN"}
        return {"text": f"Admin: {action}", "points": "0.0", "tag": "ADMIN"}
    return {"text": f"{name} · {kind}", "points": pts, "tag": "PICK"}


def record_locks(con) -> int:
    """Write pick_locked rows for picks whose kickoff has passed.

    Runs in the poller every cycle; idempotent (a pick gets exactly one
    lock row). Bye-week picks have no game row and never lock. Commits
    its own inserts. Returns the number of new rows.
    """
    ensure_table(con)
    now = datetime.now(timezone.utc)
    rows = con.execute(
        "SELECT wp.member_id, wp.player_id, wp.season, wp.week, "
        "g.kickoff_utc FROM weekly_picks wp "
        "JOIN players p ON p.player_id = wp.player_id "
        "JOIN games g ON g.season = wp.season AND g.week = wp.week "
        "AND (g.home_team = p.team OR g.away_team = p.team)").fetchall()
    n = 0
    for r in rows:
        ko_raw = r["kickoff_utc"]
        if not ko_raw:
            continue
        try:
            ko = datetime.fromisoformat(ko_raw)
        except ValueError:
            continue
        if ko.tzinfo is None:
            ko = ko.replace(tzinfo=timezone.utc)
        if ko > now:
            continue
        hit = con.execute(
            "SELECT 1 FROM audit_events WHERE kind = 'pick_locked' "
            "AND member_id = ? AND player_id = ? AND season = ? AND week = ?",
            (r["member_id"], r["player_id"], r["season"], r["week"])
        ).fetchone()
        if hit:
            continue
        if log(con, r["season"], r["week"], "pick_locked", "kickoff",
               member_id=r["member_id"], player_id=r["player_id"],
               source_feed="system", actor="poller",
               detail={"kickoff": ko_raw}):
            n += 1
    if n:
        con.commit()
    return n


def tripwire_orphans(con, limit: int = 50):
    """Score writes since the audit log began that have no audit row.

    The tripwire: every score-affecting write should trace to a feed,
    pick, or sub through audit_events. (started, orphans): started is
    False before the first audit row exists, so pre-deploy history is
    never flagged. Each orphan: {source, member_id, player_id, season,
    week, stat_key, value, when}.
    """
    try:
        era = con.execute(
            "SELECT MIN(created_at) FROM audit_events").fetchone()[0]
    except Exception:
        era = None
    if not era:
        return False, []
    orphans = []

    def _has_audit(kind, player_id, season, week, stat_key, value,
                   member_id=None, in_pid=None):
        if kind in STAT_KINDS:
            row = con.execute(
                "SELECT 1 FROM audit_events WHERE kind = ? AND player_id = ? "
                "AND season = ? AND week = ? AND stat_key = ? "
                "AND ABS(new_value - ?) < 0.000000001 LIMIT 1",
                (kind, player_id, season, week, stat_key, value)).fetchone()
            return row is not None
        row = con.execute(
            "SELECT 1 FROM audit_events WHERE kind = 'pick_subbed' "
            "AND member_id = ? AND season = ? AND week = ? "
            "AND player_id = ? LIMIT 1",
            (member_id, season, week, in_pid)).fetchone()
        return row is not None

    try:
        for r in con.execute(
                "SELECT player_id, season, week, stat_key, new_total, "
                "created_at FROM scoring_events WHERE created_at >= ? "
                "ORDER BY created_at DESC LIMIT ?",
                (era, limit)):
            if not _has_audit("stat_observed", r["player_id"], r["season"],
                              r["week"], r["stat_key"], r["new_total"]):
                orphans.append({"source": "scoring_events",
                                "player_id": r["player_id"],
                                "season": r["season"], "week": r["week"],
                                "stat_key": r["stat_key"],
                                "value": r["new_total"],
                                "when": r["created_at"], "member_id": None})
    except Exception:
        pass
    try:
        for r in con.execute(
                "SELECT player_id, season, week, stat_key, new_total, "
                "detected_at FROM stat_corrections WHERE detected_at >= ? "
                "ORDER BY detected_at DESC LIMIT ?",
                (era, limit)):
            if not _has_audit("stat_corrected", r["player_id"], r["season"],
                              r["week"], r["stat_key"], r["new_total"]):
                orphans.append({"source": "stat_corrections",
                                "player_id": r["player_id"],
                                "season": r["season"], "week": r["week"],
                                "stat_key": r["stat_key"],
                                "value": r["new_total"],
                                "when": r["detected_at"], "member_id": None})
    except Exception:
        pass
    try:
        for r in con.execute(
                "SELECT member_id, season, week, in_player_id, subbed_at "
                "FROM weekly_subs WHERE subbed_at >= ? "
                "ORDER BY subbed_at DESC LIMIT ?",
                (era, limit)):
            if not _has_audit("pick_subbed", None, r["season"], r["week"],
                              None, None, member_id=r["member_id"],
                              in_pid=r["in_player_id"]):
                orphans.append({"source": "weekly_subs",
                                "player_id": r["in_player_id"],
                                "season": r["season"], "week": r["week"],
                                "stat_key": None, "value": None,
                                "when": r["subbed_at"],
                                "member_id": r["member_id"]})
    except Exception:
        pass
    return True, orphans[:limit]
