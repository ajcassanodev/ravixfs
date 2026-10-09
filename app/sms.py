"""SMS pick reminders via Twilio.

Credentials come from the environment:
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER

When any are missing, sending is a no-op that logs a warning -- it never
raises, so the poller and the app keep working without Twilio configured.

Thursday-morning reminders are driven by run_pick_reminders(), which the
poller calls once per 15-minute run: it only fires inside the 9:00-9:15 AM
ET window on Thursdays, only for members with sms_opt_in=1 and a phone
number, only when they have fewer than 4 picks for the current pick week,
and records each send in sms_reminders so nobody gets two in one week.
"""
import logging
import os
import re
from datetime import datetime, time, timedelta, timezone

log = logging.getLogger("ravixfs.sms")

WINDOW_START = time(9, 0)
WINDOW_END = time(9, 15)
PICKS_NEEDED = 4


def normalize_phone(raw) -> str | None:
    """US numbers -> E.164 (+1XXXXXXXXXX). Returns None when unusable."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def _creds():
    return (
        os.environ.get("TWILIO_ACCOUNT_SID", ""),
        os.environ.get("TWILIO_AUTH_TOKEN", ""),
        os.environ.get("TWILIO_FROM_NUMBER", ""),
    )


def send_sms(to: str, body: str) -> bool:
    """Send one SMS via Twilio. Returns True on success.

    Never raises: returns False when unconfigured or when the send fails.
    """
    sid, auth, frm = _creds()
    if not (sid and auth and frm):
        log.warning("SMS not configured (missing TWILIO_* env); skipping send to %s", to)
        return False
    try:
        import requests
    except ImportError:
        log.warning("requests not installed; cannot send SMS to %s", to)
        return False
    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            auth=(sid, auth),
            data={"From": frm, "To": to, "Body": body},
            timeout=15,
        )
        resp.raise_for_status()
        log.info("SMS sent to %s", to)
        return True
    except Exception as exc:  # network, auth, bad number -- log and move on
        log.warning("SMS send failed for %s: %s", to, exc)
        return False


def thursday_reminder_due(now_et: datetime) -> bool:
    """True between 9:00 and 9:15 AM Eastern on Thursdays."""
    return (
        now_et.weekday() == 3  # Monday=0, Thursday=3
        and WINDOW_START <= now_et.time() < WINDOW_END
    )


def current_pick_week(con, season: int) -> int:
    """Earliest week whose last kickoff is still in the future (+4h grace).

    Same rule as app/db.py, reimplemented for the poller's raw sqlite3
    connection so poll.py doesn't need the FastAPI app import.
    """
    rows = con.execute(
        "SELECT week, MAX(kickoff_utc) AS last FROM games "
        "WHERE season = ? GROUP BY week ORDER BY week",
        (season,),
    ).fetchall()
    now = datetime.now(timezone.utc)
    for r in rows:
        last = r["last"]
        if isinstance(last, str):
            last = datetime.fromisoformat(last)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last + timedelta(hours=4) > now:
            return r["week"]
    return rows[-1]["week"] if rows else 1


def run_pick_reminders(con, season: int, now_et: datetime, base_url: str) -> int:
    """Send Thursday-morning reminders. Returns the number sent.

    Expects a sqlite3 connection with Row factory (dict-style access).
    Creates sms_reminders if missing. Commits on success.
    """
    if not thursday_reminder_due(now_et):
        return 0
    con.execute(
        "CREATE TABLE IF NOT EXISTS sms_reminders ("
        "season INTEGER NOT NULL, week INTEGER NOT NULL, "
        "member_id INTEGER NOT NULL REFERENCES members(id), "
        "sent_at TEXT NOT NULL, PRIMARY KEY (season, week, member_id))"
    )
    # Idempotent column guard for DBs that predate the phone columns.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(members)")}
    if "phone" not in cols:
        con.execute("ALTER TABLE members ADD COLUMN phone TEXT")
    if "sms_opt_in" not in cols:
        con.execute("ALTER TABLE members ADD COLUMN sms_opt_in INTEGER NOT NULL DEFAULT 0")
    week = current_pick_week(con, season)
    sent = 0
    members = con.execute(
        "SELECT id, name, phone, pick_token FROM members "
        "WHERE sms_opt_in = 1 AND phone IS NOT NULL AND phone != ''"
    ).fetchall()
    for m in members:
        n = con.execute(
            "SELECT COUNT(*) AS c FROM weekly_picks "
            "WHERE member_id = ? AND season = ? AND week = ?",
            (m["id"], season, week),
        ).fetchone()["c"]
        if n >= PICKS_NEEDED:
            continue
        already = con.execute(
            "SELECT 1 FROM sms_reminders WHERE season = ? AND week = ? AND member_id = ?",
            (season, week, m["id"]),
        ).fetchone()
        if already:
            continue
        body = (
            f"Ravix Prime5: you haven't made all 4 of your Week {week} picks yet "
            f"-- get them in before kickoff: {base_url}/submit/{m['pick_token']}"
        )
        if send_sms(m["phone"], body):
            con.execute(
                "INSERT INTO sms_reminders (season, week, member_id, sent_at) "
                "VALUES (?,?,?,?)",
                (season, week, m["id"], datetime.now(timezone.utc).isoformat()),
            )
            sent += 1
    con.commit()
    return sent
