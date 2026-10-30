"""Weekly audit digest: one email per closed week to the Ravix inbox.

Rides the poller's 15-minute timer: inside the 9:00-9:15 AM ET Tuesday
window (the week closed Monday night), the poller sends a digest of
that week's audit_events to info@ravixfs.com and records the send, so
each week reports exactly once. High-volume kinds (stat observations,
pick locks) appear as counts only; corrections, feed switches, subs,
re-picks, and admin actions get full lines. Never breaks the poll.
"""
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from . import audit as _audit
from . import db as _db
from . import emailer as _emailer

RECIPIENT = "info@ravixfs.com"
WINDOW_START = dtime(9, 0)
WINDOW_END = dtime(9, 15)
ET = ZoneInfo("America/New_York")

# High-volume kinds: counts only, no per-row lines.
COUNT_ONLY = ("stat_observed", "pick_locked")


def tuesday_report_due(now_et: datetime) -> bool:
    """True between 9:00 and 9:15 AM Eastern on Tuesdays."""
    return (
        now_et.weekday() == 1  # Monday=0, Tuesday=1
        and WINDOW_START <= now_et.time() < WINDOW_END
    )


def _et_stamp(iso_utc: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET).strftime("%a %-m/%-d %-I:%M %p")
    except Exception:
        return iso_utc or ""


def build_report(con, season: int, week: int) -> tuple[str, str]:
    """(subject, plain-text body) for one week's audit digest."""
    try:
        rows = con.execute(
            "SELECT * FROM audit_events WHERE season = ? AND week = ? "
            "ORDER BY created_at, id",
            (season, week)).fetchall()
    except Exception:
        rows = []
    try:
        names = {r["player_id"]: r["name"] for r in con.execute(
            "SELECT player_id, name FROM players")}
        member_names = {r["id"]: r["name"] for r in con.execute(
            "SELECT id, name FROM members")}
    except Exception:
        names, member_names = {}, {}
    counts: dict[str, int] = {}
    lines: dict[str, list[str]] = {
        "corrections": [], "subs": [], "admin": [], "switches": []}
    for r in rows:
        d = dict(r)
        counts[d["kind"]] = counts.get(d["kind"], 0) + 1
        if d["kind"] in COUNT_ONLY:
            continue
        f = _audit.format_event(d, names)
        who = member_names.get(d["member_id"], "League") \
            if d["member_id"] else "League"
        line = f"{_et_stamp(d['created_at'])} · {who} · {f['text']}"
        if f["points"] not in ("0.0", ""):
            line += f" ({f['points']} pts)"
        if d["kind"] == "stat_corrected":
            lines["corrections"].append(line)
        elif d["kind"] == "feed_switched":
            lines["switches"].append(line)
        elif d["kind"] in ("pick_subbed_out", "pick_subbed", "repick"):
            lines["subs"].append(line)
        elif d["kind"] == "admin_action":
            lines["admin"].append(line)
    try:
        started, orphans = _audit.tripwire_orphans(con)
        week_orphans = [o for o in orphans if o.get("week") == week]
        if not started:
            tripwire = "Tripwire: not armed yet (audit log has no rows to check)."
        elif not week_orphans:
            tripwire = "Tripwire: clean — every score write this week has a cause."
        else:
            tripwire = (f"Tripwire: {len(week_orphans)} score write(s) this week "
                        f"have no audit cause — investigate before trusting them.")
    except Exception:
        tripwire = "Tripwire: check unavailable this run."
    subject = f"Ravix Prime5: Week {week} audit report"
    body = [f"RAVIX PRIME5 — WEEK {week} AUDIT REPORT",
            f"Season {season} · {len(rows)} audit events recorded",
            "",
            tripwire, ""]
    body.append("COUNTS BY KIND")
    for kind in sorted(counts):
        body.append(f"  {kind}: {counts[kind]}")
    body.append("")
    for title, key in [("CORRECTIONS", "corrections"),
                       ("FEED SWITCHES (final overrode live)", "switches"),
                       ("SUBSTITUTIONS + RE-PICKS", "subs"),
                       ("ADMIN ACTIONS", "admin")]:
        body.append(title)
        if lines[key]:
            body.extend(f"  {ln}" for ln in lines[key])
        else:
            body.append("  (none)")
        body.append("")
    body.append("Full event log: Admin console → Audit log.")
    return subject, "\n".join(body)


def run_weekly_report(con, season: int, now_et: datetime) -> str:
    """Send Tuesday's digest for the just-closed week. Status string."""
    if not tuesday_report_due(now_et):
        return "weekly audit report: not due"
    week = _db.current_pick_week(con, season) - 1
    if week < 1:
        return "weekly audit report: no closed week yet"
    con.execute(
        "CREATE TABLE IF NOT EXISTS audit_report_sends ("
        "season INTEGER NOT NULL, week INTEGER NOT NULL, "
        "sent_at TEXT NOT NULL, PRIMARY KEY (season, week))")
    already = con.execute(
        "SELECT 1 FROM audit_report_sends WHERE season = ? AND week = ?",
        (season, week)).fetchone()
    if already:
        return f"weekly audit report: week {week} already sent"
    subject, body = build_report(con, season, week)
    if not _emailer.send_email(RECIPIENT, subject, body):
        return f"weekly audit report: send failed (week {week})"
    con.execute(
        "INSERT INTO audit_report_sends VALUES (?, ?, ?)",
        (season, week, _db.utcnow_iso()))
    con.commit()
    return f"weekly audit report sent: week {week} to {RECIPIENT}"
