"""ravixfs - NFL defensive Prime5 league site."""
import json
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db as _db
from . import gamestate as _gamestate
from . import scoring
from . import sms as _sms
from . import emailer as _emailer
from . import matchup
from shared import identity as _identity

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
# Staging gate: staging.ravixfs.com requires a personal pick link.
# No shared key: a member opens their STAGING pick link once (or pastes it
# on /locked), and the browser stays unlocked. Production never gates.
# Cookie is host-only (no Domain=) so a staging pick token is never sent
# to production, and never accepted there.
STAGING_PICK_COOKIE = "staging_pick"
BASE = os.path.dirname(__file__)

app = FastAPI(title="ravixfs")
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))

# Cache-bust the stylesheet: browsers hold onto /static/style.css, so every
# deploy that changes it must change the URL. The file's mtime moves on
# every git pull, and the app restarts on deploy, so this is always fresh.
try:
    _CSS_VERSION = str(int(os.path.getmtime(os.path.join(BASE, "static", "style.css"))))
except OSError:
    _CSS_VERSION = "1"
templates.env.globals["css_version"] = _CSS_VERSION


def _load_releases():
    """Full release history from app/releases.json, newest first.

    To ship a new version: prepend one entry (version, date, title,
    notes). The banner automatically shows the first entry; /releases
    shows the whole history. No other file needs to change.
    """
    try:
        with open(os.path.join(BASE, "releases.json"), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [r for r in data
                    if isinstance(r, dict) and r.get("version") and r.get("title")]
    except Exception:
        pass
    return []


RELEASES = _load_releases()
RELEASE = RELEASES[0] if RELEASES else None
templates.env.globals["release"] = RELEASE


def _request_pick_token(request: Request):
    """Pick token identifying this browser, or None.

    Shared session first; staging fallback cookie second. The fallback
    cookie is only honored on staging / localhost — never on production,
    so a staging token can't leak into prod identity even if a cookie
    were somehow sent there. Callers look the token up in the per-game
    DB, so a forged/unknown token simply resolves to no member.
    """
    try:
        sm = _identity.session_member(request)
        if sm and sm.get("pick_token"):
            return sm["pick_token"]
    except Exception:
        pass
    host = (request.headers.get("host", "") or "").lower().split(":")[0]
    if host.startswith("staging.") or host in ("localhost", "127.0.0.1"):
        tok = (request.cookies.get(STAGING_PICK_COOKIE, "") or "").strip()
        return tok or None
    return None


def _staging_token_member(token: str):
    """Member row for a staging pick token, or None. DB-validated."""
    token = (token or "").strip()
    if not token:
        return None
    try:
        con = _db.connect()
        try:
            return con.execute(
                "SELECT * FROM members WHERE pick_token = ?", (token,)
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return None


def _set_staging_pick_cookie(response, request: Request, token: str) -> None:
    """Host-only staging cookie carrying the member's own pick token.

    The pick token IS the credential (same as the pick link itself), so
    storing it in an HttpOnly cookie is no weaker than the link. Host-only
    (no Domain=) so it never goes to production. Secure on real hosts.
    """
    host = (request.headers.get("host", "") or "").split(":")[0].lower()
    kwargs = dict(key=STAGING_PICK_COOKIE, value=token, path="/",
                  httponly=True, samesite="lax",
                  max_age=180 * 24 * 3600)
    if host not in ("localhost", "127.0.0.1"):
        kwargs["secure"] = True
    response.set_cookie(**kwargs)


def _extract_pick_token(text: str) -> str:
    """Pull a pick token out of a pasted pick link or a bare token."""
    text = (text or "").strip()
    if not text:
        return ""
    if "/submit/" in text:
        text = text.split("/submit/", 1)[1]
        text = text.split("?")[0].split("#")[0].split("/")[0]
    return text.strip()


def _clean_url_without_token(request: Request) -> str:
    """Same URL with the ?token= param stripped, so the pick token doesn't
    linger in the address bar, history, or logs."""
    parsed = urlparse(str(request.url))
    kept = [(k, v) for k, v in parse_qsl(parsed.query) if k != "token"]
    return urlunparse(parsed._replace(query=urlencode(kept)))


def _staging_unlocked(request: Request) -> bool:
    """True when this browser has proven a staging pick link."""
    try:
        if _identity.session_member(request) is not None:
            return True
    except Exception:
        pass
    cookie = request.cookies.get(STAGING_PICK_COOKIE, "")
    return _staging_token_member(cookie) is not None


@app.middleware("http")
async def _staging_gate(request: Request, call_next):
    """Gate staging.ravixfs.com behind a personal staging pick link.

    - /locked, /unlock (compat redirect), and /static/* always pass.
    - /submit/<token> always passes: it IS the front door, and opening it
      sets both the shared session and the staging fallback cookie.
    - /admin/* always passes through: the handler still enforces ADMIN_TOKEN.
    - ?token=<staging pick token> on any URL unlocks, then redirects to
      the same URL minus the token.
    - Otherwise a shared session or a validated fallback cookie passes;
      everyone else goes to /locked.
    Localhost / pollers hit 127.0.0.1 directly (not the staging host), so
    they bypass the gate. Production never gates.
    """
    host = (request.headers.get("host", "") or "").lower()
    if not host.startswith("staging."):
        return await call_next(request)
    path = request.url.path
    if path in ("/locked", "/unlock") or path.startswith("/static/"):
        return await call_next(request)
    if path.startswith("/submit/") or path.startswith("/admin/"):
        return await call_next(request)
    token_param = request.query_params.get("token", "")
    if token_param and _staging_token_member(token_param) is not None:
        resp = RedirectResponse(_clean_url_without_token(request) or "/", status_code=303)
        _set_staging_pick_cookie(resp, request, token_param.strip())
        return resp
    if _staging_unlocked(request):
        return await call_next(request)
    nxt = request.url.path
    kept = [(k, v) for k, v in parse_qsl(request.url.query) if k != "token"]
    if kept:
        nxt += "?" + urlencode(kept)
    return RedirectResponse(f"/locked?next={quote(nxt, safe='')}", status_code=303)


@app.get("/locked", response_class=HTMLResponse)
def staging_locked(request: Request, next: str = "/", token: str = ""):
    """Staging lock page. ?token= here also unlocks (same as on any URL)."""
    if token and _staging_token_member(token) is not None:
        resp = RedirectResponse(next or "/", status_code=303)
        _set_staging_pick_cookie(resp, request, token.strip())
        return resp
    if _staging_unlocked(request):
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(request, "lock.html", {
        "request": request, "next_url": next or "/", "error": False,
    })


@app.post("/locked", response_class=HTMLResponse)
def staging_locked_post(request: Request, pick_link: str = Form(""),
                        next: str = Form("/")):
    token = _extract_pick_token(pick_link)
    if token and _staging_token_member(token) is not None:
        resp = RedirectResponse(next or "/", status_code=303)
        _set_staging_pick_cookie(resp, request, token)
        return resp
    return templates.TemplateResponse(request, "lock.html", {
        "request": request, "next_url": next or "/", "error": True,
    })


@app.get("/unlock", response_class=HTMLResponse)
def staging_unlock_compat(request: Request, next: str = "/"):
    """Old lock URL from the brief shared-key build: send to /locked."""
    return RedirectResponse(f"/locked?next={quote(next or '/', safe='')}", status_code=303)


@app.on_event("startup")
def _init_shared_identity():
    # Idempotent; no-op when SHARED_MEMBER_DB is not configured.
    # Part of the cross-game member identity (docs/shared-identity.md).
    try:
        _identity.init_shared_db()
    except Exception:
        pass
    # Run schema migrations on every startup so deploys that add columns
    # (e.g. email/email_opt_in) apply themselves. Idempotent.
    try:
        _db.init_db()
    except Exception:
        pass


def _establish_session(response, request, member_row) -> None:
    """Shared cross-subdomain session: opening a pick link signs the member
    in on every Ravix subdomain. Adopts the member into the shared store
    (preserving their pick token) when needed. No-op when unconfigured.
    Never raises."""
    try:
        con = _identity.connect_shared()
        if con is None:
            return
        try:
            m = _identity.import_member(
                con, member_row["name"], member_row["pick_token"],
                bool(member_row["is_test"]),
            )
            _identity.ensure_membership(con, m["id"], "pick5f")
            shared_id = m["id"]
        finally:
            con.close()
        _identity.set_session_cookie(response, request, shared_id)
    except Exception:
        pass


def _admin_ok(token: str) -> bool:
    return bool(ADMIN_TOKEN) and secrets.compare_digest(token, ADMIN_TOKEN)


def _resolve_player(con, name: str):
    """Name -> single player row (defense only). Returns (row, error)."""
    name = (name or "").strip()
    if not name:
        return None, "empty pick"
    rows = con.execute(
        "SELECT * FROM players WHERE lower(name) = lower(?) "
        "AND position_group IN ('DB','DL','LB')",
        (name,),
    ).fetchall()
    if not rows:
        return None, f"no defensive player found for '{name}'"
    if len(rows) > 1:
        teams = ", ".join(r["team"] or "?" for r in rows)
        return None, f"'{name}' is ambiguous ({teams}) -- be more specific"
    return rows[0], None


def _is_staging_host(host: str) -> bool:
    """True on the staging instance (staging.ravixfs.com). Used to relax
    test-member filtering there so the ticker is visible during the test pass."""
    return (host or "").lower().startswith("staging.")


# ---- Ticker headline ---------------------------------------------------
# After each set of games (Thu night, Sun 1pm, Sun 4pm, SNF, MNF) the
# ticker gains a HEADLINE segment summarizing the window that just
# finished: its top picked player plus what the window did to the week's
# lead. Windows are kickoff clusters; a window "closes" 3.5h after its
# last kickoff, when its games are final and the stats have landed. A
# player's points belong to the window his game kicked off in (subs swap
# within a window, so stint points stay in-window). Returns "" until a
# closed window actually has points in it, so the segment never shows
# stale or empty copy.

def _window_key(kickoff_utc):
    """((ET date, daypart), kickoff datetime) for a kickoff ISO string."""
    try:
        ko = datetime.fromisoformat(str(kickoff_utc).replace("Z", "+00:00"))
        if ko.tzinfo is None:
            ko = ko.replace(tzinfo=timezone.utc)
        from zoneinfo import ZoneInfo
        local = ko.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return None
    part = "early" if local.hour < 16 else "late" if local.hour < 19 else "night"
    return (local.date(), part), ko


def _window_label(key) -> str:
    day, part = key
    wd = day.weekday()  # Mon=0 .. Sun=6
    if wd == 6:
        return {"early": "SUNDAY 1 PM", "late": "SUNDAY 4 PM",
                "night": "SUNDAY NIGHT"}[part]
    if wd == 0:
        return "MONDAY NIGHT" if part == "night" else "MONDAY"
    if wd == 3:
        return "THURSDAY NIGHT" if part == "night" else "THURSDAY"
    if wd == 4:
        return "FRIDAY NIGHT" if part == "night" else "FRIDAY"
    if wd == 5:
        return "SATURDAY NIGHT" if part == "night" else "SATURDAY"
    return day.strftime("%A").upper()


def _headline_desc(stats) -> str:
    """Short stat-line flavor: 'pick-six', 'interception + sack', '11 tackles'."""
    if not stats:
        return "big game"

    def g(k):
        try:
            return float(stats.get(k) or 0)
        except Exception:
            return 0.0

    parts = []
    if g("interceptions") >= 1 and g("def_tds") >= 1:
        parts.append("pick-six")
    elif g("fumble_recoveries") >= 1 and g("def_tds") >= 1:
        parts.append("scoop-and-score")
    elif g("def_tds") >= 1:
        parts.append("defensive TD")
    if g("interceptions") >= 1 and "pick-six" not in parts:
        n = g("interceptions")
        parts.append("interception" if n == 1 else f"{n:g} interceptions")
    if g("safeties") >= 1:
        parts.append("safety")
    if g("sacks") >= 1:
        n = g("sacks")
        parts.append("sack" if n == 1 else f"{n:g} sacks")
    if g("forced_fumbles") >= 1:
        parts.append("forced fumble")
    if g("fumble_recoveries") >= 1 and not any("scoop" in p for p in parts):
        parts.append("fumble recovery")
    if len(parts) < 2 and g("tackles") >= 6:
        parts.append(f"{int(g('tackles'))} tackles")
    return " + ".join(parts[:2]) if parts else "big game"


def _week_windows(con, season: int, week: int, entries, member_lines):
    """The week's kickoff windows with per-window member gains and picked
    players, oldest first. Each window: {key, label, close, gains, players,
    entries} — entries being the week leaderboard AS OF that window's end
    (cumulative window gains), so archived headlines stay true to their
    moment even when generated later.
    """
    from datetime import timedelta
    games = con.execute(
        "SELECT kickoff_utc FROM games WHERE season = ? AND week = ?",
        (season, week)).fetchall()
    windows = {}
    for g in games:
        got = _window_key(g["kickoff_utc"])
        if not got:
            continue
        key, ko = got
        w = windows.setdefault(key, {"key": key, "last_ko": ko,
                                     "gains": {}, "players": {}})
        w["last_ko"] = max(w["last_ko"], ko)
    ordered = sorted(windows.values(), key=lambda w: w["last_ko"])
    for w in ordered:
        w["label"] = _window_label(w["key"])
        w["close"] = w["last_ko"] + timedelta(hours=3, minutes=30)
    for mname, lines in member_lines.items():
        for pid, pname, _team, _src, pts, stats in lines:
            if not pts:
                continue
            ko = _db.player_kickoff(con, pid, season, week)
            got = _window_key(ko) if ko else None
            if not got:
                continue
            w = windows.get(got[0])
            if w is None:
                continue
            w["gains"][mname] = round(w["gains"].get(mname, 0.0) + pts, 2)
            cur = w["players"].get(pid)
            if cur is None or pts > cur["pts"]:
                w["players"][pid] = {"name": pname, "pts": pts,
                                     "stats": stats, "owners": set()}
            w["players"][pid]["owners"].add(mname)
    running = {e["name"]: 0.0 for e in entries}
    for w in ordered:
        for n, g in w["gains"].items():
            running[n] = round(running.get(n, 0.0) + g, 2)
        w["entries"] = sorted(
            ({"name": n, "points": p} for n, p in running.items()),
            key=lambda e: e["points"], reverse=True)
    return ordered


def _first_names(totals):
    """First-name lookup for prose; full name when two members share one."""
    def first(name):
        fn = name.split()[0] if name else name
        return name if sum(1 for n in totals if n.split()[0] == fn) > 1 else fn
    return first


def _headline_for_window(week: int, active, is_final: bool) -> str:
    """The ticker headline text for one completed window, judged on the
    leaderboard as of that window's end (active['entries'])."""
    entries = active["entries"]
    totals = {e["name"]: e["points"] for e in entries}
    first = _first_names(totals)
    leader = entries[0]["name"]
    runner = entries[1]["name"] if len(entries) > 1 else None
    before = {n: round(totals[n] - active["gains"].get(n, 0.0), 2)
              for n in totals}
    leader_before = (max(before, key=before.get)
                     if any(v > 0 for v in before.values()) else None)
    top = None
    if active["players"]:
        top = max(active["players"].values(), key=lambda d: d["pts"])
    desc = _headline_desc(top["stats"]) if top else ""

    if is_final:
        a = totals[leader]
        b = totals[runner] if runner else 0.0
        if runner is None:
            lead_clause = f"{first(leader)} wins it, {a:.1f}"
        elif leader_before and leader_before != leader:
            lead_clause = f"{first(leader)} beats {first(runner)} {a:.1f}–{b:.1f}"
        else:
            lead_clause = f"{first(leader)} holds off {first(runner)} {a:.1f}–{b:.1f}"
        if top and top["pts"] >= 1.0:
            if leader in top["owners"]:
                tail = f"{top['name']}'s {desc} seals it"
            else:
                tail = (f"{top['name']}'s {desc} ({top['pts']:.1f} pts) "
                        f"headlines the finale")
            return f"WEEK {week} FINAL — {lead_clause}; {tail}"
        return f"WEEK {week} FINAL — {lead_clause}"

    label = active["label"]
    player_clause = ""
    if top and top["pts"] >= 1.0:
        player_clause = f"{top['name']}'s {desc} ({top['pts']:.1f} pts)"
    owned = bool(top) and leader in top["owners"]
    if leader_before is None:
        verb = f"puts {first(leader)} on top early" if owned else None
        stmt = f"{first(leader)} leads early"
    elif leader_before != leader:
        verb = f"lifts {first(leader)} into the lead" if owned else None
        stmt = f"{first(leader)} takes the lead"
    else:
        verb = None
        stmt = (f"{first(leader)} pads the lead"
                if active["gains"].get(leader, 0) > 0
                else f"{first(leader)} stays on top")
    if player_clause and verb:
        return f"{label} — {player_clause} {verb}"
    if player_clause:
        return f"{label} — {player_clause}; {stmt}"
    return f"{label} — {stmt}"


def _active_window(ordered):
    """Latest closed window that actually has points in it, or None."""
    now = datetime.now(timezone.utc)
    active = None
    for w in ordered:
        if now >= w["close"] and any(v > 0 for v in w["gains"].values()):
            active = w
    return active


def _ticker_headline(con, season: int, week: int, entries, member_lines) -> str:
    """HEADLINE segment text for the ticker, or "" when none is earned yet.

    entries: the ticker's [{name, points}] sorted desc. member_lines:
    {member name: member_week_points lines for `week`}.
    """
    if not week or not entries:
        return ""
    ordered = _week_windows(con, season, week, entries, member_lines)
    active = _active_window(ordered)
    if active is None:
        return ""
    return _headline_for_window(week, active, active is ordered[-1])


def _subs_for_window(con, season: int, week: int, wkey, member_names):
    """(member name, sub outcome) for subs whose out-player game falls in
    this window, oldest first. A sub belongs to the window its outgoing
    player's game kicked off in (subs swap within a window). Never breaks
    the report: any failure yields no sub block."""
    lines = []
    try:
        for m in con.execute("SELECT id, name FROM members"):
            if member_names is not None and m["name"] not in member_names:
                continue
            for o in scoring.sub_outcomes(con, m["id"], season, week):
                ko = _db.player_kickoff(con, o["out_player_id"],
                                        season, week)
                got = _window_key(ko) if ko else None
                if got and got[0] == wkey:
                    lines.append((m["name"], o))
    except Exception:
        return []
    lines.sort(key=lambda t: t[1]["subbed_at"])
    return lines


def _news_body_for_window(con, season: int, week: int, w,
                          is_final: bool, member_lines=None) -> str:
    """Recap body for a window's League News post (plain text; blank-line
    separated blocks, lines inside a block joined by single newlines).
    The leaderboard shown is the week's as of this window's end."""
    entries = w["entries"]
    blocks = []
    players = sorted(w["players"].values(), key=lambda d: d["pts"],
                     reverse=True)
    players = [p for p in players if p["pts"] > 0][:3]
    if players:
        lines = [f"Top performers — {w['label']}:"]
        for p in players:
            owners = ", ".join(sorted(p["owners"]))
            lines.append(f"• {p['name']} — {p['pts']:.1f} pts · "
                         f"{_headline_desc(p['stats'])} · picked by {owners}")
        blocks.append("\n".join(lines))
    gains = sorted(((n, g) for n, g in w["gains"].items() if g > 0),
                   key=lambda kv: -kv[1])
    if gains:
        blocks.append("Window scoring:\n" + " · ".join(
            f"{n} +{g:.1f}" for n, g in gains))
    subs = _subs_for_window(con, season, week, w["key"],
                            set(member_lines) if member_lines else None)
    if subs:
        lines = ["Substitutions — did the swap pay off?:"]
        for mname, o in subs:
            out_after = round(o["earned"] - o["net"], 2)
            verdict = ("Paid off" if o["net"] > 0 else
                       "Wash" if o["net"] == 0 else "Backfired")
            clock = f" ({o['game_label']})" if o["game_label"] else ""
            lines.append(
                f"• {mname} — {o['out_name']} out "
                f"({o['banked']:.1f} banked) → {o['in_name']} in{clock}: "
                f"{o['in_name']} +{o['earned']:.1f} since entry · "
                f"{o['out_name']} +{out_after:.1f} after → "
                f"net {o['net']:+.1f} · {verdict}")
        blocks.append("\n".join(lines))
    if entries:
        blocks.append(f"Week {week} leaderboard:\n" + " · ".join(
            f"{i}. {e['name']} {e['points']:.1f}"
            for i, e in enumerate(entries, 1)))
    if is_final and entries:
        blocks.append(f"{entries[0]['name']} takes Week {week}.")
        try:
            real, test = scoring.standings(con, season, through_week=week)
            table = real or test
            if table:
                blocks.append(
                    f"Season standings after Week {week}:\n" + " · ".join(
                        f"{i}. {n} {t:.1f}"
                        for i, (_id, n, t) in enumerate(table, 1)))
        except Exception:
            pass
    return "\n\n".join(blocks)


def _window_key_str(key) -> str:
    return f"{key[0].isoformat()}:{key[1]}"


def _ensure_news_posts(con, season: int, week: int, entries, member_lines):
    """Generate any missing League News auto posts for the week's closed
    windows. Returns (posts_by_window_key, active_post_id). Idempotent:
    posts are unique per (season, week, window); windows the admin
    deleted stay deleted via news_deleted_windows tombstones.
    """
    if not week or not entries:
        return {}, None, []
    try:
        existing = {r["window_key"]: r["id"] for r in con.execute(
            "SELECT id, window_key FROM news_posts "
            "WHERE season = ? AND week = ? AND window_key IS NOT NULL",
            (season, week))}
        deleted = {r["window_key"] for r in con.execute(
            "SELECT window_key FROM news_deleted_windows "
            "WHERE season = ? AND week = ?", (season, week))}
    except Exception:
        return {}, None, []  # news tables predate this deploy
    ordered = _week_windows(con, season, week, entries, member_lines)
    now = datetime.now(timezone.utc)
    changed = False
    for w in ordered:
        wk = _window_key_str(w["key"])
        if wk in existing or wk in deleted:
            continue
        if now < w["close"] or not any(v > 0 for v in w["gains"].values()):
            continue
        is_final = w is ordered[-1]
        cur = con.execute(
            "INSERT INTO news_posts (season, week, window_key, kind, "
            "headline, body, author, created_at) VALUES (?, ?, ?, 'auto', "
            "?, ?, 'Prime5 Wire', ?)",
            (season, week, wk,
             _headline_for_window(week, w, is_final),
             _news_body_for_window(con, season, week, w, is_final,
                                   member_lines),
             w["close"].strftime("%Y-%m-%dT%H:%M:%SZ")))
        existing[wk] = cur.lastrowid
        changed = True
    if changed:
        con.commit()
    active = _active_window(ordered)
    active_id = None
    if active is not None:
        active_id = existing.get(_window_key_str(active["key"]))
    return existing, active_id, ordered


def _ticker_data(con, season: int, include_test: bool = False) -> dict:
    """Live points data: current (latest-scored) week.

    Uses the latest week with stats rows (what the 15-min poller updates).
    Test members are excluded from the ticker, except on staging where
    they are included so the ticker is visible during the test pass.
    """
    wrow = con.execute(
        "SELECT MAX(week) AS w FROM weekly_stats WHERE season = ?", (season,)
    ).fetchone()
    week = wrow["w"] or 0
    pick_week = _db.current_pick_week(con, season)
    if not week:
        week = pick_week
    elif pick_week != week:
        # Live scoring: while the pick week's games are in progress (or
        # before the batch feed publishes), its live_stats rows are fresher
        # than last week's finals.
        try:
            has_live = con.execute(
                "SELECT 1 FROM live_stats WHERE season = ? AND week = ? LIMIT 1",
                (season, pick_week),
            ).fetchone()
        except Exception:
            has_live = None  # live_stats table predates this deploy
        if has_live:
            week = pick_week
    entries = []
    member_lines = {}
    where = "" if include_test else "WHERE COALESCE(is_test, 0) = 0 "
    for m in con.execute(
        "SELECT id, name FROM members " + where + "ORDER BY name"
    ):
        pts, lines = scoring.member_week_points(con, m["id"], season, week)
        entries.append({"name": m["name"], "points": pts})
        member_lines[m["name"]] = lines
    entries.sort(key=lambda e: e["points"], reverse=True)
    # "LIVE" while any game of this week has kicked off and it's the
    # current pick week (i.e. games may still be in progress).
    live = False
    games = con.execute(
        "SELECT kickoff_utc FROM games WHERE season = ? AND week = ?",
        (season, week),
    ).fetchall()
    if games and week == _db.current_pick_week(con, season):
        now = datetime.now(timezone.utc)
        for g in games:
            ko = datetime.fromisoformat(g["kickoff_utc"])
            if ko.tzinfo is None:
                ko = ko.replace(tzinfo=timezone.utc)
            if ko <= now:
                live = True
                break
    try:
        from zoneinfo import ZoneInfo
        stamp = datetime.now(ZoneInfo("America/New_York")).strftime("%-I:%M %p ET")
    except Exception:
        stamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
    try:
        headline = _ticker_headline(con, season, week, entries, member_lines)
    except Exception:
        headline = ""  # the ticker must never break over a headline
    headline_id = None
    posted, ordered = {}, []
    if headline:
        try:
            posted, headline_id, ordered = _ensure_news_posts(
                con, season, week, entries, member_lines)
        except Exception:
            headline_id = None  # link is a bonus; the headline still shows
    # Recent substitutions in the same week, newest first: every completed
    # sub scrolls in the ticker with a SUB tag, alongside the headline.
    # Same test-member rule as the entries: staging shows test members so
    # the feature is visible during the test pass, production hides them.
    # Once a window's report is out (published, or deleted by the admin),
    # that window's subs leave the ticker — the report is the record now.
    sub_news = []
    try:
        reported = set()
        try:
            for r in con.execute(
                    "SELECT window_key FROM news_posts "
                    "WHERE season = ? AND week = ? AND window_key IS NOT NULL",
                    (season, week)):
                reported.add(r["window_key"])
            for r in con.execute(
                    "SELECT window_key FROM news_deleted_windows "
                    "WHERE season = ? AND week = ?", (season, week)):
                reported.add(r["window_key"])
        except Exception:
            reported = set()
        test_filter = "" if include_test else "AND COALESCE(m.is_test, 0) = 0 "
        for s in con.execute(
                "SELECT s.out_player_id, s.game_label, m.name AS member_name, "
                "po.name AS out_name, pi.name AS in_name "
                "FROM weekly_subs s JOIN members m ON m.id = s.member_id "
                "JOIN players po ON po.player_id = s.out_player_id "
                "JOIN players pi ON pi.player_id = s.in_player_id "
                "WHERE s.season = ? AND s.week = ? " + test_filter +
                "ORDER BY s.subbed_at DESC LIMIT 20",
                (season, week)):
            ko = _db.player_kickoff(con, s["out_player_id"], season, week)
            got = _window_key(ko) if ko else None
            if got and _window_key_str(got[0]) in reported:
                continue
            text = f"{s['member_name']} subs {s['out_name']} → {s['in_name']}"
            if s["game_label"]:
                text += f" ({s['game_label']})"
            sub_news.append({"text": text})
            if len(sub_news) >= 5:
                break
    except Exception:
        sub_news = []  # the ticker must never break over sub news
    # Correction notices: stat drops in the most recently reported window
    # scroll in the ticker (linking to the amended post) until the next
    # window's report goes out. Older corrections stay in their posts.
    corrections = []
    try:
        latest_key, latest_id = None, None
        for w in ordered:
            wk = _window_key_str(w["key"])
            if wk in posted:
                latest_key, latest_id = wk, posted[wk]
        if latest_key:
            for r in con.execute(
                    "SELECT c.*, p.name AS pname FROM stat_corrections c "
                    "JOIN players p ON p.player_id = c.player_id "
                    "WHERE c.season = ? AND c.week = ? "
                    "ORDER BY c.detected_at DESC LIMIT 10",
                    (season, week)):
                ko = _db.player_kickoff(con, r["player_id"], season, week)
                got = _window_key(ko) if ko else None
                if not got or _window_key_str(got[0]) != latest_key:
                    continue
                label = _CORRECTION_STAT_LABELS.get(
                    r["stat_key"], r["stat_key"])
                corrections.append({
                    "text": f"{r['pname']}'s {label} corrected "
                            f"({r['old_total']:g} → {r['new_total']:g})",
                    "post_id": latest_id})
                if len(corrections) >= 3:
                    break
    except Exception:
        corrections = []  # the ticker must never break over corrections
    return {"week": week, "entries": entries, "live": live,
            "updated": stamp, "headline": headline,
            "headline_id": headline_id, "sub_news": sub_news,
            "corrections": corrections}


@app.get("/ticker.json")
def ticker_json(request: Request):
    con = _db.connect()
    season = _db.current_season(con)
    data = _ticker_data(con, season, include_test=_is_staging_host(request.headers.get("host", "")))
    con.close()
    return JSONResponse(data)


_SUMMARY_PLAYS = {
    "sacks": ("Sack", "Sacks"),
    "interceptions": ("Interception", "Interceptions"),
    "forced_fumbles": ("Forced fumble", "Forced fumbles"),
    "fumble_recoveries": ("Fumble recovery", "Fumble recoveries"),
    "safeties": ("Safety", "Safeties"),
    "def_tds": ("Defensive TD", "Defensive TDs"),
}


def _et_event_label(iso_ts):
    """'Thu 9:41 PM' in Eastern for a scoring event timestamp."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%a %-I:%M %p")
    except Exception:
        return ""


def _scoring_summary(con, mid, season, week, limit=12):
    """The member's scoring plays for the week, newest first.

    Built from scoring_events (the same feed as the scoring popups),
    but only the plays that actually counted: a play is kept only if it
    falls inside the player's active stint — past his entry snapshot
    and no further than his sub-out snapshot. So a subbed-out player's
    later plays don't show (they scored nothing for the member), and a
    replacement's pre-entry plays don't either. Cumulative totals are
    the bounds, so poller timing can't misplace a play. Big plays stay
    individual; tackle events are grouped per player so the list isn't
    drowned in +0.1 rows. Finally, each player+stat is clamped to the
    stint's own stat points: the event ledger only records climbs, so
    after a downward revision or a feed switch it can sum higher than
    what the stint counted, and the summary must never show more than
    the points that count.
    """
    stints = scoring.member_week_stints(con, mid, season, week)
    by_pid = {s["player_id"]: s for s in stints}
    if not by_pid:
        return []
    cfg = scoring.scoring_map(con)
    # Clamp: the event ledger only ever records climbs, so after any
    # downward revision (or a feed switch, e.g. Tank01 -> nflverse) the
    # summed events can exceed what the stint actually counted. Cap each
    # player+stat at the stint's own stat points, so the summary never
    # shows more than the points that count.
    caps = {}
    for s in stints:
        for k, d in s["stats"].items():
            caps[(s["player_id"], k)] = max(0.0, round(d * cfg.get(k, 0), 2))
    ph = ",".join("?" * len(by_pid))
    rows = con.execute(
        "SELECT e.player_id, e.stat_key, e.old_total, e.new_total, "
        "e.points_delta, e.created_at, p.name, p.team "
        "FROM scoring_events e JOIN players p ON p.player_id = e.player_id "
        f"WHERE e.season = ? AND e.week = ? AND e.player_id IN ({ph}) "
        "ORDER BY e.created_at DESC",
        (season, week, *by_pid.keys())).fetchall()
    out, tackles = [], {}
    for r in rows:
        st = by_pid[r["player_id"]]
        if not scoring.stint_counts_event(st, r["stat_key"], r["new_total"]):
            continue
        delta = round(r["new_total"] - r["old_total"], 2)
        if r["stat_key"] == "tackles":
            g = tackles.setdefault(r["player_id"], {
                "name": r["name"], "team": r["team"], "big": False,
                "count": 0.0, "points": 0.0, "at": r["created_at"]})
            g["count"] += delta
            g["points"] = round(g["points"] + r["points_delta"], 2)
            continue
        sing, plur = _SUMMARY_PLAYS.get(
            r["stat_key"], (r["stat_key"], r["stat_key"]))
        play = sing if delta <= 1 else f"{plur} ×{delta:g}"
        out.append({"name": r["name"], "team": r["team"], "big": True,
                    "play": play, "points": r["points_delta"],
                    "at": r["created_at"], "pid": r["player_id"],
                    "stat": r["stat_key"]})
    # Clamp big plays per player+stat: drop the oldest rows first until
    # the group's summed points fit the stint's cap.
    big_by_key: dict = {}
    for e in out:
        big_by_key.setdefault((e["pid"], e["stat"]), []).append(e)
    drop_ids = set()
    for (pid, stat), rows in big_by_key.items():
        cap = caps.get((pid, stat))
        if cap is None:
            continue
        total = round(sum(r["points"] for r in rows), 2)
        if total <= cap + 1e-9:
            continue
        for r in sorted(rows, key=lambda r: r["at"]):
            if total <= cap + 1e-9:
                break
            drop_ids.add(id(r))
            total = round(total - r["points"], 2)
    out = [e for e in out if id(e) not in drop_ids]
    for e in out:
        e.pop("pid", None)
        e.pop("stat", None)
    for pid, g in tackles.items():
        cap = caps.get((pid, "tackles"))
        if cap is not None:
            if g["points"] > cap + 1e-9:
                g["points"] = cap
            tdelta = by_pid[pid]["stats"].get("tackles", 0) or 0
            if g["count"] > tdelta:
                g["count"] = tdelta
        n = g.pop("count")
        g["play"] = "Tackle" if n == 1 else f"Tackles ×{n:g}"
        out.append(g)
    out.sort(key=lambda e: e["at"], reverse=True)
    for e in out:
        e["when"] = _et_event_label(e.pop("at"))
    return out[:limit]


def _dashboard_context(con, request):
    """Dashboard context for a signed-in member, or None.

    Signed in = pick-link session cookie or staging pick cookie resolves
    to a members row.
    Rank includes the live week when it has data (same as the ticker).
    """
    token = _request_pick_token(request)
    if not token:
        return None
    m = con.execute(
        "SELECT * FROM members WHERE pick_token = ?", (token,)).fetchone()
    if not m:
        return None
    mid = m["id"]
    season = _db.current_season(con)
    pick_week = _db.current_pick_week(con, season)
    wrow = con.execute(
        "SELECT MAX(week) AS w FROM weekly_stats WHERE season = ?", (season,)).fetchone()
    try:
        lrow = con.execute(
            "SELECT MAX(week) AS w FROM live_stats WHERE season = ?", (season,)).fetchone()
        live_max = lrow["w"] or 0
    except Exception:
        live_max = 0
    through_week = max(wrow["w"] or 0, live_max)

    # Rank (same table the standings page would put them in).
    table_now, test_table = scoring.standings(con, season, through_week)
    table = test_table if m["is_test"] else table_now
    rank = total = None
    leader_name, leader_total = "", 0.0
    for i, (tmid, tname, ttotal) in enumerate(table):
        if i == 0:
            leader_name, leader_total = tname, ttotal
        if tmid == mid:
            rank, total = i + 1, ttotal

    # Season pie: per-player points across the ranked weeks.
    season_by_player = {}
    for w in range(1, through_week + 1):
        _, lines = scoring.member_week_points(con, mid, season, w)
        for pid, pname, pteam, _src, pts, _st in lines:
            d = season_by_player.setdefault(
                pid, {"name": pname, "team": pteam, "points": 0.0})
            d["points"] = round(d["points"] + pts, 2)
    season_pie = sorted(season_by_player.values(),
                        key=lambda d: d["points"], reverse=True)
    if len(season_pie) > 5:
        # Top 5 contributors only; everyone else folds into one "Other"
        # slice so the donut still adds up to the true season total.
        rest = round(sum(d["points"] for d in season_pie[5:]), 2)
        season_pie = season_pie[:5]
        if rest > 0:
            season_pie.append({"name": "Other", "team": "", "points": rest})

    # This week: picks with live game status (stints include live subs).
    stints = scoring.member_week_stints(con, mid, season, pick_week)
    week_total = round(sum(s["points"] for s in stints), 2)
    sub_rows = scoring.week_subs(con, mid, season, pick_week)
    subs_left = max(0, scoring.MAX_SUBS - len(sub_rows))
    states = _gamestate.fetch_states(pick_week)
    now = datetime.now(timezone.utc)
    picks, next_lock = [], None
    for st in stints:
        pid, pname, pteam = st["player_id"], st["name"], st["team"]
        pts = st["points"]
        game = _gamestate.player_game(pteam, states)
        if game is None:
            status, label = "bye", "Bye"
        elif game["state"] == "in":
            status, label = "live", _gamestate.live_label(game)
        elif game["state"] == "post":
            status, label = "final", game["detail"] or "Final"
        else:
            ko = _db.player_kickoff(con, pid, season, pick_week)
            label = _et_kickoff_label(ko) if ko else (game["detail"] or "Scheduled")
            status = "upcoming"
        prow = con.execute(
            "SELECT status FROM players WHERE player_id = ?", (pid,)).fetchone()
        picks.append({
            "name": pname, "team": pteam, "src": st["source"],
            "player_id": pid, "points": pts, "status": status, "label": label,
            "chip": scoring.status_chip(prow["status"] if prow else ""),
            "subbed_out": not st["active"],
            "out_label": st["sub"]["game_label"] if st["sub"] else None,
            "in_label": (st["opened_by"]["game_label"]
                         if st["opened_by"] else None),
            "can_sub": bool(st["active"] and status == "live"
                            and subs_left > 0),
        })
        ko = _db.player_kickoff(con, pid, season, pick_week)
        if ko:
            kdt = datetime.fromisoformat(ko)
            if kdt.tzinfo is None:
                kdt = kdt.replace(tzinfo=timezone.utc)
            if kdt > now and (next_lock is None or kdt < next_lock[0]):
                g = con.execute(
                    "SELECT away_team, home_team FROM games WHERE season = ? "
                    "AND week = ? AND (away_team = ? OR home_team = ?) LIMIT 1",
                    (season, pick_week, pteam, pteam)).fetchone()
                matchup = f"{g['away_team']} @ {g['home_team']}" if g else pteam
                next_lock = (kdt, {"matchup": matchup,
                                   "label": _et_kickoff_label(ko)})
    week_live = any(p["status"] == "live" for p in picks)
    week_pie = sorted(
        ({"name": p["name"], "team": p["team"], "points": p["points"]}
         for p in picks),
        key=lambda d: d["points"], reverse=True)

    # Top 5 performers league-wide this week.
    try:
        pids = [r["player_id"] for r in con.execute(
            "SELECT player_id FROM weekly_stats WHERE season = ? AND week = ? "
            "UNION SELECT player_id FROM live_stats WHERE season = ? AND week = ?",
            (season, pick_week, season, pick_week))]
    except Exception:
        pids = [r["player_id"] for r in con.execute(
            "SELECT player_id FROM weekly_stats WHERE season = ? AND week = ?",
            (season, pick_week))]
    perf = []
    for pid in pids:
        pts, _ = scoring.player_week_points(con, pid, season, pick_week)
        prow = con.execute(
            "SELECT name, team FROM players WHERE player_id = ?", (pid,)).fetchone()
        if prow:
            perf.append({"name": prow["name"], "team": prow["team"],
                         "points": pts})
    perf.sort(key=lambda r: (-r["points"], r["name"]))
    top5 = perf[:5]

    # Weekly trend.
    trend = []
    for w in range(1, pick_week + 1):
        t, _ = scoring.member_week_points(con, mid, season, w)
        trend.append({"week": w, "points": t})

    # Sub history for the MY PICKS header (banked = the closed stint's points).
    closed = {s["player_id"]: s for s in stints if not s["active"]}
    subs_hist = []
    for s in sub_rows:
        orow = con.execute("SELECT name FROM players WHERE player_id = ?",
                           (s["out_player_id"],)).fetchone()
        irow = con.execute("SELECT name FROM players WHERE player_id = ?",
                           (s["in_player_id"],)).fetchone()
        out_st = closed.get(s["out_player_id"])
        subs_hist.append({
            "label": s["game_label"] or "",
            "out_name": orow["name"] if orow else s["out_player_id"],
            "in_name": irow["name"] if irow else s["in_player_id"],
            "banked": out_st["points"] if out_st else 0.0})

    return {
        "member_name": m["name"], "season": season, "week": pick_week,
        "token": token, "member": m,
        "week_live": week_live, "rank": rank, "total": total,
        "leader_name": leader_name, "leader_total": leader_total,
        "gap": round(leader_total - (total or 0), 2),
        "season_pie": season_pie, "week_pie": week_pie,
        "week_total": week_total, "picks": picks,
        "top5": top5, "trend": trend,
        "subs_left": subs_left, "subs": subs_hist,
        "summary": _scoring_summary(con, mid, season, pick_week),
        "next_lock": next_lock[1] if next_lock else None,
        "ticker": _ticker_data(
            con, season,
            include_test=_is_staging_host(request.headers.get("host", ""))),
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    con = _db.connect()
    ctx = _dashboard_context(con, request)
    con.close()
    if ctx is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "dashboard.html", {"request": request, **ctx})


@app.get("/dashboard.json")
def dashboard_json(request: Request):
    """Live-refresh payload for the signed-in member's dashboard.

    Same auth as /dashboard (pick-link session cookie). Returns only the
    dynamic bits; the ticker has its own endpoint. 401 when signed out so
    the page's poller can stand down quietly.
    """
    con = _db.connect()
    ctx = _dashboard_context(con, request)
    con.close()
    if ctx is None:
        return JSONResponse({"detail": "Not signed in"}, status_code=401)
    return JSONResponse({
        "week": ctx["week"],
        "week_live": ctx["week_live"],
        "rank": ctx["rank"],
        "total": ctx["total"],
        "gap": ctx["gap"],
        "leader_name": ctx["leader_name"],
        "week_total": ctx["week_total"],
        "season_pie": ctx["season_pie"],
        "week_pie": ctx["week_pie"],
        "picks": ctx["picks"],
        "top5": ctx["top5"],
        "trend": ctx["trend"],
        "summary": ctx["summary"],
        "next_lock": ctx["next_lock"],
    })


@app.get("/scoring-events.json")
def scoring_events_json(request: Request, since: str = ""):
    """Recent scoring plays for picked players, for the notification popups.

    `since` is an ISO timestamp; only events after it are returned. Players
    are limited to this week's picked set (weekly picks plus sub-ins,
    league-wide). Each event carries mine=true when the viewer -- identified
    by the pick-link session / staging pick cookie -- has that player in an
    active stint and the play falls inside it (same rule as the scoring
    summary), so a subbed-out player's later plays never badge YOUR PICK.
    """
    con = _db.connect()
    try:
        season = _db.current_season(con)
        week = _db.current_pick_week(con, season)
        picked = {r["player_id"] for r in con.execute(
            "SELECT player_id FROM weekly_picks WHERE season = ? AND week = ? "
            "UNION SELECT in_player_id FROM weekly_subs "
            "WHERE season = ? AND week = ?",
            (season, week, season, week))}
        mine_stints = {}
        if picked:
            token = _request_pick_token(request)
            if token:
                m = con.execute(
                    "SELECT * FROM members WHERE pick_token = ?",
                    (token,)).fetchone()
                if m:
                    mine_stints = {
                        s["player_id"]: s for s in
                        scoring.member_week_stints(con, m["id"], season, week)}
        events = []
        if picked:
            q = ("SELECT e.*, p.name, p.team FROM scoring_events e "
                 "JOIN players p ON p.player_id = e.player_id "
                 "WHERE e.season = ? AND e.week = ?")
            args: list = [season, week]
            if since:
                q += " AND e.created_at > ?"
                args.append(since)
            q += " ORDER BY e.created_at ASC LIMIT 50"
            for r in con.execute(q, args):
                if r["player_id"] not in picked:
                    continue
                st = mine_stints.get(r["player_id"])
                mine = (st is not None and scoring.stint_counts_event(
                    st, r["stat_key"], r["new_total"]))
                events.append({
                    "player": r["name"],
                    "team": r["team"],
                    "stat": r["stat_key"],
                    "delta": round(r["new_total"] - r["old_total"], 2),
                    "points": r["points_delta"],
                    "at": r["created_at"],
                    "mine": mine,
                })
        return JSONResponse({"events": events, "now": _db.utcnow_iso()})
    finally:
        con.close()


def _enrich_table(table_now, table_prev, through_week):
    """Standings card dicts: rank, behind-leader, avg/week, weekly movement."""
    rank_prev = {mid: i + 1 for i, (mid, _, _) in enumerate(table_prev)}
    leader_total = table_now[0][2] if table_now else 0.0
    cards = []
    for i, (mid, name, total) in enumerate(table_now):
        rank = i + 1
        pr = rank_prev.get(mid)
        if pr is None:
            move_class, move_arrow, move_n = "move-same", "–", 0
        elif pr > rank:
            move_class, move_arrow, move_n = "move-up", f"▲{pr - rank}", pr - rank
        elif pr < rank:
            move_class, move_arrow, move_n = "move-down", f"▼{rank - pr}", pr - rank
        else:
            move_class, move_arrow, move_n = "move-same", "–", 0
        if move_n > 0:
            move_title = f"up {move_n} this week"
        elif move_n < 0:
            move_title = f"down {-move_n} this week"
        else:
            move_title = "no change this week"
        cards.append({
            "rank": rank, "name": name, "total": total,
            "move_class": move_class, "move_arrow": move_arrow,
            "move_title": move_title,
            "behind": round(leader_total - total, 2),
            "avg": round(total / through_week, 2) if through_week else 0.0,
        })
    return cards


def _standings_context(con, request):
    """Standings rows with behind-leader and avg/week, plus ticker."""
    season = _db.current_season(con)
    wrow = con.execute(
        "SELECT MAX(week) AS w FROM weekly_stats WHERE season = ?", (season,)
    ).fetchone()
    through_week = wrow["w"] or 0
    table_now, test_now = scoring.standings(con, season, through_week)
    if through_week > 1:
        table_prev, test_prev = scoring.standings(con, season, through_week - 1)
    else:
        table_prev, test_prev = [], []
    return {
        "season": season,
        "standings": _enrich_table(table_now, table_prev, through_week),
        "test_cards": _enrich_table(test_now, test_prev, through_week),
        "ticker": _ticker_data(
            con, season,
            include_test=_is_staging_host(request.headers.get("host", ""))),
        "through_week": through_week, "scoring": scoring.scoring_map(con),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    con = _db.connect()
    dash = _dashboard_context(con, request)
    if dash is not None:
        con.close()
        return templates.TemplateResponse(
            request, "dashboard.html", {"request": request, **dash})
    ctx = _standings_context(con, request)
    con.close()
    return templates.TemplateResponse(request, "index.html", {
        "request": request, **ctx,
    })


@app.get("/standings", response_class=HTMLResponse)
def standings_page(request: Request):
    """The standings, always -- even for signed-in members."""
    con = _db.connect()
    ctx = _standings_context(con, request)
    con.close()
    return templates.TemplateResponse(request, "index.html", {
        "request": request, **ctx,
    })


@app.get("/week/{season}/{week}", response_class=HTMLResponse)
def week_view(request: Request, season: int, week: int):
    con = _db.connect()
    members = con.execute("SELECT id, name FROM members ORDER BY name").fetchall()
    detail = []
    for m in members:
        total, lines = scoring.member_week_points(con, m["id"], season, week)
        detail.append((m["name"], total, lines))
    detail.sort(key=lambda r: r[1], reverse=True)
    stat_keys = scoring.STAT_KEYS
    ticker = _ticker_data(con, season, include_test=_is_staging_host(request.headers.get("host", "")))
    con.close()
    return templates.TemplateResponse(request, "week.html", {
        "request": request, "season": season, "week": week,
        "detail": detail, "stat_keys": stat_keys, "ticker": ticker,
    })


def _et_kickoff_label(ko_iso: str) -> str:
    """'2026-10-11T20:20Z' -> 'Sun 8:20 PM' (America/New_York)."""
    try:
        from zoneinfo import ZoneInfo
        ko = datetime.fromisoformat(ko_iso)
        if ko.tzinfo is None:
            ko = ko.replace(tzinfo=timezone.utc)
        return ko.astimezone(ZoneInfo("America/New_York")).strftime("%a %-I:%M %p")
    except Exception:
        return ""


def _et_stamp(iso_utc: str) -> str:
    """'2026-10-13T18:37:49.646235+00:00' -> 'Tue 10/13 2:37 PM' (America/New_York)."""
    if not iso_utc:
        return "—"
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(iso_utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%a %-m/%-d %-I:%M %p")
    except Exception:
        return iso_utc


@app.get("/live", response_class=HTMLResponse)
def live_view(request: Request):
    """Who's playing right now: every picked player with their game status
    (live quarter, final, upcoming kickoff, or bye)."""
    con = _db.connect()
    season = _db.current_season(con)
    week = _db.current_pick_week(con, season)
    states = _gamestate.fetch_states(week)
    feed_down = not states
    members = []
    live_count = 0
    for m in con.execute("SELECT id, name FROM members ORDER BY name"):
        total, lines = scoring.member_week_points(con, m["id"], season, week)
        picks = []
        for pid, pname, pteam, src, pts, _stats in lines:
            game = _gamestate.player_game(pteam, states)
            if game is None:
                status, label = ("bye", "Bye")
            elif game["state"] == "in":
                status, label = ("live", _gamestate.live_label(game))
                live_count += 1
            elif game["state"] == "post":
                status, label = ("final", game["detail"] or "Final")
            else:
                ko = _db.player_kickoff(con, pid, season, week)
                label = _et_kickoff_label(ko) if ko else (game["detail"] or "Scheduled")
                status = "upcoming"
            prow = con.execute(
                "SELECT status FROM players WHERE player_id = ?",
                (pid,)).fetchone()
            picks.append({
                "name": pname, "team": pteam, "src": src,
                "points": pts, "status": status, "label": label,
                "chip": scoring.status_chip(prow["status"] if prow else ""),
            })
        members.append({"name": m["name"], "total": total, "picks": picks})
    members.sort(key=lambda e: e["total"], reverse=True)
    ticker = _ticker_data(con, season, include_test=_is_staging_host(request.headers.get("host", "")))
    con.close()
    return templates.TemplateResponse(request, "live.html", {
        "request": request, "season": season, "week": week,
        "members": members, "ticker": ticker,
        "live_count": live_count, "feed_down": feed_down,
    })


@app.get("/guide", response_class=HTMLResponse)
def guide(request: Request):
    return templates.TemplateResponse(request, "guide.html", {"request": request})


@app.get("/releases", response_class=HTMLResponse)
def releases_page(request: Request):
    """Release notes for every version, newest first."""
    return templates.TemplateResponse(request, "releases.html", {
        "request": request, "releases": RELEASES,
    })


def _et_news_stamp(iso_ts) -> str:
    """'Thu Oct 22 · 11:50 PM ET' for a League News timestamp."""
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York")).strftime(
            "%a %b %-d · %-I:%M %p ET")
    except Exception:
        return ""


_CORRECTION_STAT_LABELS = {
    "sacks": "sacks", "interceptions": "interceptions",
    "forced_fumbles": "forced fumbles",
    "fumble_recoveries": "fumble recoveries",
    "safeties": "safeties", "def_tds": "defensive TDs",
}


def _corrections_for_window(con, season: int, week: int, wkey_str: str):
    """Correction blocks for one published window, oldest first.

    Read-time only: the stored post is never rewritten. Each block groups
    one player's corrections in the window — the stat changes plus the
    player's total old → new (current total minus the summed deltas;
    exact when nothing else changed for that player since, which is the
    normal overturn case). Owners are the members with a stint for that
    player this week. Never breaks the post: any failure yields no blocks.
    """
    try:
        rows = con.execute(
            "SELECT c.*, p.name AS pname FROM stat_corrections c "
            "JOIN players p ON p.player_id = c.player_id "
            "WHERE c.season = ? AND c.week = ? ORDER BY c.detected_at",
            (season, week)).fetchall()
    except Exception:
        return []
    blocks = []
    try:
        by_player = {}
        for r in rows:
            ko = _db.player_kickoff(con, r["player_id"], season, week)
            got = _window_key(ko) if ko else None
            if not got or _window_key_str(got[0]) != wkey_str:
                continue
            by_player.setdefault(r["player_id"], []).append(r)
        for pid, rs in by_player.items():
            pname = rs[0]["pname"]
            total_delta = round(sum(r["points_delta"] for r in rs), 2)
            try:
                cur, _ = scoring.player_week_points(con, pid, season, week)
            except Exception:
                cur = 0.0
            old_total = round(cur - total_delta, 2)
            owners = set()
            for m in con.execute("SELECT id, name FROM members"):
                try:
                    stints = scoring.member_week_stints(
                        con, m["id"], season, week)
                except Exception:
                    continue
                if any(s["player_id"] == pid for s in stints):
                    owners.add(m["name"])
            stamp = _et_news_stamp(rs[0]["detected_at"])
            lines = [f"Correction — {stamp}:" if stamp else "Correction:"]
            for r in rs:
                label = _CORRECTION_STAT_LABELS.get(
                    r["stat_key"], r["stat_key"])
                line = (
                    f"• {pname} — {label} {r['old_total']:g} → "
                    f"{r['new_total']:g} ({r['points_delta']:+.1f} pts); "
                    f"total {old_total:.1f} → {cur:.1f}")
                if owners:
                    line += f" · picked by {', '.join(sorted(owners))}"
                lines.append(line)
            blocks.append(lines)
    except Exception:
        return []
    return blocks


def _news_post_view(con, r) -> dict:
    comments = con.execute(
        "SELECT c.body, c.created_at, m.name AS author FROM news_comments c "
        "JOIN members m ON m.id = c.member_id WHERE c.post_id = ? ORDER BY c.id",
        (r["id"],)).fetchall()
    blocks = [b.split("\n") for b in r["body"].split("\n\n")] \
        if r["body"] else []
    updated = False
    if r["kind"] == "auto" and r["window_key"]:
        try:
            corr = _corrections_for_window(
                con, r["season"], r["week"], r["window_key"])
        except Exception:
            corr = []
        if corr:
            blocks.extend(corr)
            updated = True
    return {
        "id": r["id"], "headline": r["headline"], "author": r["author"],
        "kind": r["kind"], "when": _et_news_stamp(r["created_at"]),
        "blocks": blocks, "updated": updated,
        "comments": [{"author": c["author"], "body": c["body"],
                      "when": _et_news_stamp(c["created_at"])}
                     for c in comments],
    }


@app.get("/news", response_class=HTMLResponse)
def news_page(request: Request):
    """League News: auto recaps for each set of games (the ticker headline
    links here), commissioner posts (rule changes, announcements), and
    member replies on every post."""
    con = _db.connect()
    season = _db.current_season(con)
    ticker = _ticker_data(
        con, season,
        include_test=_is_staging_host(request.headers.get("host", "")))
    member = None
    tok = _request_pick_token(request)
    if tok:
        member = con.execute(
            "SELECT * FROM members WHERE pick_token = ?", (tok,)).fetchone()
    posts = []
    try:
        rows = con.execute(
            "SELECT * FROM news_posts WHERE season = ? "
            "ORDER BY created_at DESC, id DESC", (season,)).fetchall()
        posts = [_news_post_view(con, r) for r in rows]
    except Exception:
        posts = []  # news tables predate this deploy
    con.close()
    return templates.TemplateResponse(request, "news.html", {
        "request": request, "ticker": ticker, "posts": posts,
        "member": member, "season": season})


@app.post("/news/{post_id}/reply")
def news_reply(request: Request, post_id: int, body: str = Form("")):
    tok = _request_pick_token(request)
    if not tok:
        raise HTTPException(403, "open your pick link to reply")
    body = (body or "").strip()[:2000]
    if not body:
        return RedirectResponse(f"/news#post-{post_id}", status_code=303)
    con = _db.connect()
    member = con.execute(
        "SELECT * FROM members WHERE pick_token = ?", (tok,)).fetchone()
    post = con.execute(
        "SELECT id FROM news_posts WHERE id = ?", (post_id,)).fetchone()
    if member is None or post is None:
        con.close()
        raise HTTPException(404, "not found")
    con.execute(
        "INSERT INTO news_comments (post_id, member_id, body, created_at) "
        "VALUES (?, ?, ?, ?)",
        (post_id, member["id"], body,
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    con.commit()
    con.close()
    return RedirectResponse(f"/news#post-{post_id}", status_code=303)


@app.get("/admin/{token}/news", response_class=HTMLResponse)
def admin_news(request: Request, token: str):
    if not _admin_ok(token):
        raise HTTPException(404, "not found")
    con = _db.connect()
    season = _db.current_season(con)
    ticker = _ticker_data(
        con, season,
        include_test=_is_staging_host(request.headers.get("host", "")))
    posts = []
    try:
        for r in con.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM news_comments c "
                "WHERE c.post_id = p.id) AS replies FROM news_posts "
                "WHERE season = ? ORDER BY created_at DESC, id DESC",
                (season,)):
            posts.append({"id": r["id"], "headline": r["headline"],
                          "kind": r["kind"], "author": r["author"],
                          "replies": r["replies"],
                          "when": _et_news_stamp(r["created_at"])})
    except Exception:
        posts = []  # news tables predate this deploy
    con.close()
    return templates.TemplateResponse(request, "admin_news.html", {
        "request": request, "token": token, "season": season,
        "posts": posts, "ticker": ticker})


@app.post("/admin/{token}/news")
def admin_news_create(token: str, headline: str = Form(""),
                      body: str = Form("")):
    if not _admin_ok(token):
        raise HTTPException(404, "not found")
    headline = (headline or "").strip()[:200]
    body = (body or "").strip()[:8000]
    if not headline:
        raise HTTPException(400, "headline required")
    con = _db.connect()
    season = _db.current_season(con)
    week = _db.current_pick_week(con, season)
    con.execute(
        "INSERT INTO news_posts (season, week, window_key, kind, headline, "
        "body, author, created_at) VALUES (?, ?, NULL, 'admin', ?, ?, "
        "'Commissioner', ?)",
        (season, week, headline, body,
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    con.commit()
    con.close()
    return RedirectResponse(f"/admin/{token}/news", status_code=303)


@app.post("/admin/{token}/news/{post_id}/delete")
def admin_news_delete(token: str, post_id: int):
    if not _admin_ok(token):
        raise HTTPException(404, "not found")
    con = _db.connect()
    row = con.execute(
        "SELECT * FROM news_posts WHERE id = ?", (post_id,)).fetchone()
    if row is not None:
        if row["window_key"]:
            # Tombstone so the generator doesn't resurrect an auto post.
            con.execute(
                "INSERT OR IGNORE INTO news_deleted_windows "
                "(season, week, window_key) VALUES (?, ?, ?)",
                (row["season"], row["week"], row["window_key"]))
        con.execute("DELETE FROM news_comments WHERE post_id = ?", (post_id,))
        con.execute("DELETE FROM news_posts WHERE id = ?", (post_id,))
        con.commit()
    con.close()
    return RedirectResponse(f"/admin/{token}/news", status_code=303)


@app.get("/submit/{token}", response_class=HTMLResponse)
def submit_form(request: Request, token: str):
    """Personal pick link: establish the member's session, then send them
    to their dashboard (the member home page). Picks are made on
    /projections; reminder settings live on the dashboard.
    """
    con = _db.connect()
    m = con.execute("SELECT * FROM members WHERE pick_token = ?", (token,)).fetchone()
    con.close()
    if not m:
        raise HTTPException(404, "bad pick link")
    resp = RedirectResponse("/dashboard", status_code=302)
    _establish_session(resp, request, m)
    # Staging fallback: opening the pick link unlocks staging even when
    # the shared session secret isn't configured on the server.
    _set_staging_pick_cookie(resp, request, token)
    return resp


@app.get("/pick", response_class=HTMLResponse)
def pick_redirect(request: Request):
    """Cross-game entry point: resolve the session / staging pick cookie
    to this member's Prime5 pick link. Additive only -- existing links untouched."""
    token = _request_pick_token(request)
    if not token:
        raise HTTPException(404, "no Ravix session -- open your pick link first")
    return RedirectResponse(f"/submit/{token}", status_code=302)


@app.post("/submit/{token}/reminders")
def save_reminders(token: str, phone: str = Form(""), sms_opt_in: str = Form(""),
                   email: str = Form(""), email_opt_in: str = Form("")):
    """Save a member's SMS/email reminder settings from their dashboard."""
    con = _db.connect()
    m = con.execute("SELECT * FROM members WHERE pick_token = ?", (token,)).fetchone()
    if not m:
        con.close()
        raise HTTPException(404, "bad pick link")
    phone = (phone or "").strip()
    opt_in = 1 if sms_opt_in == "1" else 0
    e164 = _sms.normalize_phone(phone) if phone else None
    if opt_in and phone and not e164:
        con.close()
        return RedirectResponse(f"/dashboard?reminders=badphone", status_code=303)
    email = (email or "").strip()
    email_opt = 1 if email_opt_in == "1" else 0
    clean_email = _emailer.valid_email(email) if email else None
    if email_opt and email and not clean_email:
        con.close()
        return RedirectResponse(f"/dashboard?reminders=bademail", status_code=303)
    con.execute(
        "UPDATE members SET phone = ?, sms_opt_in = ?, email = ?, email_opt_in = ? WHERE id = ?",
        (e164, opt_in, clean_email, email_opt, m["id"]),
    )
    con.commit()
    con.close()
    return RedirectResponse(f"/dashboard?reminders=saved", status_code=303)


# ---- Live substitutions (Week 7 rule) ----

def _log_sub_event(con, m, season, week, event, out_pid="", in_pid="",
                   detail=""):
    """Record one step of sub-flow engagement (admin report funnel).

    Best-effort: a logging failure must never break the member's flow.
    """
    try:
        con.execute(
            "INSERT INTO sub_events (member_id, season, week, event, "
            "out_player_id, in_player_id, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (m["id"], season, week, event, out_pid or "", in_pid or "",
             detail or "", _db.utcnow_iso()))
        con.commit()
    except Exception:
        pass

def _sub_game_live(con, prow, season, week, states):
    """(is_live, clock_label) for a player's game right now.

    ESPN state wins when the feed has the game; otherwise fall back to
    the kickoff window (started, less than 4 hours ago), like /live.
    """
    game = _gamestate.player_game(prow["team"], states) if states else None
    if game is not None:
        if game["state"] == "in":
            return True, _gamestate.live_label(game)
        return False, None
    ko = _db.player_kickoff(con, prow["player_id"], season, week)
    if not ko:
        return False, None
    kdt = datetime.fromisoformat(ko)
    if kdt.tzinfo is None:
        kdt = kdt.replace(tzinfo=timezone.utc)
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    return (kdt <= now < kdt + timedelta(hours=4)), None


def _validate_sub(con, m, season, week, out_pid, in_pid, states):
    """Validate a proposed sub. Returns (err, info); err is None on success.

    info: {out_row, in_row, label, out_stats, in_stats, out_pts, in_pts,
    subs_left}. Used by the confirm page (display) and by apply
    (authoritative re-check at write time).
    """
    subs = scoring.week_subs(con, m["id"], season, week)
    if len(subs) >= scoring.MAX_SUBS:
        return "You've used both subs this week", None
    stints = scoring.member_week_stints(con, m["id"], season, week)
    active = {s["player_id"]: s for s in stints if s["active"]}
    seen = {s["player_id"] for s in stints}
    out_stint = active.get(out_pid)
    if out_stint is None:
        return "That player isn't in your active lineup", None
    if in_pid in seen:
        return "That player is already in (or was in) your lineup this week", None
    in_row = con.execute(
        "SELECT * FROM players WHERE player_id = ? "
        "AND position_group IN ('DB','DL','LB')", (in_pid,)).fetchone()
    if not in_row:
        return "Replacement player not found", None
    out_row = con.execute(
        "SELECT * FROM players WHERE player_id = ?", (out_pid,)).fetchone()
    ko_out = _db.player_kickoff(con, out_pid, season, week)
    ko_in = _db.player_kickoff(con, in_pid, season, week)
    if not ko_out or not ko_in or ko_out != ko_in:
        return "The replacement must come from the same kickoff window", None
    live_out, label = _sub_game_live(con, out_row, season, week, states)
    live_in, _ = _sub_game_live(con, in_row, season, week, states)
    if not (live_out and live_in):
        return "Subs are only allowed while the game is live", None
    _, out_stats = scoring.player_week_points(con, out_pid, season, week)
    in_pts, in_stats = scoring.player_week_points(con, in_pid, season, week)
    return None, {
        "out_row": out_row, "in_row": in_row, "label": label,
        "out_stats": out_stats, "in_stats": in_stats,
        "out_pts": out_stint["points"], "in_pts": in_pts,
        "subs_left": scoring.MAX_SUBS - len(subs),
    }


def _sub_member_or_404(con, token):
    m = con.execute("SELECT * FROM members WHERE pick_token = ?",
                    (token,)).fetchone()
    if not m:
        raise HTTPException(404, "bad pick link")
    return m


@app.get("/submit/{token}/sub", response_class=HTMLResponse)
def sub_choose(request: Request, token: str, out: str = "",
               err: str = ""):
    """Sub step 1: choose the replacement (same kickoff window, live only)."""
    con = _db.connect()
    m = _sub_member_or_404(con, token)
    season = _db.current_season(con)
    week = _db.current_pick_week(con, season)
    states = _gamestate.fetch_states(week)
    stints = scoring.member_week_stints(con, m["id"], season, week)
    active = {s["player_id"]: s for s in stints if s["active"]}
    seen = {s["player_id"] for s in stints}
    subs_used = len(scoring.week_subs(con, m["id"], season, week))
    out_stint = active.get(out)
    options, out_info = [], None
    if subs_used >= scoring.MAX_SUBS:
        err = err or "You've used both subs this week."
    elif out_stint is None:
        err = err or "That player isn't in your active lineup."
    else:
        out_row = con.execute("SELECT * FROM players WHERE player_id = ?",
                              (out,)).fetchone()
        live, label = _sub_game_live(con, out_row, season, week, states)
        if not live:
            err = err or "Subs are only allowed while the game is live."
        else:
            ko = _db.player_kickoff(con, out, season, week)
            ko_by_team = {}
            for g in con.execute(
                    "SELECT away_team, home_team, kickoff_utc FROM games "
                    "WHERE season = ? AND week = ?", (season, week)):
                ko_by_team[g["away_team"]] = g["kickoff_utc"]
                ko_by_team[g["home_team"]] = g["kickoff_utc"]
            out_info = {"name": out_stint["name"], "team": out_stint["team"],
                        "points": out_stint["points"], "label": label,
                        "player_id": out}
            for c in con.execute(
                    "SELECT * FROM players "
                    "WHERE position_group IN ('DB','DL','LB')").fetchall():
                if c["player_id"] in seen:
                    continue
                if ko_by_team.get(c["team"]) != ko:
                    continue
                clive, _ = _sub_game_live(con, c, season, week, states)
                if not clive:
                    continue
                pts, stats = scoring.player_week_points(
                    con, c["player_id"], season, week)
                options.append({"player_id": c["player_id"],
                                "name": c["name"], "team": c["team"],
                                "position": c["position"],
                                "points": pts, "stats": stats})
            options.sort(key=lambda o: (-o["points"], o["name"]))
    if out_info is not None:
        _log_sub_event(con, m, season, week, "choose", out_pid=out)
    ticker = _ticker_data(
        con, season,
        include_test=_is_staging_host(request.headers.get("host", "")))
    con.close()
    return templates.TemplateResponse(request, "sub.html", {
        "request": request, "token": token, "member_name": m["name"],
        "week": week, "step": "choose", "err": err or None,
        "out": out_info, "options": options,
        "subs_left": scoring.MAX_SUBS - subs_used, "ticker": ticker})


@app.post("/submit/{token}/sub/review", response_class=HTMLResponse)
def sub_review(request: Request, token: str, out: str = Form(""),
               inn: str = Form("")):
    """Sub step 2: confirm the swap (subs can't be undone)."""
    con = _db.connect()
    m = _sub_member_or_404(con, token)
    season = _db.current_season(con)
    week = _db.current_pick_week(con, season)
    states = _gamestate.fetch_states(week)
    err, info = _validate_sub(con, m, season, week, out, inn, states)
    if err:
        con.close()
        from urllib.parse import quote
        return RedirectResponse(
            f"/submit/{token}/sub?out={out}&err={quote(err)}",
            status_code=303)
    _log_sub_event(con, m, season, week, "review", out_pid=out, in_pid=inn)
    ticker = _ticker_data(
        con, season,
        include_test=_is_staging_host(request.headers.get("host", "")))
    con.close()
    return templates.TemplateResponse(request, "sub.html", {
        "request": request, "token": token, "member_name": m["name"],
        "week": week, "step": "confirm", "err": None,
        "out": {"name": info["out_row"]["name"],
                "team": info["out_row"]["team"],
                "points": info["out_pts"], "player_id": out},
        "inn": {"name": info["in_row"]["name"],
                "team": info["in_row"]["team"],
                "position": info["in_row"]["position"],
                "points": info["in_pts"], "player_id": inn},
        "subs_left": info["subs_left"], "ticker": ticker})


@app.post("/submit/{token}/sub/apply")
def sub_apply(token: str, out: str = Form(""), inn: str = Form("")):
    """Sub step 3: write the sub (re-validated at write time)."""
    con = _db.connect()
    m = _sub_member_or_404(con, token)
    season = _db.current_season(con)
    week = _db.current_pick_week(con, season)
    states = _gamestate.fetch_states(week)
    err, info = _validate_sub(con, m, season, week, out, inn, states)
    if err:
        _log_sub_event(con, m, season, week, "rejected", out_pid=out,
                       in_pid=inn, detail=err)
        con.close()
        from urllib.parse import quote
        return RedirectResponse(f"/dashboard?suberr={quote(err)}",
                                status_code=303)
    con.execute(
        "INSERT INTO weekly_subs (member_id, season, week, out_player_id, "
        "in_player_id, subbed_at, game_label, out_stats, in_stats) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (m["id"], season, week, out, inn, _db.utcnow_iso(), info["label"],
         json.dumps(info["out_stats"]), json.dumps(info["in_stats"])))
    con.execute(
        "INSERT INTO sub_events (member_id, season, week, event, "
        "out_player_id, in_player_id, detail, created_at) "
        "VALUES (?,?,?,'applied',?,?,?,?)",
        (m["id"], season, week, out, inn, "", _db.utcnow_iso()))
    con.commit()
    con.close()
    return RedirectResponse("/dashboard?subbed=1", status_code=303)


# ---------------- admin ----------------

@app.get("/admin/{token}", response_class=HTMLResponse)
def admin_home(request: Request, token: str):
    if not _admin_ok(token):
        raise HTTPException(404, "not found")
    con = _db.connect()
    season = _db.current_season(con)
    members = con.execute("SELECT * FROM members ORDER BY name").fetchall()
    cfg = con.execute("SELECT * FROM scoring_config").fetchall()
    ticker = _ticker_data(con, season, include_test=_is_staging_host(request.headers.get("host", "")))
    con.close()
    return templates.TemplateResponse(request, "admin.html", {
        "request": request, "token": token, "season": season,
        "members": members, "scoring": cfg, "ticker": ticker,
    })


@app.post("/admin/{token}/members/add")
def admin_add_member(token: str, name: str = Form(""), is_test: int = Form(0)):
    if not _admin_ok(token):
        raise HTTPException(404, "not found")
    name = name.strip()
    if not name:
        raise HTTPException(400, "name required")
    con = _db.connect()
    pt = secrets.token_urlsafe(16)
    con.execute(
        "INSERT INTO members (name, pick_token, is_test) VALUES (?,?,?)",
        (name, pt, 1 if is_test else 0),
    )
    con.commit()
    con.close()
    return RedirectResponse(f"/admin/{token}", status_code=303)


@app.post("/admin/{token}/scoring")
def admin_scoring(token: str, stat_key: str = Form(""), points: float = Form(0)):
    if not _admin_ok(token):
        raise HTTPException(404, "not found")
    if stat_key not in scoring.STAT_KEYS:
        raise HTTPException(400, "unknown stat")
    con = _db.connect()
    con.execute("UPDATE scoring_config SET points=? WHERE stat_key=?", (points, stat_key))
    con.commit()
    con.close()
    return RedirectResponse(f"/admin/{token}", status_code=303)


@app.get("/admin/{token}/participation", response_class=HTMLResponse)
def admin_participation(request: Request, token: str, week: int = 0):
    if not _admin_ok(token):
        raise HTTPException(404, "not found")
    con = _db.connect()
    season = _db.current_season(con)
    weeks = [r["week"] for r in con.execute(
        "SELECT DISTINCT week FROM weekly_picks WHERE season=? ORDER BY week", (season,))]
    if week not in weeks:
        week = weeks[-1] if weeks else 0
    rows = []
    if week:
        for m in con.execute("SELECT * FROM members ORDER BY name").fetchall():
            cnt, first, last = con.execute(
                "SELECT COUNT(*), MIN(submitted_at), MAX(submitted_at) FROM weekly_picks "
                "WHERE member_id=? AND season=? AND week=?",
                (m["id"], season, week)).fetchone()
            sms = con.execute(
                "SELECT sent_at FROM sms_reminders WHERE member_id=? AND season=? AND week=?",
                (m["id"], season, week)).fetchone()
            email = con.execute(
                "SELECT sent_at FROM email_reminders WHERE member_id=? AND season=? AND week=?",
                (m["id"], season, week)).fetchone()
            rows.append({
                "name": m["name"], "is_test": m["is_test"],
                "sms_opt_in": m["sms_opt_in"], "email_opt_in": m["email_opt_in"],
                "picks": cnt,
                "first": _et_stamp(first), "last": _et_stamp(last),
                "sms_sent": _et_stamp(sms["sent_at"]) if sms else "",
                "email_sent": _et_stamp(email["sent_at"]) if email else "",
            })
    # Substitution engagement for the selected week (Week 7 feature).
    sub_rows, sub_summary = [], None
    if week:
        funnel = {"choose": 0, "review": 0, "applied": 0, "rejected": 0}
        try:
            for r in con.execute(
                    "SELECT event, COUNT(*) c FROM sub_events "
                    "WHERE season=? AND week=? GROUP BY event",
                    (season, week)):
                if r["event"] in funnel:
                    funnel[r["event"]] = r["c"]
        except Exception:
            pass  # sub_events predates this deploy
        total_subs, members_with = 0, 0
        for m in con.execute("SELECT * FROM members ORDER BY name").fetchall():
            outs = scoring.sub_outcomes(con, m["id"], season, week)
            season_used = con.execute(
                "SELECT COUNT(*) c FROM weekly_subs "
                "WHERE member_id=? AND season=?",
                (m["id"], season)).fetchone()["c"]
            for o in outs:
                o["when"] = _et_stamp(o["subbed_at"])
            total_subs += len(outs)
            members_with += 1 if outs else 0
            sub_rows.append({
                "name": m["name"], "is_test": m["is_test"],
                "used": len(outs), "subs": outs,
                "net": round(sum(o["net"] for o in outs), 2),
                "season_used": season_used})
        sub_summary = {"total_subs": total_subs,
                       "members_with": members_with,
                       "members": len(sub_rows), "funnel": funnel}
    con.close()
    return templates.TemplateResponse(request, "participation.html", {
        "request": request, "token": token, "season": season,
        "weeks": weeks, "week": week, "rows": rows,
        "sub_rows": sub_rows, "sub_summary": sub_summary,
    })


# ---------------- projections + pick-from-report ----------------

PROJ_SORTS = {
    "proj": (lambda d: d["proj_pts"], True),
    "adj": (lambda d: d["adj_pts"], True),
    "player": (lambda d: d["name"].lower(), False),
    "team": (lambda d: d["team"], False),
    "pos": (lambda d: d["position"], False),
    "tkl": (lambda d: d["avg"]["tkl"], True),
    "sack": (lambda d: d["avg"]["sacks"], True),
    "ff": (lambda d: d["avg"]["ff"], True),
    "fr": (lambda d: d["avg"]["fr"], True),
    "int": (lambda d: d["avg"]["ints"], True),
    "td": (lambda d: d["avg"]["tds"], True),
    "season": (lambda d: d["season_pts"], True),
}


def _ko_started(ko_iso):
    if not ko_iso:
        return False
    ko = datetime.fromisoformat(ko_iso)
    if ko.tzinfo is None:
        ko = ko.replace(tzinfo=timezone.utc)
    return ko <= datetime.now(timezone.utc)


def _projections_context(con, request, token="", sort="proj", dir="",
                         pos="", q=""):
    season = _db.current_season(con)
    week = _db.current_pick_week(con, season)
    through = week - 1
    member = None
    if token:
        member = con.execute("SELECT * FROM members WHERE pick_token = ?",
                             (token,)).fetchone()
    rows, picks, picked_ids = [], [], set()
    if through >= 1:
        rows = scoring.projections(con, season, through)
        if pos in ("DB", "DL", "LB"):
            rows = [r for r in rows if r["position_group"] == pos]
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in r["name"].lower()]
        matchup.annotate_rows(con, season, week, through, rows,
                              scoring.scoring_map(con))
        keyfn, default_desc = PROJ_SORTS.get(sort, PROJ_SORTS["proj"])
        desc = (dir == "desc") if dir in ("asc", "desc") else default_desc
        rows.sort(key=keyfn, reverse=desc)
    if member:
        picks = con.execute(
            "SELECT wp.player_id, p.name, p.team FROM weekly_picks wp "
            "JOIN players p ON p.player_id = wp.player_id "
            "WHERE wp.member_id = ? AND wp.season = ? AND wp.week = ?",
            (member["id"], season, week)).fetchall()
        picked_ids = {p["player_id"] for p in picks}
    kos = {r["home_team"]: r["kickoff_utc"] for r in con.execute(
        "SELECT home_team, kickoff_utc FROM games WHERE season=? AND week=?",
        (season, week))}
    kos.update({r["away_team"]: r["kickoff_utc"] for r in con.execute(
        "SELECT away_team, kickoff_utc FROM games WHERE season=? AND week=?",
        (season, week))})
    # per-row pick state for the template
    for r in rows:
        pid = r["player_id"]
        r["chip"] = scoring.status_chip(r["status"])
        if pid in picked_ids:
            r["state"] = "picked"
        else:
            ko = kos.get(r["team"])
            if ko and _ko_started(ko):
                r["state"] = "started"
            elif not ko:
                r["state"] = "bye"
            else:
                r["state"] = "open"
    pick_kos = []
    for p in picks:
        ko = _db.player_kickoff(con, p["player_id"], season, week)
        pick_kos.append({"name": p["name"], "team": p["team"],
                         "player_id": p["player_id"],
                         "started": _ko_started(ko) if ko else False})
    return {
        "season": season, "week": week, "through": through,
        "rows": rows, "member": member,
        "picks": pick_kos,
        "scoring": scoring.scoring_map(con),
        "sort": sort if sort in PROJ_SORTS else "proj",
        "dir": dir if dir in ("asc", "desc") else "",
        "pos": pos, "q": q, "token": token,
        "sort_links": _sort_links(token, sort, dir, pos, q),
    }


@app.get("/projections", response_class=HTMLResponse)
def projections_page(request: Request, token: str = "", sort: str = "proj",
                     dir: str = "", pos: str = "", q: str = ""):
    if not token:
        # Member opened their personal pick link earlier? The session
        # cookie (or staging pick cookie) identifies them, so they don't
        # have to paste their token.
        token = _request_pick_token(request) or ""
    con = _db.connect()
    ctx = _projections_context(con, request, token, sort, dir, pos, q)
    con.close()
    return templates.TemplateResponse(request, "projections.html", {
        "request": request, **ctx,
        "notice": request.query_params.get("notice", ""),
    })


SORT_LABELS = [
    ("player", "Player"), ("team", "Team"), ("pos", "Pos"), ("proj", "Proj"),
    ("adj", "Adj"),
    ("tkl", "Tkl/g"), ("sack", "Sack/g"), ("ff", "FF/g"), ("fr", "FR/g"),
    ("int", "INT/g"), ("td", "TD/g"), ("season", "Season"),
]


@app.get("/player/{player_id}.json")
def player_card(player_id: str):
    """Player popup data: info, injury/bye warnings, per-week game stats."""
    con = _db.connect()
    season = _db.current_season(con)
    p = con.execute(
        "SELECT player_id, name, team, position, position_group, status "
        "FROM players WHERE player_id = ?", (player_id,)
    ).fetchone()
    if not p:
        con.close()
        raise HTTPException(status_code=404, detail="unknown player")
    status = (p["status"] or "").upper()
    warnings = []
    if status and status != "ACT":
        label = {"RES": "On reserve (IR/out)", "INA": "Inactive"}.get(status, status)
        warnings.append({"kind": "injury", "text": label})
    # Bye weeks: weeks 1-18 where the player's team has no scheduled game.
    team_weeks = {r["week"] for r in con.execute(
        "SELECT week FROM games WHERE season = ? AND (home_team = ? OR away_team = ?)",
        (season, p["team"], p["team"])).fetchall()}
    cur_pick = _db.current_pick_week(con, season)
    byes = [w for w in range(1, 19) if w not in team_weeks and w >= cur_pick]
    for w in byes:
        warnings.append({"kind": "bye", "text": f"Bye week {w}"})
    cfg = scoring.scoring_map(con)
    weeks = []
    for r in con.execute(
        "SELECT * FROM weekly_stats WHERE player_id = ? AND season = ? ORDER BY week",
        (player_id, season)).fetchall():
        stats = {k: (r[k] or 0) for k in scoring.STAT_KEYS}
        pts = sum(stats[k] * cfg.get(k, 0) for k in scoring.STAT_KEYS)
        weeks.append({"week": r["week"], "stats": stats, "points": round(pts, 1)})
    con.close()
    return JSONResponse({
        "player_id": p["player_id"], "name": p["name"], "team": p["team"],
        "position": p["position"], "position_group": p["position_group"],
        "status": p["status"], "warnings": warnings, "weeks": weeks,
    })


def _sort_links(token, sort, dir, pos, q):
    from urllib.parse import urlencode
    links = []
    for key, label in SORT_LABELS:
        _, default_desc = PROJ_SORTS[key]
        cur = dir if dir in ("asc", "desc") else ("desc" if default_desc else "asc")
        if sort == key:
            ndir = "asc" if cur == "desc" else "desc"
            arrow, active = (" ▼" if cur == "desc" else " ▲"), True
        else:
            ndir, arrow, active = ("desc" if default_desc else "asc"), "", False
        params = {"token": token, "sort": key, "dir": ndir,
                  "pos": pos, "q": q}
        links.append({"label": label, "url": "/projections?" + urlencode(params),
                      "arrow": arrow, "active": active})
    return links


def _proj_redirect(token, sort, dir, pos, q, notice=""):
    from urllib.parse import urlencode
    params = {"token": token, "sort": sort, "dir": dir,
              "pos": pos, "q": q}
    if notice:
        params["notice"] = notice
    return RedirectResponse("/projections?" + urlencode(params),
                            status_code=303)


@app.post("/projections/pick", response_class=HTMLResponse)
def projections_pick(request: Request, token: str = Form(""),
                     player_id: str = Form(""), sort: str = Form("proj"),
                     dir: str = Form(""), pos: str = Form(""),
                     q: str = Form("")):
    con = _db.connect()
    m = con.execute("SELECT * FROM members WHERE pick_token = ?",
                    (token,)).fetchone()
    if not m:
        con.close()
        raise HTTPException(404, "bad pick link")
    season = _db.current_season(con)
    week = _db.current_pick_week(con, season)
    prow = con.execute(
        "SELECT * FROM players WHERE player_id = ? "
        "AND position_group IN ('DB','DL','LB')", (player_id,)).fetchone()
    err = None
    if not prow:
        err = "player not found"
    else:
        existing = con.execute(
            "SELECT player_id FROM weekly_picks WHERE member_id=? AND season=? AND week=?",
            (m["id"], season, week)).fetchall()
        have = {r["player_id"] for r in existing}
        if prow["player_id"] in have:
            err = f"{prow['name']} is already one of your picks"
        elif len(have) >= scoring.WEEKLY_PICKS:
            err = f"you already have {scoring.WEEKLY_PICKS} picks (remove one first)"
        else:
            ko = _db.player_kickoff(con, prow["player_id"], season, week)
            if ko and _ko_started(ko):
                err = f"{prow['name']}'s game already started"
    if err:
        con.close()
        return _proj_redirect(token, sort, dir, pos, q, notice="err:" + err)
    con.execute(
        "INSERT INTO weekly_picks (member_id, player_id, season, week, submitted_at) "
        "VALUES (?,?,?,?,?)",
        (m["id"], prow["player_id"], season, week, _db.utcnow_iso()))
    con.commit()
    con.close()
    return _proj_redirect(token, sort, dir, pos, q,
                          notice="ok:picked " + prow["name"])


@app.post("/projections/unpick", response_class=HTMLResponse)
def projections_unpick(request: Request, token: str = Form(""),
                       player_id: str = Form(""), sort: str = Form("proj"),
                       dir: str = Form(""), pos: str = Form(""),
                       q: str = Form("")):
    con = _db.connect()
    m = con.execute("SELECT * FROM members WHERE pick_token = ?",
                    (token,)).fetchone()
    if not m:
        con.close()
        raise HTTPException(404, "bad pick link")
    season = _db.current_season(con)
    week = _db.current_pick_week(con, season)
    row = con.execute(
        "SELECT wp.player_id, p.name FROM weekly_picks wp "
        "JOIN players p ON p.player_id = wp.player_id "
        "WHERE wp.member_id=? AND wp.season=? AND wp.week=? AND wp.player_id=?",
        (m["id"], season, week, player_id)).fetchone()
    if not row:
        con.close()
        return _proj_redirect(token, sort, dir, pos, q)
    ko = _db.player_kickoff(con, player_id, season, week)
    if ko and _ko_started(ko):
        con.close()
        return _proj_redirect(token, sort, dir, pos, q,
                              notice="err:game already started -- pick is locked in")
    con.execute(
        "DELETE FROM weekly_picks WHERE member_id=? AND season=? AND week=? AND player_id=?",
        (m["id"], season, week, player_id))
    con.commit()
    con.close()
    return _proj_redirect(token, sort, dir, pos, q,
                          notice="ok:removed " + row["name"])
