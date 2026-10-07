# Shared member identity — design

**Goal (Adam, 2026-10-07):** one member account across all Ravix apps.
Anyone Adam sends a pick link to can use every application — Pick5, the
football pool, and future games — without a second invite.

**Status:** foundation implemented 2026-10-07 (`shared/` package). Pick5 on
staging reads/writes the shared session (additive only). The football pool
(`pool/`, branch `staging-pool`) is the first app built on it.

## 1. Per-environment shared member store

Each environment has exactly one shared member SQLite DB, used by every game
app in that environment:

| env     | SHARED_MEMBER_DB                          | apps sharing it                              |
|---------|-------------------------------------------|----------------------------------------------|
| staging | /opt/ravixfs-shared-staging/shared.db     | staging.ravixfs.com, staging-footballpool…   |
| prod    | /opt/ravixfs-shared-prod/shared.db        | prod-pick5f.ravixfs.com, future prod games…  |

This mirrors the existing separate-DBs rule: staging and prod never share a
member row, so **test members can never leak into prod**. Each game keeps its
own DB for game data (picks, scores, config); the shared DB holds identity
only.

## 2. Schema (`shared/schema.sql`)

- `members(id, name, pick_token UNIQUE, email, password_hash, is_test, created_at)`
  - `pick_token`: the legacy private pick-link token. Preserved forever —
    every old pick link keeps working.
  - `email` / `password_hash`: reserved now, used by the invite-only
    password login that builds **after** the staging test pass.
- `game_memberships(member_id, game_key, created_at)` — which games each
  member is in (`pick5f`, `pool`, …). A member with no row for a game simply
  hasn't been invited there yet; opening that game's pick link grants it.

## 3. Token preservation & migration

- The shared store never mints a new token for an existing member:
  `import_member()` upserts on `pick_token`.
- **Backfill:** `python -m shared.migrate --from <per-game db> --game pick5f`
  copies a legacy `members` table into the shared store (staging first, prod
  later). Idempotent — safe to re-run.
- **Lazy import (the steady state):** when any app is shown a pick token it
  doesn't know, and `POOL_IMPORT_DB` (or the game's equivalent) points at a
  legacy DB, the app imports that member on the spot and grants the game.
  Result: a pick link issued by *any* game works in *every* game, even if the
  backfill never ran.

## 4. Cross-subdomain session

- Cookie `ravix_member`, value `member_id.env.sig`
  (`sig = HMAC-SHA256(SESSION_SECRET_<ENV>, "member_id.env")`).
- `Domain=.ravixfs.com` (parent-domain scope — every game subdomain sees
  it), `Path=/`, `HttpOnly`, `Secure`, `SameSite=Lax`, 180-day expiry.
  On localhost the Domain/Secure attrs are omitted so local testing works.
- The `env` claim (`staging`|`prod`, from `RAVIX_ENV`) is part of the signed
  payload: a staging-issued session is cryptographically unusable on prod
  and vice versa, even though both live under `.ravixfs.com`.
- Secrets: `SESSION_SECRET_STAGING` / `SESSION_SECRET_PROD` in
  `/etc/ravixfs.env` (same pattern as `ADMIN_TOKEN`), generated once per
  environment and never committed. Every app in an env uses the same secret
  so any of them can verify a session any other one issued.
- **Opening any pick link establishes the session** (`/submit/{token}` on
  Pick5, `/pick/{token}` on the pool). A new `/pick` route on each app
  resolves the session to that member's pick link — the "log me in
  everywhere" entry point.

## 5. Migration plan for the Pick5 app

1. **Staging (done 2026-10-07):** `shared/` ships on the `staging` branch;
   `app/main.py` gains the additive session hooks (set cookie on pick-link
   open, `GET /pick` session→link redirect). No existing route changed, no
   existing pick link affected. Staging auto-deploys via the sync timer.
2. **Server (Adam):** add `SESSION_SECRET_STAGING` to `/etc/ravixfs.env`,
   add `SHARED_MEMBER_DB` + `RAVIX_ENV=staging` to the staging unit,
   restart. Run the backfill once.
3. **Pool staging:** same pattern on `staging-pool` (its own checkout, DB,
   units). The pool lazy-imports Pick5-issued tokens from the Pick5 staging
   DB, so Adam's existing test members work there immediately.
4. **Prod (after the staging test pass):** merge `shared/` to `main`;
   same env vars with `RAVIX_ENV=prod`, `SESSION_SECRET_PROD`, prod shared
   DB; run the backfill against the prod Pick5 DB. Prod pick links keep
   working unchanged.

## 6. How the decided password login plugs in later

The invite-only design (commissioner adds member in admin → one-time setup
link → member sets password; email+password login; private pick links keep
working as fallback) needs no schema change: `email` and `password_hash`
already exist. The session cookie stays the mechanism — password login just
becomes a second way to *issue* it, alongside opening a pick link. Planned
additions at that time: `setup_token` one-time links, `/login`,
`/forgot-password`, `/change-password` routes, commissioner role flag.

## 7. Open decisions for Adam

- **Tie games in the pool:** currently 0 points for everyone on a tied
  game (no winner). Alternatives: 0.5 each, or refund the game. Say the word
  and it changes in one line (`pool/main.py: _score_game`).
- **Public "my games" hub:** not built yet — a landing-page or
  `my.ravixfs.com` page listing the games your session is in. Easy once
  sessions are live everywhere.
- **Email collection:** `email` is NULL for everyone until the password
  phase; pick links remain the only identifier until then.
