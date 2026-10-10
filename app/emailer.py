"""Email pick reminders via SMTP (GoDaddy / info@ravixfs.com).

Standard library only: smtplib + ssl + email.message.

Config from the environment:
    RAVIX_SMTP_HOST   (default: smtpout.secureserver.net)
    RAVIX_SMTP_PORT   (default: 465, implicit TLS)
    RAVIX_SMTP_USER   (GoDaddy mailbox login, e.g. info@ravixfs.com)
    RAVIX_SMTP_PASS   (mailbox password)
    RAVIX_EMAIL_FROM  (default: info@ravixfs.com)

When RAVIX_SMTP_USER or RAVIX_SMTP_PASS is missing, sending is a no-op that
logs a warning -- it never raises, so the poller and the app keep working
without email configured.

Thursday-morning reminders are driven by run_email_reminders(), which the
poller calls once per 15-minute run: it only fires inside the 9:00-9:15 AM
ET window on Thursdays, only for members with email_opt_in=1 and a valid
email address, only when they have fewer than 4 picks for the current pick
week, and records each send in email_reminders so nobody gets two in one
week.
"""
import logging
import os
import re
import smtplib
import ssl
from datetime import datetime, time, timedelta, timezone
from email.message import EmailMessage

log = logging.getLogger("ravixfs.emailer")

WINDOW_START = time(9, 0)
WINDOW_END = time(9, 15)
PICKS_NEEDED = 4

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_email(raw) -> str | None:
    """Trim and sanity-check an email address. Returns the cleaned address
    or None when unusable."""
    addr = (raw or "").strip()
    if len(addr) > 254 or not EMAIL_RE.match(addr):
        return None
    return addr


def _config():
    return (
        os.environ.get("RAVIX_SMTP_HOST", "smtpout.secureserver.net"),
        int(os.environ.get("RAVIX_SMTP_PORT", "465")),
        os.environ.get("RAVIX_SMTP_USER", ""),
        os.environ.get("RAVIX_SMTP_PASS", ""),
        os.environ.get("RAVIX_EMAIL_FROM", "info@ravixfs.com"),
    )


def send_email(to: str, subject: str, body: str) -> bool:
    """Send one email via SMTP. Returns True on success.

    Never raises: returns False when unconfigured or when the send fails.
    """
    host, port, user, pw, sender = _config()
    if not (user and pw):
        log.warning(
            "Email not configured (missing RAVIX_SMTP_USER/PASS); "
            "skipping send to %s", to
        )
        return False
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as smtp:
            smtp.login(user, pw)
            smtp.send_message(msg)
        log.info("Email sent to %s", to)
        return True
    except Exception as exc:  # network, auth, bad address -- log and move on
        log.warning("Email send failed for %s: %s", to, exc)
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


def run_email_reminders(con, season: int, now_et: datetime, base_url: str) -> int:
    """Send Thursday-morning reminder emails. Returns the number sent.

    Expects a sqlite3 connection with Row factory (dict-style access).
    Creates email_reminders if missing. Commits on success.
    """
    if not thursday_reminder_due(now_et):
        return 0
    con.execute(
        "CREATE TABLE IF NOT EXISTS email_reminders ("
        "season INTEGER NOT NULL, week INTEGER NOT NULL, "
        "member_id INTEGER NOT NULL REFERENCES members(id), "
        "sent_at TEXT NOT NULL, PRIMARY KEY (season, week, member_id))"
    )
    # Idempotent column guard for DBs that predate the email columns.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(members)")}
    if "email" not in cols:
        con.execute("ALTER TABLE members ADD COLUMN email TEXT")
    if "email_opt_in" not in cols:
        con.execute("ALTER TABLE members ADD COLUMN email_opt_in INTEGER NOT NULL DEFAULT 0")
    week = current_pick_week(con, season)
    sent = 0
    members = con.execute(
        "SELECT id, name, email, pick_token FROM members "
        "WHERE email_opt_in = 1 AND email IS NOT NULL AND email != ''"
    ).fetchall()
    for m in members:
        addr = valid_email(m["email"])
        if not addr:
            continue
        n = con.execute(
            "SELECT COUNT(*) AS c FROM weekly_picks "
            "WHERE member_id = ? AND season = ? AND week = ?",
            (m["id"], season, week),
        ).fetchone()["c"]
        if n >= PICKS_NEEDED:
            continue
        already = con.execute(
            "SELECT 1 FROM email_reminders WHERE season = ? AND week = ? AND member_id = ?",
            (season, week, m["id"]),
        ).fetchone()
        if already:
            continue
        subject = f"Ravix Prime5: your Week {week} picks are due"
        body = (
            f"Hi {m['name']},\n\n"
            f"You haven't made all 4 of your Week {week} Prime5 picks yet. "
            f"Get them in before kickoff:\n{base_url}/submit/{m['pick_token']}\n\n"
            f"To stop these reminders, reply to this email or uncheck the "
            f"email reminders box on your picks page.\n"
        )
        if send_email(addr, subject, body):
            con.execute(
                "INSERT INTO email_reminders (season, week, member_id, sent_at) "
                "VALUES (?,?,?,?)",
                (season, week, m["id"], datetime.now(timezone.utc).isoformat()),
            )
            sent += 1
    con.commit()
    return sent
