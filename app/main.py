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
    where = "" if include_test else "WHERE COALESCE(is_test, 0) = 0 "
    for m in con.execute(
        "SELECT id, name FROM members " + where + "ORDER BY name"
    ):
        pts, _ = scoring.member_week_points(con, m["id"], season, week)
        entries.append({"name": m["name"], "points": pts})
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
    return {"week": week, "entries": entries, "live": live, "updated": stamp}


@app.get("/ticker.json")
def ticker_json(request: Request):
    con = _db.connect()
    season = _db.current_season(con)
    data = _ticker_data(con, season, include_test=_is_staging_host(request.headers.get("host", "")))
    con.close()
    return JSONResponse(data)


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

    # This week: picks with live game status.
    week_total, lines = scoring.member_week_points(con, mid, season, pick_week)
    states = _gamestate.fetch_states(pick_week)
    now = datetime.now(timezone.utc)
    picks, next_lock = [], None
    for pid, pname, pteam, src, pts, _st in lines:
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
        picks.append({"name": pname, "team": pteam, "src": src,
                      "points": pts, "status": status, "label": label})
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

    # Top 4 performers league-wide this week.
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
    top4 = perf[:4]

    # Weekly trend.
    trend = []
    for w in range(1, pick_week + 1):
        t, _ = scoring.member_week_points(con, mid, season, w)
        trend.append({"week": w, "points": t})

    return {
        "member_name": m["name"], "season": season, "week": pick_week,
        "token": token, "member": m,
        "week_live": week_live, "rank": rank, "total": total,
        "leader_name": leader_name, "leader_total": leader_total,
        "gap": round(leader_total - (total or 0), 2),
        "season_pie": season_pie, "week_pie": week_pie,
        "week_total": week_total, "picks": picks,
        "top4": top4, "trend": trend,
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


@app.get("/scoring-events.json")
def scoring_events_json(request: Request, since: str = ""):
    """Recent scoring plays for picked players, for the notification popups.

    `since` is an ISO timestamp; only events after it are returned. Players
    are limited to this week's picked set (weekly picks, league-wide). Each
    event carries mine=true when the viewer -- identified by the pick-link
    session / staging pick cookie -- has that player picked.
    """
    con = _db.connect()
    try:
        season = _db.current_season(con)
        week = _db.current_pick_week(con, season)
        picked = {r["player_id"] for r in con.execute(
            "SELECT player_id FROM weekly_picks WHERE season = ? AND week = ?",
            (season, week))}
        mine = set()
        if picked:
            token = _request_pick_token(request)
            if token:
                mine = {r["player_id"] for r in con.execute(
                    "SELECT wp.player_id FROM weekly_picks wp "
                    "JOIN members m ON m.id = wp.member_id "
                    "WHERE m.pick_token = ? AND wp.season = ? AND wp.week = ?",
                    (token, season, week))}
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
                events.append({
                    "player": r["name"],
                    "team": r["team"],
                    "stat": r["stat_key"],
                    "delta": round(r["new_total"] - r["old_total"], 2),
                    "points": r["points_delta"],
                    "at": r["created_at"],
                    "mine": r["player_id"] in mine,
                })
        return JSONResponse({"events": events, "now": _db.utcnow_iso()})
    finally:
        con.close()


def _standings_context(con, request):
    """Standings rows with behind-leader and avg/week, plus ticker."""
    season = _db.current_season(con)
    wrow = con.execute(
        "SELECT MAX(week) AS w FROM weekly_stats WHERE season = ?", (season,)
    ).fetchone()
    through_week = wrow["w"] or 0
    table_now, test_table = scoring.standings(con, season, through_week)
    table_prev, _ = (
        scoring.standings(con, season, through_week - 1)
        if through_week > 1 else ([], [])
    )
    rank_prev = {mid: i + 1 for i, (mid, _, _) in enumerate(table_prev)}
    leader_total = table_now[0][2] if table_now else 0.0
    standings = []
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
        standings.append({
            "rank": rank, "name": name, "total": total,
            "move_class": move_class, "move_arrow": move_arrow,
            "move_n": move_n,
            "behind": round(leader_total - total, 2),
            "avg": round(total / through_week, 2) if through_week else 0.0,
        })
    return {
        "season": season, "standings": standings,
        "test_table": test_table,
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
            picks.append({
                "name": pname, "team": pteam, "src": src,
                "points": pts, "status": status, "label": label,
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
