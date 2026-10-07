"""Single member identity for all Ravix game apps.

Per-environment shared member store: every staging app opens the same
SHARED_MEMBER_DB; every prod app opens the prod one. Test members live only
in the staging store, so they can never leak into prod.

Cross-subdomain session: a signed cookie scoped to .ravixfs.com. Opening any
pick link on any Ravix subdomain establishes it, so the member is recognized
on every other subdomain. The cookie carries an env claim (staging|prod) and
is only accepted by apps running in the same env, each verifying with its own
SESSION_SECRET_<ENV>.

All helpers are no-ops returning None/False when the shared store or the
session secret is not configured, so games keep working standalone.
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")
COOKIE_NAME = "ravix_member"
SESSION_DAYS = 180


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def app_env() -> str:
    """'staging' or 'prod'. Set per systemd unit via RAVIX_ENV."""
    return os.environ.get("RAVIX_ENV", "staging").strip().lower() or "staging"


def shared_db_path() -> str:
    return os.environ.get("SHARED_MEMBER_DB", "").strip()


def session_secret() -> str:
    return os.environ.get(f"SESSION_SECRET_{app_env().upper()}", "").strip()


def connect_shared():
    """Connection to the environment's shared member DB, or None."""
    path = shared_db_path()
    if not path:
        return None
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_shared_db() -> bool:
    """Idempotent schema init. False when the shared store isn't configured."""
    con = connect_shared()
    if con is None:
        return False
    with open(SCHEMA_PATH) as f:
        con.executescript(f.read())
    con.commit()
    con.close()
    return True


# ---------------- members ----------------

def get_member_by_token(con, token):
    return con.execute(
        "SELECT * FROM members WHERE pick_token = ?", (token,)
    ).fetchone()


def get_member_by_id(con, member_id):
    return con.execute(
        "SELECT * FROM members WHERE id = ?", (member_id,)
    ).fetchone()


def create_member(con, name: str, is_test: bool = False):
    """New member with a fresh pick token."""
    name = (name or "").strip()
    if not name:
        raise ValueError("name required")
    token = secrets.token_urlsafe(16)
    cur = con.execute(
        "INSERT INTO members (name, pick_token, is_test, created_at) "
        "VALUES (?,?,?,?)",
        (name, token, 1 if is_test else 0, utcnow_iso()),
    )
    con.commit()
    return get_member_by_id(con, cur.lastrowid)


def import_member(con, name: str, pick_token: str, is_test: bool = False):
    """Adopt a member from a legacy per-game DB, preserving their pick token
    so old pick links keep working."""
    row = get_member_by_token(con, pick_token)
    if row:
        con.execute(
            "UPDATE members SET name = ?, is_test = ? WHERE id = ?",
            (name, 1 if is_test else 0, row["id"]),
        )
        con.commit()
        return get_member_by_id(con, row["id"])
    cur = con.execute(
        "INSERT INTO members (name, pick_token, is_test, created_at) "
        "VALUES (?,?,?,?)",
        (name, pick_token, 1 if is_test else 0, utcnow_iso()),
    )
    con.commit()
    return get_member_by_id(con, cur.lastrowid)


def ensure_membership(con, member_id: int, game_key: str) -> None:
    con.execute(
        "INSERT OR IGNORE INTO game_memberships (member_id, game_key, created_at) "
        "VALUES (?,?,?)",
        (member_id, game_key, utcnow_iso()),
    )
    con.commit()


def member_games(con, member_id: int) -> list:
    return [r["game_key"] for r in con.execute(
        "SELECT game_key FROM game_memberships WHERE member_id = ? ORDER BY game_key",
        (member_id,),
    ).fetchall()]


def game_members(con, game_key: str, include_test: bool = False):
    q = ("SELECT m.* FROM members m JOIN game_memberships g "
         "ON g.member_id = m.id WHERE g.game_key = ?")
    if not include_test:
        q += " AND COALESCE(m.is_test, 0) = 0"
    return con.execute(q + " ORDER BY m.name", (game_key,)).fetchall()


# ---------------- cross-subdomain session ----------------

def _sign(member_id: int, env: str, secret: str) -> str:
    msg = f"{member_id}.{env}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def issue_session_value(member_id: int):
    """Signed 'member_id.env.sig' value, or None when unconfigured."""
    secret = session_secret()
    if not secret:
        return None
    env = app_env()
    return f"{member_id}.{env}.{_sign(member_id, env, secret)}"


def verify_session_value(value: str):
    """Return member_id if the cookie value is valid for this env, else None."""
    secret = session_secret()
    if not secret or not value:
        return None
    try:
        member_id_s, env, sig = value.split(".")
        member_id = int(member_id_s)
    except (ValueError, AttributeError):
        return None
    if env != app_env():
        return None
    if not hmac.compare_digest(sig, _sign(member_id, env, secret)):
        return None
    return member_id


def cookie_domain_for(host: str):
    """Parent-domain scope so every *.ravixfs.com subdomain sees the cookie.
    Returns None for localhost/IPs (Domain attr would be rejected there)."""
    host = (host or "").split(":")[0].lower()
    if host.endswith(".ravixfs.com"):
        return ".ravixfs.com"
    return None


def set_session_cookie(response, request, member_id: int) -> bool:
    """Establish the cross-subdomain session on a response. False if skipped."""
    value = issue_session_value(member_id)
    if value is None:
        return False
    host = (request.headers.get("host", "") or "").split(":")[0].lower()
    domain = cookie_domain_for(host)
    kwargs = dict(
        key=COOKIE_NAME, value=value, path="/", httponly=True,
        samesite="lax", max_age=SESSION_DAYS * 24 * 3600,
    )
    if domain:
        kwargs["domain"] = domain
    if host not in ("localhost", "127.0.0.1"):
        kwargs["secure"] = True
    response.set_cookie(**kwargs)
    return True


def session_member_id(request):
    return verify_session_value(request.cookies.get(COOKIE_NAME, ""))


def session_member(request):
    """Member row for this request's shared session, or None."""
    member_id = session_member_id(request)
    if member_id is None:
        return None
    con = connect_shared()
    if con is None:
        return None
    try:
        return get_member_by_id(con, member_id)
    finally:
        con.close()
