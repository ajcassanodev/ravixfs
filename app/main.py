"""ravixfs - NFL defensive Pick5 league site."""
import os
import secrets
from datetime import datetime, timezone

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db as _db
from . import scoring

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
BASE = os.path.dirname(__file__)

app = FastAPI(title="ravixfs")
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))


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
    if not week:
        week = _db.current_pick_week(con, season)
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


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    con = _db.connect()
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
    standings = []
    for i, (mid, name, total) in enumerate(table_now):
        rank = i + 1
        lp = _db.member_locked_player(con, mid, season)
        pr = rank_prev.get(mid)
        if pr is None:
            move_class, move_arrow = "move-same", "–"
        elif pr > rank:
            move_class, move_arrow = "move-up", "▲"
        elif pr < rank:
            move_class, move_arrow = "move-down", "▼"
        else:
            move_class, move_arrow = "move-same", "·"
        standings.append({
            "rank": rank, "name": name, "total": total,
            "move_class": move_class, "move_arrow": move_arrow,
            "locked_name": lp["name"] if lp else "",
            "locked_team": lp["team"] if lp else "",
        })
    ticker = _ticker_data(con, season, include_test=_is_staging_host(request.headers.get("host", "")))
    cfg = scoring.scoring_map(con)
    con.close()
    return templates.TemplateResponse(request, "index.html", {
        "request": request, "season": season, "standings": standings,
        "test_table": test_table, "ticker": ticker,
        "through_week": through_week, "scoring": cfg,
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
    return templates.TemplateResponse(request, "submit.html", {
        "request": request, "member": m, "season": season, "week": week,
        "locked": locked, "players": players, "locked_names": locked_names,
        "kickoffs": kos, "now": now,
        "existing": [e["name"] for e in existing], "errors": [], "token": token,
    })


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


# ---------------- projections + pick-from-report ----------------

PROJ_SORTS = {
    "proj": (lambda d: d["proj_pts"], True),
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
    rows, locked, picks, locked_ids, picked_ids = [], None, [], set(), set()
    if through >= 1:
        rows = scoring.projections(con, season, through)
        if pos in ("DB", "DL", "LB"):
            rows = [r for r in rows if r["position_group"] == pos]
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in r["name"].lower()]
        keyfn, default_desc = PROJ_SORTS.get(sort, PROJ_SORTS["proj"])
        desc = (dir == "desc") if dir in ("asc", "desc") else default_desc
        rows.sort(key=keyfn, reverse=desc)
    if member:
        locked = _db.member_locked_player(con, member["id"], season)
        locked_ids = _db.locked_player_ids(con, season)
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
        if pid in locked_ids:
            r["state"] = "locked"
        elif pid in picked_ids:
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
        "rows": rows, "member": member, "locked": locked,
        "picks": pick_kos, "locked_ids": locked_ids,
        "scoring": scoring.scoring_map(con),
        "sort": sort if sort in PROJ_SORTS else "proj",
        "dir": dir if dir in ("asc", "desc") else "",
        "pos": pos, "q": q, "token": token,
        "sort_links": _sort_links(token, sort, dir, pos, q),
    }


@app.get("/projections", response_class=HTMLResponse)
def projections_page(request: Request, token: str = "", sort: str = "proj",
                     dir: str = "", pos: str = "", q: str = ""):
    con = _db.connect()
    ctx = _projections_context(con, request, token, sort, dir, pos, q)
    con.close()
    return templates.TemplateResponse(request, "projections.html", {
        "request": request, **ctx,
        "notice": request.query_params.get("notice", ""),
    })


SORT_LABELS = [
    ("player", "Player"), ("team", "Team"), ("pos", "Pos"), ("proj", "Proj"),
    ("tkl", "Tkl/g"), ("sack", "Sack/g"), ("ff", "FF/g"), ("fr", "FR/g"),
    ("int", "INT/g"), ("td", "TD/g"), ("season", "Season"),
]


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
    elif prow["player_id"] in _db.locked_player_ids(con, season):
        err = f"{prow['name']} is someone's locked player"
    else:
        existing = con.execute(
            "SELECT player_id FROM weekly_picks WHERE member_id=? AND season=? AND week=?",
            (m["id"], season, week)).fetchall()
        have = {r["player_id"] for r in existing}
        if prow["player_id"] in have:
            err = f"{prow['name']} is already one of your picks"
        elif len(have) >= 4:
            err = "you already have 4 picks (remove one first)"
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
