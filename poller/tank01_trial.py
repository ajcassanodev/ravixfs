"""Tank01 trial harness: snapshot live box scores during the trial window.

One-night trial (Bills @ Rams, Mon 2026-10-12, 8:15pm ET): polls Tank01's
getNFLBoxScore on every poller run inside the trial window and stores the
raw payloads, so per-player defensive stat freshness can be compared against
nflverse after the game. Safe no-op when TANK01_API_KEY is unset or outside
the window. Failures never break the main poll.
"""
import json
import os
import urllib.request
from datetime import datetime, timezone

API_HOST = "tank01-nfl-live-in-game-real-time-statistics-nfl.p.rapidapi.com"
GAME_ID = "20261012_BUF@LAR"  # Bills @ Rams, Mon 2026-10-12 8:15pm ET
# Trial window (UTC): 7:00pm ET Mon -> 1:00am ET Tue. Kickoff through final.
WINDOW_START = datetime(2026, 10, 12, 23, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 10, 13, 5, 0, tzinfo=timezone.utc)

# Tank01 Defense category -> ravixfs scoring key (for the post-game compare).
FIELD_MAP = {
    "totalTackles": "tackles",
    "sacks": "sacks",
    "defensiveInterceptions": "interceptions",
    "forcedFumbles": "forced_fumbles",
    "fumblesRecovered": "fumble_recoveries",
    "defTD": "def_tds",
    # Note: safeties are team-level in every NFL box score, not per-player.
}


def run_trial(con) -> str:
    key = os.environ.get("TANK01_API_KEY", "").strip()
    now = datetime.now(timezone.utc)
    if not key:
        return "tank01 trial: no API key, skipping"
    if not (WINDOW_START <= now <= WINDOW_END):
        return "tank01 trial: outside trial window, skipping"
    con.execute(
        "CREATE TABLE IF NOT EXISTS tank01_snapshots ("
        "fetched_at TEXT NOT NULL, game_id TEXT NOT NULL, payload TEXT NOT NULL)"
    )
    url = f"https://{API_HOST}/getNFLBoxScore?gameID={GAME_ID}"
    req = urllib.request.Request(url, headers={
        "x-rapidapi-key": key,
        "x-rapidapi-host": API_HOST,
        "User-Agent": "ravixfs-trial/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode("utf-8")
        data = json.loads(payload)
    except Exception as e:
        return f"tank01 trial: fetch failed: {e}"
    pstats = data.get("playerStats") or {}
    n_def = sum(
        1 for p in pstats.values()
        if isinstance(p, dict) and p.get("Defense")
    )
    con.execute(
        "INSERT INTO tank01_snapshots (fetched_at, game_id, payload) "
        "VALUES (?,?,?)",
        (now.isoformat(), GAME_ID, payload),
    )
    con.commit()
    status = data.get("gameStatus") or data.get("gameStatusCode") or "?"
    return f"tank01 trial: snapshot stored ({n_def} defenders, status={status})"
