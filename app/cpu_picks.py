"""Auto-pick method test: CPU Random vs CPU Smart (staging only).

Two test members play out the rest of the season with computer-made picks
so we can measure how well the computer picks before auto-picks become a
member feature / production add-on:

  - "CPU Random": 5 defenders drawn at random from the projections pool
    (every defender with at least one game this season -- the same pool
    members pick from). No filtering: hurt, inactive and bye-week players
    are all eligible. The draw is seeded per week so it is reproducible.
  - "CPU Smart": top 5 by matchup-adjusted projection (app/matchup.py),
    skipping bye-week and non-active players -- the computer analyzes the
    matchups and plays the best available defenders.

Generation rides the poller: on Thursdays from 9:00 AM ET until the week's
first kickoff, any poller run fills the CPU members' picks if they don't
have 5 yet (idempotent). Staging only: guarded on the RAVIXFS_DB path so
this can never create members or picks in the production database.
"""

import os
import json
import random
import secrets
from datetime import datetime, time as dtime, timezone, timedelta
from zoneinfo import ZoneInfo

from . import db as _db
from . import gamestate as _gamestate
from . import matchup as _matchup
from . import scoring as _scoring
from . import audit as _audit

ET = ZoneInfo("America/New_York")
CPU_MEMBERS = (("CPU Random", "random"), ("CPU Smart", "smart"))
PICKS_PER_WEEK = 5
WINDOW_START = dtime(9, 0)  # Thursday 9:00 AM ET


def _staging_db() -> bool:
    return "staging" in os.environ.get("RAVIXFS_DB", "")


def ensure_cpu_members(con) -> dict:
    """Create the two CPU test members if missing; return {name: id}."""
    ids = {}
    for name, _method in CPU_MEMBERS:
        row = con.execute(
            "SELECT id FROM members WHERE name = ?", (name,)).fetchone()
        if row:
            ids[name] = row["id"]
        else:
            cur = con.execute(
                "INSERT INTO members (name, pick_token, is_test) "
                "VALUES (?, ?, 1)",
                (name, secrets.token_urlsafe(24)))
            ids[name] = cur.lastrowid
    return ids


def _first_kickoff(con, season: int, week: int):
    row = con.execute(
        "SELECT MIN(kickoff_utc) AS ko FROM games "
        "WHERE season = ? AND week = ?", (season, week)).fetchone()
    if not row or not row["ko"]:
        return None
    ko = datetime.fromisoformat(row["ko"].replace("Z", "+00:00"))
    return ko if ko.tzinfo else ko.replace(tzinfo=timezone.utc)


def generation_due(con, season: int, week: int, now_et: datetime) -> bool:
    """Thursday 9:00 AM ET until the week's first kickoff."""
    if now_et.weekday() != 3 or now_et.time() < WINDOW_START:
        return False
    ko = _first_kickoff(con, season, week)
    if ko is not None and now_et.astimezone(timezone.utc) >= ko:
        return False
    return True


def random_picks(con, season: int, week: int, n: int = PICKS_PER_WEEK):
    """Uniform random draw from the projections pool. No filtering."""
    rows = _scoring.projections(con, season, week - 1)
    pool = sorted(r["player_id"] for r in rows)
    rng = random.Random(f"cpu-random-{season}-w{week}")
    return rng.sample(pool, min(n, len(pool)))


def smart_picks(con, season: int, week: int, n: int = PICKS_PER_WEEK):
    """Top-n by matchup-adjusted projection; skips bye + non-active."""
    rows = _scoring.projections(con, season, week - 1)
    _matchup.annotate_rows(con, season, week, week - 1, rows,
                           _scoring.scoring_map(con))
    elig = [r for r in rows
            if r.get("opponent")
            and (not r["status"] or r["status"].upper() == "ACT")]
    elig.sort(key=lambda r: (-r["adj_pts"], -r["proj_pts"], r["name"]))
    return [r["player_id"] for r in elig[:n]]


def run_cpu_picks(con, season: int, now_et: datetime = None) -> str:
    """Fill the CPU members' picks for the current pick week, if due."""
    if not _staging_db():
        return "cpu picks skipped: not the staging database"
    now_et = now_et or datetime.now(ET)
    week = _db.current_pick_week(con, season)
    if not generation_due(con, season, week, now_et):
        return f"cpu picks: not due (week {week})"
    ids = ensure_cpu_members(con)
    stamp = datetime.now(timezone.utc).isoformat()
    filled = []
    for name, method in CPU_MEMBERS:
        mid = ids[name]
        have = {r["player_id"] for r in con.execute(
            "SELECT player_id FROM weekly_picks "
            "WHERE member_id = ? AND season = ? AND week = ?",
            (mid, season, week))}
        if len(have) >= PICKS_PER_WEEK:
            continue
        picks = (random_picks(con, season, week) if method == "random"
                 else smart_picks(con, season, week))
        for pid in picks:
            if len(have) >= PICKS_PER_WEEK:
                break
            if pid in have:
                continue
            cur = con.execute(
                "INSERT OR IGNORE INTO weekly_picks "
                "(member_id, player_id, season, week, submitted_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (mid, pid, season, week, stamp))
            if cur.rowcount == 1:
                # Audit: CPU Thursday fill (the method-test record).
                _audit.log(con, season, week, "pick_submitted", "cpu_fill",
                           member_id=mid, player_id=pid,
                           source_feed="system",
                           actor=f"cpu-{method}",
                           detail={"cpu_method": method})
            have.add(pid)
        filled.append(f"{name} ({len(have)} picks)")
    con.commit()
    return f"cpu picks week {week}: " + (", ".join(filled) or "already full")


# ---- Member auto-picks (Week 8 upgrade) -------------------------------
# NOT staging-gated: this is a real member feature (the CPU test above
# is the staging-only part). A member with the dashboard toggle ON gets
# open slots filled on every poller cycle, window by window: each cycle
# fills whatever slots are open from players whose games haven't kicked
# off yet, so a slot emptied by an edit gets refilled before the next
# window. Fills use the commissioner's method setting (default Random).
# Picks are tagged AUTO (weekly_picks.source) and stay editable until
# their own kickoff; subs work the same as for manual picks. CPU test
# members are skipped here — their fixed methods and Thursday fill above
# are untouched.

METHODS = ("random", "smart")
CPU_NAMES = {name for name, _ in CPU_MEMBERS}


def _ensure_settings(con) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS settings "
        "(key TEXT PRIMARY KEY, value TEXT)")


def get_auto_method(con) -> str:
    """Commissioner's member auto-pick method; 'random' unless set."""
    try:
        row = con.execute(
            "SELECT value FROM settings WHERE key = 'auto_pick_method'"
        ).fetchone()
        if row and row["value"] in METHODS:
            return row["value"]
    except Exception:
        # settings table predates this deploy — create and retry once.
        try:
            _ensure_settings(con)
            row = con.execute(
                "SELECT value FROM settings WHERE key = 'auto_pick_method'"
            ).fetchone()
            if row and row["value"] in METHODS:
                return row["value"]
        except Exception:
            pass
    return "random"


def effective_member_method(con, member_row) -> str:
    """The auto-pick method that actually applies to one member.

    Admin-set member_auto ('off' | 'random' | 'smart') wins. NULL means
    the admin never set this member, so the global setting applies
    (the pre-per-member behavior). 'off' means no fills even when the
    member's own toggle is ON -- admin Off always wins.
    """
    try:
        v = (member_row["member_auto"] or "").strip().lower()
    except Exception:
        v = ""
    if v in ("off", "random", "smart"):
        return v
    return get_auto_method(con)


def set_auto_method(con, method: str) -> None:
    if method not in METHODS:
        raise ValueError("unknown auto-pick method")
    _ensure_settings(con)
    con.execute(
        "INSERT INTO settings (key, value) VALUES ('auto_pick_method', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (method,))
    con.commit()


def _parse_ko(iso):
    if not iso:
        return None
    try:
        kdt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return kdt if kdt.tzinfo else kdt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _not_started(con, pid: str, season: int, week: int, now_utc) -> bool:
    """True when the player's game hasn't kicked off (bye = no game)."""
    kdt = _parse_ko(_db.player_kickoff(con, pid, season, week))
    return kdt is None or kdt > now_utc


def member_random_picks(con, season: int, week: int, exclude,
                        now_utc, n: int):
    """Unseeded uniform draw from the not-yet-started pool.

    Unseeded on purpose: a seeded draw would hand a member the same
    player back after they removed him, which would feel broken.
    Bye-week players stay eligible, matching the Random method.
    """
    rows = _scoring.projections(con, season, week - 1)
    pool = [r["player_id"] for r in rows
            if r["player_id"] not in exclude
            and _not_started(con, r["player_id"], season, week, now_utc)]
    rng = random.Random()
    return rng.sample(pool, min(n, len(pool)))


def member_smart_picks(con, season: int, week: int, exclude,
                       now_utc, n: int):
    """Top-n Adj from the not-yet-started pool; skips bye + non-ACT,
    matching the Smart method."""
    rows = _scoring.projections(con, season, week - 1)
    _matchup.annotate_rows(con, season, week, week - 1, rows,
                           _scoring.scoring_map(con))
    elig = [r for r in rows
            if r["player_id"] not in exclude
            and r.get("opponent")
            and (not r["status"] or r["status"].upper() == "ACT")
            and _not_started(con, r["player_id"], season, week, now_utc)]
    elig.sort(key=lambda r: (-r["adj_pts"], -r["proj_pts"], r["name"]))
    return [r["player_id"] for r in elig[:n]]


def run_member_auto_picks(con, season: int, now_et: datetime = None) -> str:
    """Fill open slots for every member with the toggle ON.

    Each member runs under their own admin-set method (Off / Random /
    Smart); members the admin never set follow the global method.
    Deactivated members and CPU test members are skipped here — CPU
    test members keep their fixed methods and Thursday fill above.
    """
    now_et = now_et or datetime.now(ET)
    now_utc = now_et.astimezone(timezone.utc)
    week = _db.current_pick_week(con, season)
    stamp = datetime.now(timezone.utc).isoformat()
    filled = []
    try:
        members = con.execute(
            "SELECT id, name, member_auto FROM members "
            "WHERE auto_picks = 1 AND COALESCE(is_active, 1) = 1").fetchall()
    except Exception:
        return "member auto-picks skipped: auto_picks predates this deploy"
    for m in members:
        if m["name"] in CPU_NAMES:
            continue
        method = effective_member_method(con, m)
        if method == "off":
            continue
        try:
            stints = _scoring.member_week_stints(con, m["id"], season, week)
        except Exception:
            continue
        # Active stints are the current lineup; every stint player
        # (including subbed-out ones, who can't return) is excluded.
        have = {s["player_id"] for s in stints}
        open_slots = PICKS_PER_WEEK - sum(1 for s in stints if s["active"])
        if open_slots <= 0:
            continue
        if method == "smart":
            picks = member_smart_picks(con, season, week, have, now_utc,
                                       open_slots)
        else:
            picks = member_random_picks(con, season, week, have, now_utc,
                                        open_slots)
        for pid in picks:
            cur = con.execute(
                "INSERT OR IGNORE INTO weekly_picks "
                "(member_id, player_id, season, week, submitted_at, source) "
                "VALUES (?, ?, ?, ?, ?, 'auto')",
                (m["id"], pid, season, week, stamp))
            if cur.rowcount == 1:
                # Audit: member auto-pick fill (window = the pick's
                # kickoff; method = the commissioner's setting).
                _audit.log(con, season, week, "pick_auto_filled",
                           "auto_fill", member_id=m["id"], player_id=pid,
                           source_feed="system", actor="poller",
                           detail={"method": method,
                                   "kickoff": _db.player_kickoff(
                                       con, pid, season, week)})
        if picks:
            filled.append(f"{m['name']} (+{len(picks)})")
    if filled:
        con.commit()
    return (f"member auto-picks week {week} (per-member methods): "
            + (", ".join(filled) or "nothing open"))


# ---- CPU Smart live subs (Week 8) ------------------------------------
# Staging only. Smart manages its lineup like a member, so we can watch
# what it does in real games before bonus picks arrive in Week 10:
#
#   - Pre-kickoff: Smart can re-pick any pick whose game hasn't
#     started. An inactive (non-ACT) or bye pick always moves to the
#     best available unstarted player; an active pick moves only when
#     another available unstarted player's Adj beats its own by
#     SMART_SUB_MARGIN (1.0) or more. Edits are free and unlimited —
#     the margin is what keeps Smart from churning on noise.
#   - Live: every poller cycle, each live Smart pick's projected
#     REMAINING points (Adj x share of game left) is compared with the
#     best same-window replacement's projected remaining. Smart subs
#     only when the replacement leads by SMART_SUB_MARGIN (1.0) or
#     more, so it never churns subs on noise. Banked points stay either
#     way, so only the future matters.
#
# Member rules are mirrored exactly: 2 subs per week, same kickoff
# window (exact kickoff equality, as the member rule enforces), both
# games live, no re-entry (a subbed-out player can never return), and
# the sub is written through the same weekly_subs rows members use —
# so ticker SUB items, window recaps, and the admin Live substitutions
# section all pick Smart's subs up automatically. At most one sub per
# poller cycle (the largest edge); the next cycle reconsiders.
# CPU Random is untouched.

DEFAULT_MARGIN = 1.0
MIN_CALLS = 6
MARGIN_FLOOR, MARGIN_CEIL = 0.5, 2.0
MAX_STEP = 0.25
LEARN_RATE = 0.5
_GAME_LEN_HOURS = 3.25


def _smart_id(con):
    """CPU Smart's member id, or None when the member doesn't exist."""
    row = con.execute(
        "SELECT id FROM members WHERE name = 'CPU Smart'").fetchone()
    return row["id"] if row else None


def _ensure_decisions(con):
    con.execute(
        "CREATE TABLE IF NOT EXISTS smart_decisions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "member_id INTEGER NOT NULL, season INTEGER NOT NULL, "
        "week INTEGER NOT NULL, kind TEXT NOT NULL, "
        "out_player_id TEXT NOT NULL, in_player_id TEXT NOT NULL, "
        "edge REAL NOT NULL, gated INTEGER NOT NULL DEFAULT 1, "
        "net REAL, settled INTEGER NOT NULL DEFAULT 0, "
        "decided_at TEXT NOT NULL, settled_at TEXT)")


def get_smart_margin(con) -> float:
    """Current Smart margin; the 1.0 default unless learning moved it."""
    try:
        row = con.execute(
            "SELECT value FROM settings WHERE key = 'smart_sub_margin'"
        ).fetchone()
        if row:
            return max(MARGIN_FLOOR,
                       min(MARGIN_CEIL, float(row["value"])))
    except Exception:
        pass
    return DEFAULT_MARGIN


def _set_smart_margin(con, margin: float) -> None:
    _ensure_settings(con)
    con.execute(
        "INSERT INTO settings (key, value) VALUES ('smart_sub_margin', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (f"{margin:.2f}",))
    con.commit()


def _log_decision(con, mid, season, week, kind, out_pid, in_pid, edge,
                  gated):
    """Append one Smart decision to the learning ledger. The caller
    commits: this rides the same transaction as the sub/pick write."""
    _ensure_decisions(con)
    con.execute(
        "INSERT INTO smart_decisions (member_id, season, week, kind, "
        "out_player_id, in_player_id, edge, gated, decided_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (mid, season, week, kind, out_pid, in_pid, round(edge, 2),
         1 if gated else 0, _db.utcnow_iso()))


def _game_live(con, team, pid, season, week, states):
    """(is_live, clock_label) for a player's game right now.

    Mirrors main._sub_game_live without importing the web app: ESPN
    state wins when the feed has the game, else the kickoff window
    (started, less than 4 hours ago)."""
    game = _gamestate.player_game(team, states) if states else None
    if game is not None:
        if game["state"] == "in":
            return True, _gamestate.live_label(game)
        return False, None
    ko = _db.player_kickoff(con, pid, season, week)
    kdt = _parse_ko(ko)
    if kdt is None:
        return False, None
    now = datetime.now(timezone.utc)
    return (kdt <= now < kdt + timedelta(hours=4)), None


def _game_progress(team, states, kickoff_utc, now_utc):
    """Share of the game elapsed (0..1), or None if not started.

    ESPN period/clock when the feed has the game (halftime = 0.5,
    overtime = 1.0); otherwise elapsed time since kickoff over a
    typical 3.25-hour game."""
    game = _gamestate.player_game(team, states) if states else None
    if game is not None and game["state"] == "in":
        detail = (game.get("detail") or "").lower()
        if "halftime" in detail:
            return 0.5
        period = game.get("period", 0) or 0
        if period > 4:
            return 1.0
        clock = game.get("clock", "") or ""
        try:
            mins, secs = clock.split(":")
            remaining = int(mins) * 60 + int(secs)
            elapsed = (period - 1) * 900 + (900 - remaining)
            return max(0.0, min(1.0, elapsed / 3600.0))
        except Exception:
            pass
    kdt = _parse_ko(kickoff_utc)
    if kdt is None or now_utc < kdt:
        return None
    hours = (now_utc - kdt).total_seconds() / 3600.0
    return max(0.0, min(1.0, hours / _GAME_LEN_HOURS))


def _repick_choice(out_pid, out_status, out_ko, out_adj, rows, seen,
                   unstarted, now_utc, margin=DEFAULT_MARGIN):
    """(replacement_id, predicted_edge, gated) for a Smart pick, or
    (None, None, False) to stand pat.

    Pure decision logic (no DB): the caller supplies the projections
    rows, the current lineup set, and the set of not-yet-started
    player_ids. A started pick (kickoff passed) never moves here —
    only live subs can move it. An inactive/bye pick always moves to
    the best available unstarted player (predicted gain = the
    replacement's whole Adj, since the dropped player scores 0) and
    is NOT a margin judgment (gated=False). An active pick moves
    only when the best available player's Adj beats its own by the
    margin (gated=True). Candidates must be ACT, have an opponent,
    and not already be in the lineup."""
    kdt = _parse_ko(out_ko)
    if kdt is not None and kdt <= now_utc:
        return None, None, False
    status = (out_status or "").upper()
    must_move = (out_ko is None) or (status and status != "ACT")
    cands = [r for r in rows
             if r["player_id"] not in seen
             and r["player_id"] in unstarted
             and r.get("opponent")
             and (r["status"] or "").upper() == "ACT"]
    if not cands:
        return None, None, False
    cands.sort(key=lambda r: (-r["adj_pts"], -r["proj_pts"], r["name"]))
    best = cands[0]
    if must_move:
        return best["player_id"], best["adj_pts"], False
    if out_adj is None:
        return None, None, False
    edge = best["adj_pts"] - out_adj
    if edge >= margin:
        return best["player_id"], edge, True
    return None, None, False


def run_smart_subs(con, season: int, now_et: datetime = None) -> str:
    """CPU Smart's sub decisions for the current week, staging only."""
    if not _staging_db():
        return "smart subs skipped: not the staging database"
    now_et = now_et or datetime.now(ET)
    now_utc = now_et.astimezone(timezone.utc)
    week = _db.current_pick_week(con, season)
    mid = _smart_id(con)
    if mid is None:
        return "smart subs skipped: no CPU Smart member"
    margin = get_smart_margin(con)
    rows = _scoring.projections(con, season, week - 1)
    _matchup.annotate_rows(con, season, week, week - 1, rows,
                           _scoring.scoring_map(con))
    ko_by_pid = {}
    for r in rows:
        ko_by_pid[r["player_id"]] = _db.player_kickoff(
            con, r["player_id"], season, week)

    def _ko(pid):
        if pid in ko_by_pid:
            return ko_by_pid[pid]
        return _db.player_kickoff(con, pid, season, week)

    # ---- Pre-kickoff: full re-pick ability, free edits ----
    # Any pick whose game hasn't started can move: inactive/bye picks
    # always move, active picks move only on a 1.0+ Adj edge. The
    # pool is every unstarted player (any window — members enjoy the
    # same freedom on pre-kickoff edits); the margin is the only
    # stabilizer, and it is enough: Adj is fixed during the week, so
    # a swap strictly improves the slot and can never oscillate.
    stints = _scoring.member_week_stints(con, mid, season, week)
    seen = {s["player_id"] for s in stints}
    unstarted = set()
    for pid, ko in ko_by_pid.items():
        kdt = _parse_ko(ko)
        if kdt is not None and kdt > now_utc:
            unstarted.add(pid)
    adj = {r["player_id"]: r for r in rows}
    edited = []
    for s in stints:
        if not s["active"]:
            continue
        pid = s["player_id"]
        prow = con.execute(
            "SELECT status FROM players WHERE player_id = ?",
            (pid,)).fetchone()
        status = (prow["status"] if prow else "") or ""
        out_row = adj.get(pid)
        new_pid, edge, gated = _repick_choice(
            pid, status, _ko(pid),
            out_row["adj_pts"] if out_row else None,
            rows, seen, unstarted, now_utc, margin)
        if new_pid is None:
            continue
        new_name = adj[new_pid]["name"] if new_pid in adj else new_pid
        con.execute(
            "UPDATE weekly_picks SET player_id = ? "
            "WHERE member_id = ? AND season = ? AND week = ? "
            "AND player_id = ?",
            (new_pid, mid, season, week, pid))
        _log_decision(con, mid, season, week, "repick", pid, new_pid,
                      edge, gated)
        # Audit: Smart re-pick (links the smart_decisions row by kind).
        _audit.log(con, season, week, "repick", "smart_repick",
                   member_id=mid, player_id=new_pid,
                   source_feed="smart", actor="cpu-smart",
                   detail={"out_player_id": pid, "edge": edge,
                           "gated": gated})
        seen.discard(pid)
        seen.add(new_pid)
        edited.append(f"{s['name']} -> {new_name}")
    if edited:
        con.commit()
    edit_note = (f" (pre-kickoff edits: {', '.join(edited)})"
                 if edited else "")

    # ---- Live: at most one sub per cycle, the largest edge ----
    subs = _scoring.week_subs(con, mid, season, week)
    if len(subs) >= _scoring.MAX_SUBS:
        return f"smart subs week {week}: subs used up{edit_note}"
    stints = _scoring.member_week_stints(con, mid, season, week)
    seen = {s["player_id"] for s in stints}
    live_stints = []
    for s in stints:
        if not s["active"]:
            continue
        ko = _ko(s["player_id"])
        kdt = _parse_ko(ko)
        if kdt is None or not (kdt <= now_utc < kdt + timedelta(hours=4)):
            continue
        live_stints.append((s, ko))
    if not live_stints:
        return f"smart subs week {week}: no live games{edit_note}"
    states = _gamestate.fetch_states(week)
    best = None
    for s, ko in live_stints:
        live_out, label = _game_live(con, s["team"], s["player_id"],
                                     season, week, states)
        if not live_out:
            continue
        out_row = adj.get(s["player_id"])
        if out_row is None:
            continue
        prog_out = _game_progress(s["team"], states, ko, now_utc)
        if prog_out is None:
            continue
        rem_out = out_row["adj_pts"] * (1.0 - prog_out)
        for r in rows:
            if r["player_id"] in seen:
                continue
            if not r.get("opponent"):
                continue
            if (r["status"] or "").upper() != "ACT":
                continue
            if ko_by_pid.get(r["player_id"]) != ko:
                continue
            live_in, _ = _game_live(con, r["team"], r["player_id"],
                                    season, week, states)
            if not live_in:
                continue
            prog_in = _game_progress(r["team"], states, ko, now_utc)
            if prog_in is None:
                continue
            rem_in = r["adj_pts"] * (1.0 - prog_in)
            edge = rem_in - rem_out
            if edge >= margin and (best is None
                                   or edge > best[0]):
                best = (edge, s, r, label)
    if best is None:
        return f"smart subs week {week}: no move{edit_note}"
    edge, out_stint, in_row, label = best
    _, out_stats = _scoring.player_week_points(
        con, out_stint["player_id"], season, week)
    _, in_stats = _scoring.player_week_points(
        con, in_row["player_id"], season, week)
    cur = con.execute(
        "INSERT INTO weekly_subs (member_id, season, week, out_player_id, "
        "in_player_id, subbed_at, game_label, out_stats, in_stats) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (mid, season, week, out_stint["player_id"], in_row["player_id"],
         _db.utcnow_iso(), label, json.dumps(out_stats),
         json.dumps(in_stats)))
    sub_id = cur.lastrowid
    con.execute(
        "INSERT INTO sub_events (member_id, season, week, event, "
        "out_player_id, in_player_id, detail, created_at) "
        "VALUES (?,?,?,'applied',?,?,?,?)",
        (mid, season, week, out_stint["player_id"], in_row["player_id"],
         f"cpu-smart edge {edge:.1f}", _db.utcnow_iso()))
    _log_decision(con, mid, season, week, "sub",
                  out_stint["player_id"], in_row["player_id"], edge, True)
    # Audit: Smart live sub, as the member pair (links weekly_subs row).
    _audit.log(con, season, week, "pick_subbed_out", "live_sub",
               member_id=mid, player_id=out_stint["player_id"],
               source_feed="smart", actor="cpu-smart",
               detail={"sub_row": sub_id,
                       "in_player_id": in_row["player_id"], "edge": edge})
    _audit.log(con, season, week, "pick_subbed", "live_sub",
               member_id=mid, player_id=in_row["player_id"],
               source_feed="smart", actor="cpu-smart",
               detail={"sub_row": sub_id,
                       "out_player_id": out_stint["player_id"],
                       "edge": edge})
    con.commit()
    msg = (f"smart subs week {week}: {out_stint['name']} -> "
           f"{in_row['name']} (edge {edge:.1f})")
    return msg + edit_note


# ---- CPU Smart learning loop (Week 8) -------------------------------
# The margin is no longer just a constant: every gated decision (live
# subs + advantage re-picks; forced inactive/bye moves excluded) is
# recorded in smart_decisions with its predicted edge, and once its
# week closes the actual net is settled. After each closed week the
# margin auto-tunes from the cumulative settled gated record:
#   delta = LEARN_RATE * (mean predicted edge - mean actual net),
#   clamped to +/-MAX_STEP per week and to the [FLOOR, CEIL] band.
# Overpromising (edges bigger than nets) raises the margin — Smart
# demands more evidence before acting. Underpromising lowers it.
# Tuning needs MIN_CALLS settled gated decisions, so early weeks only
# collect evidence. Settled once: later stat corrections do not
# re-tune history. The pass is idempotent (settled rows are skipped,
# tuning converges), so it can run every poller cycle.

def _tune_margin(old, edges, nets):
    """(new_margin, info) from the cumulative gated record. Pure."""
    n = len(edges)
    if n < MIN_CALLS:
        return old, f"{n}/{MIN_CALLS} judged calls"
    avg_edge = sum(edges) / n
    avg_net = sum(nets) / n
    delta = LEARN_RATE * (avg_edge - avg_net)
    delta = max(-MAX_STEP, min(MAX_STEP, delta))
    new = max(MARGIN_FLOOR, min(MARGIN_CEIL, round(old + delta, 2)))
    info = (f"{n} judged calls, avg edge {avg_edge:.2f}, "
            f"avg net {avg_net:.2f}")
    return new, info


def learning_summary(con, season: int):
    """Admin display for the learning loop; None when the ledger
    predates this deploy. Never raises."""
    try:
        _ensure_decisions(con)
        margin = get_smart_margin(con)
        recs = con.execute(
            "SELECT week, kind, edge, net FROM smart_decisions "
            "WHERE season = ? AND gated = 1 AND settled = 1 "
            "ORDER BY id", (season,)).fetchall()
        judged = len(recs)
        avg_edge = (round(sum(r["edge"] for r in recs) / judged, 2)
                    if judged else None)
        avg_net = (round(sum(r["net"] for r in recs) / judged, 2)
                   if judged else None)
        recent = [{"week": r["week"], "kind": r["kind"],
                   "edge": r["edge"], "net": r["net"]}
                  for r in recs[-5:]]
        return {"margin": margin, "judged": judged,
                "avg_edge": avg_edge, "avg_net": avg_net,
                "recent": recent}
    except Exception:
        return None


def run_smart_learning(con, season: int, now_et: datetime = None) -> str:
    """Settle just-closed weeks' Smart decisions, then auto-tune the
    margin. Staging only; safe no-op otherwise. Never breaks the poll."""
    if not _staging_db():
        return "smart learning skipped: not the staging database"
    now_et = now_et or datetime.now(ET)
    week = _db.current_pick_week(con, season)
    mid = _smart_id(con)
    if mid is None:
        return "smart learning skipped: no CPU Smart member"
    _ensure_decisions(con)
    # Settle every unsettled decision from a closed week. Sub nets
    # come from the graded sub outcomes; re-pick nets are full-week
    # points gained vs standing pat. A bad row never blocks the rest.
    settled = 0
    for d in con.execute(
            "SELECT * FROM smart_decisions WHERE member_id = ? "
            "AND season = ? AND settled = 0 AND week < ?",
            (mid, season, week)).fetchall():
        try:
            net = None
            if d["kind"] == "sub":
                for o in _scoring.sub_outcomes(con, mid, season,
                                               d["week"]):
                    if (o["out_player_id"] == d["out_player_id"]
                            and o["in_player_id"] == d["in_player_id"]):
                        net = o["net"]
                        break
            else:
                out_pts, _ = _scoring.player_week_points(
                    con, d["out_player_id"], season, d["week"])
                in_pts, _ = _scoring.player_week_points(
                    con, d["in_player_id"], season, d["week"])
                net = round(in_pts - out_pts, 2)
            if net is None:
                continue
            con.execute(
                "UPDATE smart_decisions SET net = ?, settled = 1, "
                "settled_at = ? WHERE id = ?",
                (net, _db.utcnow_iso(), d["id"]))
            settled += 1
        except Exception:
            continue
    if settled:
        con.commit()
    # Tune from the cumulative settled gated record.
    old = get_smart_margin(con)
    recs = con.execute(
        "SELECT edge, net FROM smart_decisions WHERE member_id = ? "
        "AND season = ? AND gated = 1 AND settled = 1",
        (mid, season)).fetchall()
    if len(recs) >= MIN_CALLS:
        new, info = _tune_margin(old, [r["edge"] for r in recs],
                                 [r["net"] for r in recs])
        if new != old:
            _set_smart_margin(con, new)
            note = f"; margin {old:.2f} -> {new:.2f} ({info})"
        else:
            note = f"; margin holds at {old:.2f} ({info})"
    else:
        note = (f"; collecting evidence "
                f"({len(recs)}/{MIN_CALLS} judged calls)")
    return f"smart learning: settled {settled} decisions{note}"
