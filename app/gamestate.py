"""Live NFL game state from ESPN's public scoreboard API.

Used by the /live page to show which picked players are in active games
and what quarter they're in. Results are cached in memory for 60 seconds
so page loads stay fast and we don't hammer the API. Any failure returns
an empty mapping and the caller falls back to kickoff times from the DB.
"""
import json
import time
import urllib.request

_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
_TTL = 60
_TIMEOUT = 8

# Sleeper/DB abbreviation -> ESPN abbreviation.
_TEAM_FIX = {"WAS": "WSH", "LA": "LAR"}

_cache = {"at": 0.0, "states": {}}


def _espn_abbr(team: str) -> str:
    return _TEAM_FIX.get((team or "").upper(), (team or "").upper())


def fetch_states() -> dict:
    """{ESPN_TEAM_ABBR: {"state": "pre"|"in"|"post", "detail": str,
    "period": int, "clock": str}} — cached 60s, empty dict on failure."""
    now = time.time()
    if now - _cache["at"] < _TTL and _cache["states"]:
        return _cache["states"]
    try:
        req = urllib.request.Request(
            _SCOREBOARD_URL, headers={"User-Agent": "ravixfs-live/1.0"}
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        states = {}
        for event in data.get("events", []):
            comps = event.get("competitions") or []
            if not comps:
                continue
            comp = comps[0]
            status = comp.get("status", {})
            stype = status.get("type", {}) or {}
            state = stype.get("state", "")  # pre | in | post
            detail = stype.get("shortDetail", "") or stype.get("detail", "")
            for c in comp.get("competitors", []):
                abbr = (c.get("team", {}) or {}).get("abbreviation", "")
                if abbr:
                    states[abbr.upper()] = {
                        "state": state,
                        "detail": detail,
                        "period": status.get("period", 0) or 0,
                        "clock": status.get("displayClock", "") or "",
                    }
        _cache["at"] = now
        _cache["states"] = states
        return states
    except Exception:
        # Serve stale cache if we have it, else empty (caller falls back).
        return _cache["states"]


def player_game(team: str, states: dict) -> dict | None:
    """Game state for a player's team, or None if the team isn't playing
    (bye week) or the feed is down."""
    return states.get(_espn_abbr(team))


def live_label(game: dict) -> str:
    """'Q2 · 9:33' style label for an in-progress game."""
    detail = (game.get("detail") or "").lower()
    if "halftime" in detail:
        return "Halftime"
    period = game.get("period", 0) or 0
    clock = game.get("clock", "") or ""
    q = {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4"}.get(period, "")
    if period > 4:
        q = "OT"
    if q and clock:
        return f"{q} · {clock}"
    return game.get("detail") or q or "Live"
