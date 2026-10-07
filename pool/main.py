"""Ravix football pool: pick the winner of each selected NFL game.

Commissioner (Adam) selects which games are in the pool each week via the
admin page (default: all games). Members pick winners through their pick
link; 1 point per correct pick; winners come from final scores. Each pick
locks at its own game's kickoff; the pick window opens Tuesday.

Member identity is shared across all Ravix games (see docs/shared-identity.md):
opening any pick link signs the member in on every Ravix subdomain.
"""
import os
import sqlite3
from datetime import datetime, timezone

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pool import db as _db
from shared import identity as _identity

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
IMPORT_DB = os.environ.get("POOL_IMPORT_DB", "")  # legacy per-game DB for lazy token import
BASE = os.path.dirname(__file__)

app = FastAPI(title="ravixfs-pool")
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))


def _admin_ok(token: str) -> bool:
    import secrets
    return bool(ADMIN_TOKEN) and secrets.compare_digest(token, ADMIN_TOKEN)


@app.on_event("startup")
def _init():
    _db.init_db()
    try:
        _identity.init_shared_db()
    except Exception:
        pass


# ---------------- shared member resolution ----------------

def _resolve_member(token: str):
    """Shared-store member for a pick token, lazily importing from the legacy
    Pick5 DB so a link issued by any game works in every game."""
    con = _identity.connect_shared()
    if con is None:
        return None, None
    try:
        m = _identity.get_member_by_token(con, token)
        if m is None and IMPORT_DB and os.path.exists(IMPORT_DB):
            src = sqlite3.connect(IMPORT_DB)
            src.row_factory = sqlite3.Row
            try:
                old = src.execute(
                    "SELECT name, pick_token, COALESCE(is_test,0) AS is_test "
                    "FROM members WHERE pick_token = ?", (token,)
                ).fetchone()
            finally:
                src.close()
            if old:
                m = _identity.import_member(con, old["name"], old["pick_token"],
                                           bool(old["is_test"]))
        if m is None:
            return None, None
        _identity.ensure_membership(con, m["id"], "pool")
        return _identity.get_member_by_id(con, m["id"]), con
    except Exception:
        con.close()
        return None, None
    # NOTE: caller closes con


def _close(con):
    try:
        con.close()
    except Exception:
        pass


def _ko_started(ko_iso) -> bool:
    if not ko_iso:
        return False
    ko = datetime.fromisoformat(ko_iso)
    if ko.tzinfo is None:
        ko = ko.replace(tzinfo=timezone.utc)
    return ko <= datetime.now(timezone.utc)


def _et_str(ko_iso) -> str:
    try:
        from zoneinfo import ZoneInfo
        ko = datetime.fromisoformat(ko_iso)
        if ko.tzinfo is None:
            ko = ko.replace(tzinfo=timezone.utc)
        return ko.astimezone(ZoneInfo("America/New_York")).strftime("%a %-I:%M %p ET")
    except Exception:
        return ko_iso or ""


def _week_games(con, season, week):
    rows = con.execute(
        "SELECT * FROM pool_games WHERE season = ? AND week = ? "
        "ORDER BY kickoff_utc", (season, week)
    ).fetchall()
    out = []
    for g in rows:
        d = dict(g)
        d["started"] = _ko_started(g["kickoff_utc"])
        d["ko_et"] = _et_str(g["kickoff_utc"])
        d["winner"] = _db.game_winner(g)
        out.append(d)
    return out


# ---------------- public pages ----------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    con = _db.connect()
    season = _db.current_season(con)
    week = _db.current_pick_week(con, season)
    table = _db.season_standings(con, season, include_test=False)
    test_table = _db.season_standings(con, season, include_test=True)
    test_table = [t for t in test_table if t[3]]
    # this-week record per member
    wk = {}
    for mid, name, total, _ in table:
        c, d = _db.member_week_score(con, mid, season, week)
        wk[mid] = (c, d)
    games = _week_games(con, season, week)
    con.close()
    return templates.TemplateResponse(request, "index.html", {
        "request": request, "season": season, "week": week,
        "table": table, "test_table": test_table, "wk": wk, "games": games,
    })


@app.get("/week/{season}/{week}", response_class=HTMLResponse)
def week_view(request: Request, season: int, week: int):
    con = _db.connect()
    games = _week_games(con, season, week)
    s = _identity.connect_shared()
    members, rows = [], []
    if s is not None:
        try:
            members = _identity.game_members(s, "pool", include_test=True)
        finally:
            _close(s)
    picks = {}
    for m in members:
        pr = con.execute(
            "SELECT away_team, home_team, picked_team FROM pool_picks "
            "WHERE member_id = ? AND season = ? AND week = ?",
            (m["id"], season, week),
        ).fetchall()
        picks[m["id"]] = {(r["away_team"], r["home_team"]): r["picked_team"]
                          for r in pr}
    for m in members:
        c, d = _db.member_week_score(con, m["id"], season, week)
        rows.append({"name": m["name"], "is_test": bool(m["is_test"]),
                     "id": m["id"], "correct": c, "decided": d})
    rows.sort(key=lambda r: (-r["correct"], r["name"].lower()))
    con.close()
    return templates.TemplateResponse(request, "week.html", {
        "request": request, "season": season, "week": week,
        "games": games, "rows": rows, "picks": picks,
    })


@app.get("/pick", response_class=HTMLResponse)
def pick_redirect(request: Request):
    """Cross-game entry point: resolve the shared session to the member's
    pool pick link."""
    m = _identity.session_member(request)
    if not m:
        raise HTTPException(404, "no Ravix session -- open your pick link first")
    return RedirectResponse(f"/pick/{m['pick_token']}", status_code=302)


@app.get("/pick/{token}", response_class=HTMLResponse)
def pick_form(request: Request, token: str):
    m, scon = _resolve_member(token)
    if m is None:
        _close(scon)
        raise HTTPException(404, "bad pick link")
    con = _db.connect()
    season = _db.current_season(con)
    week = _db.current_pick_week(con, season)
    games = [g for g in _week_games(con, season, week) if g["included"]]
    existing = {
        (r["away_team"], r["home_team"]): r["picked_team"]
        for r in con.execute(
            "SELECT away_team, home_team, picked_team FROM pool_picks "
            "WHERE member_id = ? AND season = ? AND week = ?",
            (m["id"], season, week),
        ).fetchall()
    }
    con.close()
    resp = templates.TemplateResponse(request, "pick.html", {
        "request": request, "member": m, "season": season, "week": week,
        "games": games, "existing": existing,
        "errors": [], "saved": request.query_params.get("saved", ""),
        "token": token,
    })
    try:
        _identity.set_session_cookie(resp, request, m["id"])
    except Exception:
        pass
    _close(scon)
    return resp


@app.post("/pick/{token}", response_class=HTMLResponse)
async def submit_picks(request: Request, token: str):
    m, scon = _resolve_member(token)
    if m is None:
        _close(scon)
        raise HTTPException(404, "bad pick link")
    con = _db.connect()
    season = _db.current_season(con)
    week = _db.current_pick_week(con, season)
    now = datetime.now(timezone.utc)
    form = await request.form()

    games = { (g["away_team"], g["home_team"]): g for g in _week_games(con, season, week)
              if g["included"] }
    errors, saved = [], 0
    ts = now.isoformat()
    for (away, home), g in games.items():
        key = f"g_{away}_{home}"
        val = (form.get(key) or "").strip().upper()
        if not val:
            continue  # no pick submitted for this game
        if val not in (away, home):
            errors.append(f"{away} @ {home}: invalid pick")
            continue
        if g["started"]:
            # Kickoff lock: each pick locks at its own game's kickoff.
            errors.append(f"{away} @ {home} already started -- pick locked")
            continue
        con.execute(
            "INSERT INTO pool_picks (member_id, season, week, away_team, home_team, "
            "picked_team, submitted_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT (member_id, season, week, away_team, home_team) "
            "DO UPDATE SET picked_team=excluded.picked_team, "
            "submitted_at=excluded.submitted_at",
            (m["id"], season, week, away, home, val, ts),
        )
        saved += 1
    con.commit()

    if errors:
        existing = {
            (r["away_team"], r["home_team"]): r["picked_team"]
            for r in con.execute(
                "SELECT away_team, home_team, picked_team FROM pool_picks "
                "WHERE member_id = ? AND season = ? AND week = ?",
                (m["id"], season, week),
            ).fetchall()
        }
        con.close()
        resp = templates.TemplateResponse(request, "pick.html", {
            "request": request, "member": m, "season": season, "week": week,
            "games": list(games.values()), "existing": existing,
            "errors": errors, "saved": "", "token": token,
        })
    else:
        con.close()
        resp = RedirectResponse(f"/pick/{token}?saved={saved}", status_code=303)
    try:
        _identity.set_session_cookie(resp, request, m["id"])
    except Exception:
        pass
    _close(scon)
    return resp


# ---------------- admin ----------------

@app.get("/admin/{token}", response_class=HTMLResponse)
def admin_home(request: Request, token: str, week: int = 0):
    if not _admin_ok(token):
        raise HTTPException(404, "not found")
    con = _db.connect()
    season = _db.current_season(con)
    if not week:
        week = _db.current_pick_week(con, season)
    games = _week_games(con, season, week)
    s = _identity.connect_shared()
    members = []
    if s is not None:
        try:
            members = _identity.game_members(s, "pool", include_test=True)
        finally:
            _close(s)
    host = request.headers.get("host", "")
    links = [(m["name"], bool(m["is_test"]),
              f"https://{host}/pick/{m['pick_token']}") for m in members]
    weeks = [r["week"] for r in con.execute(
        "SELECT DISTINCT week FROM pool_games WHERE season = ? ORDER BY week",
        (season,))]
    con.close()
    return templates.TemplateResponse(request, "admin.html", {
        "request": request, "token": token, "season": season, "week": week,
        "weeks": weeks, "games": games, "links": links,
    })


@app.post("/admin/{token}/games")
async def admin_save_games(request: Request, token: str, week: int = Form(0)):
    if not _admin_ok(token):
        raise HTTPException(404, "not found")
    form = await request.form()
    con = _db.connect()
    season = _db.current_season(con)
    games = con.execute(
        "SELECT away_team, home_team FROM pool_games WHERE season = ? AND week = ?",
        (season, week),
    ).fetchall()
    for g in games:
        key = f"incl_{g['away_team']}_{g['home_team']}"
        included = 1 if form.get(key) else 0
        con.execute(
            "UPDATE pool_games SET included = ? WHERE season = ? AND week = ? "
            "AND away_team = ? AND home_team = ?",
            (included, season, week, g["away_team"], g["home_team"]),
        )
    con.commit()
    con.close()
    return RedirectResponse(f"/admin/{token}?week={week}&saved=1", status_code=303)


@app.post("/admin/{token}/members/add")
def admin_add_member(token: str, name: str = Form(""), is_test: int = Form(0)):
    if not _admin_ok(token):
        raise HTTPException(404, "not found")
    name = name.strip()
    if not name:
        raise HTTPException(400, "name required")
    con = _identity.connect_shared()
    if con is None:
        raise HTTPException(500, "shared member store not configured")
    try:
        m = _identity.create_member(con, name, bool(is_test))
        _identity.ensure_membership(con, m["id"], "pool")
    finally:
        _close(con)
    return RedirectResponse(f"/admin/{token}", status_code=303)
