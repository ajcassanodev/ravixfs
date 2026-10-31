"""ravixfs - NFL defensive Pick5 league site."""
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlencode, urlunparse, urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db as _db
from . import scoring

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
BASE = os.path.dirname(__file__)

app = FastAPI(title="ravixfs")
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))


# Production gate: the prod game (prod-prime5f / prod-pick5f) is
# members-only, like staging. No shared key: a member opens their private
# pick link once (or pastes it on /locked), and the browser stays unlocked
# for 180 days. The pick token IS the credential (same as the pick link
# itself), so storing it in a host-only HttpOnly cookie is no weaker than
# the link. Localhost bypasses the gate: pollers and health checks hit
# 127.0.0.1 directly, never through a public host.
PICK_COOKIE = "prime5_pick"


def _token_member(token: str):
    """Member row for a pick token, or None. DB-validated."""
    token = (token or "").strip()
    if not token:
        return None
    try:
        con = _db.connect()
        try:
            return con.execute(
                "SELECT * FROM members WHERE pick_token = ?",
                (token,),
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return None


def _extract_pick_token(text: str) -> str:
    """Pull a pick token out of a pasted pick link or a bare token."""
    text = (text or "").strip()
    if not text:
        return ""
    if "/submit/" in text:
        text = text.split("/submit/", 1)[1]
        text = text.split("?")[0].split("#")[0].split("/")[0]
    return text.strip()


def _set_pick_cookie(response, request: Request, token: str) -> None:
    """Host-only cookie carrying the member's own pick token."""
    host = (request.headers.get("host", "") or "").split(":")[0].lower()
    kwargs = dict(key=PICK_COOKIE, value=token, path="/",
                  httponly=True, samesite="lax",
                  max_age=180 * 24 * 3600)
    if host not in ("localhost", "127.0.0.1"):
        kwargs["secure"] = True
    response.set_cookie(**kwargs)


def _clean_url_without_token(request: Request) -> str:
    """Same URL with the ?token= param stripped, so the pick token doesn't
    linger in the address bar, history, or logs."""
    parsed = urlparse(str(request.url))
    kept = [(k, v) for k, v in parse_qsl(parsed.query) if k != "token"]
    return urlunparse(parsed._replace(query=urlencode(kept)))


def _gate_unlocked(request: Request) -> bool:
    """True when this browser's pick cookie resolves to a member."""
    try:
        cookie = (request.cookies.get(PICK_COOKIE, "") or "").strip()
        return _token_member(cookie) is not None
    except Exception:
        return False


@app.middleware("http")
async def _prod_gate(request: Request, call_next):
    """Gate the prod game behind a personal pick link.

    - /locked and /static/* always pass.
    - /submit/<token> always passes: it IS the front door, and opening it
      sets the pick cookie (see submit_form).
    - /admin/* always passes through: the handler still enforces ADMIN_TOKEN.
    - ?token=<pick token> on any URL unlocks, then redirects to the same
      URL minus the token.
    - Otherwise a validated pick cookie passes; everyone else goes to /locked.
    """
    host = (request.headers.get("host", "") or "").lower().split(":")[0]
    if host in ("localhost", "127.0.0.1"):
        return await call_next(request)
    path = request.url.path
    if path in ("/locked",) or path.startswith("/static/"):
        return await call_next(request)
    if path.startswith("/submit/") or path.startswith("/admin/"):
        return await call_next(request)
    token_param = request.query_params.get("token", "")
    if token_param and _token_member(token_param) is not None:
        resp = RedirectResponse(_clean_url_without_token(request) or "/", status_code=303)
        _set_pick_cookie(resp, request, token_param.strip())
        return resp
    if _gate_unlocked(request):
        return await call_next(request)
    nxt = request.url.path
    kept = [(k, v) for k, v in parse_qsl(request.url.query) if k != "token"]
    if kept:
        nxt += "?" + urlencode(kept)
    return RedirectResponse(f"/locked?next={quote(nxt, safe='')}", status_code=303)


@app.get("/locked", response_class=HTMLResponse)
def prod_locked(request: Request, next: str = "/", token: str = ""):
    """Members-only lock page. ?token= here also unlocks (same as on any URL)."""
    if token and _token_member(token) is not None:
        resp = RedirectResponse(next or "/", status_code=303)
        _set_pick_cookie(resp, request, token.strip())
        return resp
    if _gate_unlocked(request):
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(request, "lock.html", {
        "request": request, "next_url": next or "/", "error": False,
    })


@app.post("/locked", response_class=HTMLResponse)
def prod_locked_post(request: Request, pick_link: str = Form(""),
                       next: str = Form("/")):
    token = _extract_pick_token(pick_link)
    if token and _token_member(token) is not None:
        resp = RedirectResponse(next or "/", status_code=303)
        _set_pick_cookie(resp, request, token)
        return resp
    return templates.TemplateResponse(request, "lock.html", {
        "request": request, "next_url": next or "/", "error": True,
    })


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


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    con = _db.connect()
    season = _db.current_season(con)
    table, test_table = scoring.standings(con, season)
    wrow = con.execute(
        "SELECT MAX(week) AS w FROM weekly_stats WHERE season = ?", (season,)
    ).fetchone()
    cfg = scoring.scoring_map(con)
    con.close()
    return templates.TemplateResponse(request, "index.html", {
        "request": request, "season": season, "table": table,
        "test_table": test_table,
        "through_week": wrow["w"] or 0, "scoring": cfg,
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
    con.close()
    return templates.TemplateResponse(request, "week.html", {
        "request": request, "season": season, "week": week,
        "detail": detail, "stat_keys": stat_keys,
    })


@app.get("/draft", response_class=HTMLResponse)
def draft_board(request: Request):
    con = _db.connect()
    season = _db.current_season(con)
    rows = con.execute(
        "SELECT m.name AS member, p.name AS player, p.team, p.position, "
        "lp.acquired_via, lp.since_week FROM locked_players lp "
        "JOIN members m ON m.id = lp.member_id "
        "JOIN players p ON p.player_id = lp.player_id "
        "WHERE lp.season = ? AND lp.active = 1 ORDER BY m.name",
        (season,),
    ).fetchall()
    moves = con.execute(
        "SELECT m.name AS member, p.name AS player, lp.acquired_via, "
        "lp.since_week, lp.active FROM locked_players lp "
        "JOIN members m ON m.id = lp.member_id "
        "JOIN players p ON p.player_id = lp.player_id "
        "WHERE lp.season = ? AND lp.active = 0 ORDER BY lp.id DESC",
        (season,),
    ).fetchall()
    con.close()
    return templates.TemplateResponse(request, "draft.html", {
        "request": request, "season": season, "rows": rows, "moves": moves,
    })


@app.get("/trades", response_class=HTMLResponse)
def trade_log(request: Request):
    con = _db.connect()
    season = _db.current_season(con)
    rows = con.execute(
        "SELECT t.week, g.name AS giver, r.name AS receiver, p.name AS player, "
        "t.recorded_at FROM trades t "
        "JOIN members g ON g.id = t.giver_member_id "
        "JOIN members r ON r.id = t.receiver_member_id "
        "JOIN players p ON p.player_id = t.player_id "
        "WHERE t.season = ? ORDER BY t.week DESC, t.id DESC",
        (season,),
    ).fetchall()
    con.close()
    return templates.TemplateResponse(request, "trades.html", {
        "request": request, "season": season, "rows": rows,
    })


@app.get("/guide", response_class=HTMLResponse)
def guide(request: Request):
    return templates.TemplateResponse(request, "guide.html", {"request": request})


@app.get("/submit/{token}", response_class=HTMLResponse)
def submit_form(request: Request, token: str):
    con = _db.connect()
    m = con.execute("SELECT * FROM members WHERE pick_token = ?", (token,)).fetchone()
    if not m:
        con.close()
        raise HTTPException(404, "bad pick link")
    season = _db.current_season(con)
    week = _db.current_pick_week(con, season)
    locked = _db.member_locked_player(con, m["id"], season)
    players = con.execute(
        "SELECT name, team, position, status FROM players "
        "WHERE position_group IN ('DB','DL','LB') ORDER BY name"
    ).fetchall()
    locked_names = {r["name"] for r in con.execute(
        "SELECT p.name FROM locked_players lp JOIN players p ON p.player_id=lp.player_id "
        "WHERE lp.season=? AND lp.active=1", (season,))}
    # annotate kickoff status per player for the form
    now = _db.utcnow_iso()
    # kickoff per team, so the form can flag locked / bye players
    kos = {r["home_team"]: r["kickoff_utc"] for r in
           con.execute("SELECT home_team, kickoff_utc FROM games WHERE season=? AND week=?", (season, week))}
    kos.update({r["away_team"]: r["kickoff_utc"] for r in
                con.execute("SELECT away_team, kickoff_utc FROM games WHERE season=? AND week=?", (season, week))})
    existing = con.execute(
        "SELECT p.name FROM weekly_picks wp JOIN players p ON p.player_id=wp.player_id "
        "WHERE wp.member_id=? AND wp.season=? AND wp.week=?",
        (m["id"], season, week),
    ).fetchall()
    con.close()
    resp = templates.TemplateResponse(request, "submit.html", {
        "request": request, "member": m, "season": season, "week": week,
        "locked": locked, "players": players, "locked_names": locked_names,
        "kickoffs": kos, "now": now,
        "existing": [e["name"] for e in existing], "errors": [], "token": token,
    })
    # Opening the pick link unlocks browsing, same as /locked.
    _set_pick_cookie(resp, request, token)
    return resp


@app.post("/submit/{token}", response_class=HTMLResponse)
def submit_picks(
    request: Request, token: str,
    pick1: str = Form(""), pick2: str = Form(""),
    pick3: str = Form(""), pick4: str = Form(""),
):
    con = _db.connect()
    m = con.execute("SELECT * FROM members WHERE pick_token = ?", (token,)).fetchone()
    if not m:
        con.close()
        raise HTTPException(404, "bad pick link")
    season = _db.current_season(con)
    week = _db.current_pick_week(con, season)
    now = datetime.now(timezone.utc)
    locked_ids = _db.locked_player_ids(con, season)

    errors, resolved = [], []
    seen = set()
    for i, raw in enumerate([pick1, pick2, pick3, pick4], 1):
        prow, err = _resolve_player(con, raw)
        if err:
            errors.append(f"Pick {i}: {err}")
            continue
        if prow["player_id"] in seen:
            errors.append(f"Pick {i}: {prow['name']} already picked")
            continue
        seen.add(prow["player_id"])
        if prow["player_id"] in locked_ids:
            errors.append(f"Pick {i}: {prow['name']} is someone's locked player")
            continue
        ko = _db.player_kickoff(con, prow["player_id"], season, week)
        if ko and ko <= now.isoformat():
            errors.append(f"Pick {i}: {prow['name']}'s game already started")
            continue
        resolved.append(prow)

    if len(resolved) != 4:
        if not errors:
            errors.append("Enter all 4 picks.")
    else:
        # replace this member's picks for the week (re-submit allowed while open)
        con.execute(
            "DELETE FROM weekly_picks WHERE member_id=? AND season=? AND week=?",
            (m["id"], season, week),
        )
        ts = now.isoformat()
        for prow in resolved:
            con.execute(
                "INSERT INTO weekly_picks (member_id, player_id, season, week, submitted_at) "
                "VALUES (?,?,?,?,?)",
                (m["id"], prow["player_id"], season, week, ts),
            )
        con.commit()
        con.close()
        return RedirectResponse(f"/submit/{token}?saved=1", status_code=303)

    # re-render with errors
    players = con.execute(
        "SELECT name, team, position, status FROM players "
        "WHERE position_group IN ('DB','DL','LB') ORDER BY name"
    ).fetchall()
    locked = _db.member_locked_player(con, m["id"], season)
    locked_names = {r["name"] for r in con.execute(
        "SELECT p.name FROM locked_players lp JOIN players p ON p.player_id=lp.player_id "
        "WHERE lp.season=? AND lp.active=1", (season,))}
    kos = {r["home_team"]: r["kickoff_utc"] for r in
           con.execute("SELECT home_team, kickoff_utc FROM games WHERE season=? AND week=?", (season, week))}
    kos.update({r["away_team"]: r["kickoff_utc"] for r in
                con.execute("SELECT away_team, kickoff_utc FROM games WHERE season=? AND week=?", (season, week))})
    con.close()
    return templates.TemplateResponse(request, "submit.html", {
        "request": request, "member": m, "season": season, "week": week,
        "locked": locked, "players": players, "locked_names": locked_names,
        "kickoffs": kos, "now": now.isoformat(), "existing": [],
        "errors": errors, "token": token,
    })


# ---------------- admin ----------------

@app.get("/admin/{token}", response_class=HTMLResponse)
def admin_home(request: Request, token: str):
    if not _admin_ok(token):
        raise HTTPException(404, "not found")
    con = _db.connect()
    season = _db.current_season(con)
    members = con.execute("SELECT * FROM members ORDER BY name").fetchall()
    cfg = con.execute("SELECT * FROM scoring_config").fetchall()
    con.close()
    return templates.TemplateResponse(request, "admin.html", {
        "request": request, "token": token, "season": season,
        "members": members, "scoring": cfg,
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


def _admin_set_locked(con, member_id: int, player_id: str, via: str,
                      season: int, week: int):
    """Deactivate current locked player, activate the new one."""
    con.execute(
        "UPDATE locked_players SET active=0 WHERE member_id=? AND season=? AND active=1",
        (member_id, season),
    )
    con.execute(
        "INSERT INTO locked_players (member_id, player_id, acquired_via, since_week, season, active, created_at) "
        "VALUES (?,?,?,?,?,1,?)",
        (member_id, player_id, via, week, season, _db.utcnow_iso()),
    )


@app.post("/admin/{token}/draft")
def admin_draft(token: str, member_id: int = Form(0), player: str = Form(""),
                week: int = Form(1)):
    if not _admin_ok(token):
        raise HTTPException(404, "not found")
    con = _db.connect()
    season = _db.current_season(con)
    prow, err = _resolve_player(con, player)
    if err:
        con.close()
        raise HTTPException(400, err)
    if prow["player_id"] in _db.locked_player_ids(con, season):
        con.close()
        raise HTTPException(400, "player already locked")
    _admin_set_locked(con, member_id, prow["player_id"], "draft", season, week)
    con.commit()
    con.close()
    return RedirectResponse(f"/admin/{token}", status_code=303)


@app.post("/admin/{token}/redraft")
def admin_redraft(token: str, member_id: int = Form(0), player: str = Form(""),
                  week: int = Form(1)):
    if not _admin_ok(token):
        raise HTTPException(404, "not found")
    con = _db.connect()
    season = _db.current_season(con)
    prow, err = _resolve_player(con, player)
    if err:
        con.close()
        raise HTTPException(400, err)
    if prow["player_id"] in _db.locked_player_ids(con, season):
        con.close()
        raise HTTPException(400, "player already locked")
    _admin_set_locked(con, member_id, prow["player_id"], "redraft", season, week)
    con.commit()
    con.close()
    return RedirectResponse(f"/admin/{token}", status_code=303)


@app.post("/admin/{token}/trade")
def admin_trade(token: str, giver_id: int = Form(0), receiver_id: int = Form(0),
                week: int = Form(1)):
    """Straight-up swap of the two members' locked players."""
    if not _admin_ok(token):
        raise HTTPException(404, "not found")
    con = _db.connect()
    season = _db.current_season(con)
    g = _db.member_locked_player(con, giver_id, season)
    r = _db.member_locked_player(con, receiver_id, season)
    if not g or not r:
        con.close()
        raise HTTPException(400, "both members need a locked player")
    _admin_set_locked(con, giver_id, r["player_id"], "trade", season, week)
    _admin_set_locked(con, receiver_id, g["player_id"], "trade", season, week)
    ts = _db.utcnow_iso()
    con.execute(
        "INSERT INTO trades (season, week, giver_member_id, receiver_member_id, player_id, recorded_at) "
        "VALUES (?,?,?,?,?,?)", (season, week, giver_id, receiver_id, g["player_id"], ts))
    con.execute(
        "INSERT INTO trades (season, week, giver_member_id, receiver_member_id, player_id, recorded_at) "
        "VALUES (?,?,?,?,?,?)", (season, week, receiver_id, giver_id, r["player_id"], ts))
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
