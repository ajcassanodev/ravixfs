# ravixfs — NFL Defensive Prime5

League website for Adam's NFL defensive-player Prime5 game (test year 2026–27).
Members draft one exclusive locked player, then submit 4 fresh defensive picks
each week. Points come from real NFL stats, refreshed every 15 minutes during
games.

**Live site:** https://ravixfs.com

## How it works

- **Data:** nflverse release CSVs (weekly player stats, rosters, schedules) —
  free, no API key. See `DATA_NOTES.md` (kept out of the repo) for the exact
  stat → column mapping.
- **Scoring:** points are *computed*, never stored:
  `weekly_stats × scoring_config`. The commissioner can change scoring
  mid-season from the admin page and every past week recalculates instantly.
- **Picks:** each member gets a secret pick link (`/submit/<token>`). Each pick
  locks individually at its own game's kickoff — a Thursday pick doesn't lock
  your Sunday picks. Late picks for started games are rejected at submit time.
- **Auth model:** member pick tokens + one admin token (`ADMIN_TOKEN` env var).
  No passwords, no accounts. Fine for a test-year league of friends; do not
  use this auth model for anything handling money.

## Run locally

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
RAVIXFS_DB=$PWD/ravixfs.db venv/bin/python -c "from app.db import init_db; init_db()"
ADMIN_TOKEN=local-dev RAVIXFS_DB=$PWD/ravixfs.db venv/bin/uvicorn app.main:app --reload
# data (writes to RAVIXFS_DB):
RAVIXFS_DB=$PWD/ravixfs.db venv/bin/python poller/poll.py --check  # dry run
RAVIXFS_DB=$PWD/ravixfs.db venv/bin/python poller/poll.py
```

Open http://127.0.0.1:8000. Create members and record the draft at
`/admin/<ADMIN_TOKEN>`.

## Deploy

The repo must be **public** so the server can pull code without credentials
(it contains no secrets — tokens live in `/etc/ravixfs.env` on the server).

1. Provision Ubuntu 24.04 (Hetzner CX22, Ashburn).
2. In the Hetzner web console (as root), run:
   `curl -fsSL https://raw.githubusercontent.com/ajcassanodev/ravixfs/main/deploy/bootstrap.sh | bash`
   Save the printed admin token.
3. Point `ravixfs.com` A record at the server IP. Caddy fetches the HTTPS cert.

What bootstrap installs: Caddy (reverse proxy → localhost:8000), the FastAPI
app as a systemd service, a 15-min data poller timer, a 5-min `git pull`
self-update timer (restarts the app on change), UFW (22/80/443), and
unattended security upgrades.

## Layout

- `app/` — FastAPI site (standings, week detail, draft board, trades, pick
  submission, admin)
- `poller/poll.py` — nflverse → SQLite
- `schema.sql` — full DB schema
- `deploy/` — server setup + systemd units + Caddyfile
