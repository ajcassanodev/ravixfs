-- ravixfs schema (SQLite). Points are NEVER stored: they are computed
-- from weekly_stats x scoring_config by the scoring engine (app/scoring.py).

CREATE TABLE IF NOT EXISTS players (
  player_id         TEXT PRIMARY KEY,   -- GSIS id, e.g. '00-0023459'
  name              TEXT NOT NULL,      -- display name
  team              TEXT,               -- current team abbrev
  position          TEXT,               -- detailed: DE, CB, ILB, ...
  position_group    TEXT,               -- DB, DL, LB, ...
  status            TEXT,               -- ACT, INA, RES, ...
  source_updated_at TEXT                -- ISO ts of last poller touch
);
CREATE INDEX IF NOT EXISTS idx_players_team ON players(team);
CREATE INDEX IF NOT EXISTS idx_players_group ON players(position_group);

CREATE TABLE IF NOT EXISTS weekly_stats (
  player_id       TEXT NOT NULL,
  season          INTEGER NOT NULL,
  week            INTEGER NOT NULL,
  tackles         REAL NOT NULL DEFAULT 0,  -- solo + assists
  sacks           REAL NOT NULL DEFAULT 0,
  interceptions   REAL NOT NULL DEFAULT 0,
  forced_fumbles  REAL NOT NULL DEFAULT 0,
  fumble_recoveries REAL NOT NULL DEFAULT 0,
  safeties        REAL NOT NULL DEFAULT 0,
  def_tds         REAL NOT NULL DEFAULT 0,  -- def_tds + fumble_recovery_tds
  PRIMARY KEY (player_id, season, week)
);

CREATE TABLE IF NOT EXISTS games (
  season      INTEGER NOT NULL,
  week        INTEGER NOT NULL,
  away_team   TEXT NOT NULL,
  home_team   TEXT NOT NULL,
  kickoff_utc TEXT NOT NULL,             -- ISO-8601 UTC
  PRIMARY KEY (season, week, away_team, home_team)
);
CREATE INDEX IF NOT EXISTS idx_games_kickoff ON games(kickoff_utc);

CREATE TABLE IF NOT EXISTS members (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  pick_token TEXT NOT NULL UNIQUE,      -- secret per-member submit URL
  is_test    INTEGER NOT NULL DEFAULT 0, -- 1 = test member, hidden from real standings
  phone      TEXT,                      -- E.164 mobile for SMS pick reminders
  sms_opt_in INTEGER NOT NULL DEFAULT 0, -- 1 = text me Thursday mornings
  email      TEXT,                      -- address for email pick reminders
  email_opt_in INTEGER NOT NULL DEFAULT 0 -- 1 = email me Thursday mornings
);

-- Thursday-morning pick reminders already sent (one per member per week).
CREATE TABLE IF NOT EXISTS sms_reminders (
  season    INTEGER NOT NULL,
  week      INTEGER NOT NULL,
  member_id INTEGER NOT NULL REFERENCES members(id),
  sent_at   TEXT NOT NULL,              -- ISO-8601 UTC
  PRIMARY KEY (season, week, member_id)
);

-- Thursday-morning pick reminder emails already sent (one per member per week).
CREATE TABLE IF NOT EXISTS email_reminders (
  season    INTEGER NOT NULL,
  week      INTEGER NOT NULL,
  member_id INTEGER NOT NULL REFERENCES members(id),
  sent_at   TEXT NOT NULL,              -- ISO-8601 UTC
  PRIMARY KEY (season, week, member_id)
);

-- One active row per member (partial unique index enforces it).
CREATE TABLE IF NOT EXISTS locked_players (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id    INTEGER NOT NULL REFERENCES members(id),
  player_id    TEXT NOT NULL REFERENCES players(player_id),
  acquired_via TEXT NOT NULL,           -- draft | redraft | trade
  since_week   INTEGER NOT NULL,        -- season week it became locked
  season       INTEGER NOT NULL,
  active       INTEGER NOT NULL DEFAULT 1,
  created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_locked_active
  ON locked_players(member_id, season) WHERE active = 1;

CREATE TABLE IF NOT EXISTS weekly_picks (
  member_id    INTEGER NOT NULL REFERENCES members(id),
  player_id    TEXT NOT NULL REFERENCES players(player_id),
  season       INTEGER NOT NULL,
  week         INTEGER NOT NULL,
  submitted_at TEXT NOT NULL,           -- ISO-8601 UTC, enforces lock rule
  PRIMARY KEY (member_id, season, week, player_id)
);

CREATE TABLE IF NOT EXISTS trades (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  season             INTEGER NOT NULL,
  week               INTEGER NOT NULL,
  giver_member_id    INTEGER NOT NULL REFERENCES members(id),
  receiver_member_id INTEGER NOT NULL REFERENCES members(id),
  player_id          TEXT NOT NULL REFERENCES players(player_id),
  recorded_at        TEXT NOT NULL
);

-- stat_key matches weekly_stats column names. Change points here and every
-- past week recalculates -- no data migration needed (test-year friendly).
CREATE TABLE IF NOT EXISTS scoring_config (
  stat_key TEXT PRIMARY KEY,
  points   REAL NOT NULL
);
INSERT OR IGNORE INTO scoring_config (stat_key, points) VALUES
  ('tackles', 0.1),
  ('sacks', 2),
  ('forced_fumbles', 3),
  ('fumble_recoveries', 3),
  ('interceptions', 3),
  ('safeties', 4),
  ('def_tds', 6);

-- Team offensive stats (for matchup intelligence): per-team per-week
-- aggregates from nflverse stats_player_week. Refreshed by the poller.
-- ints_thrown/sacks_allowed/fumbles_lost are opponent-tendency inputs;
-- plays approximates offensive snaps (tackle opportunity).
CREATE TABLE IF NOT EXISTS offense_stats (
  season        INTEGER NOT NULL,
  week          INTEGER NOT NULL,
  team          TEXT NOT NULL,
  ints_thrown   REAL NOT NULL DEFAULT 0,
  sacks_allowed REAL NOT NULL DEFAULT 0,
  fumbles_lost  REAL NOT NULL DEFAULT 0,
  plays         REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (season, week, team)
);

-- Live in-game defensive stats (Tank01). Per-player rows for the current
-- week while games are in progress. app/scoring.py prefers nflverse
-- weekly_stats whenever a final row exists; live_stats only fills the gap.
CREATE TABLE IF NOT EXISTS live_stats (
  player_id       TEXT NOT NULL,
  season          INTEGER NOT NULL,
  week            INTEGER NOT NULL,
  tackles         REAL NOT NULL DEFAULT 0,
  sacks           REAL NOT NULL DEFAULT 0,
  interceptions   REAL NOT NULL DEFAULT 0,
  forced_fumbles  REAL NOT NULL DEFAULT 0,
  fumble_recoveries REAL NOT NULL DEFAULT 0,
  safeties        REAL NOT NULL DEFAULT 0,
  def_tds         REAL NOT NULL DEFAULT 0,
  game_id         TEXT NOT NULL DEFAULT '',
  fetched_at      TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (player_id, season, week)
);

-- Tank01 API call budget guard (free tier: 1,000 req/month).
CREATE TABLE IF NOT EXISTS tank01_usage (
  month TEXT NOT NULL PRIMARY KEY,
  calls INTEGER NOT NULL DEFAULT 0
);

-- Scoring events (Tank01 live scoring): one row per per-stat increase seen
-- by the poller, powering the "what just happened" notification popups.
-- The 1-min and 15-min pollers share live_stats as their high-water mark;
-- UNIQUE(player_id, season, week, stat_key, new_total) makes the rare
-- same-second race between them a harmless no-op instead of a duplicate.
CREATE TABLE IF NOT EXISTS scoring_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  player_id     TEXT NOT NULL,
  season        INTEGER NOT NULL,
  week          INTEGER NOT NULL,
  stat_key      TEXT NOT NULL,
  old_total     REAL NOT NULL DEFAULT 0,
  new_total     REAL NOT NULL DEFAULT 0,
  points_delta  REAL NOT NULL DEFAULT 0,
  game_id       TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL,
  UNIQUE (player_id, season, week, stat_key, new_total)
);
CREATE INDEX IF NOT EXISTS idx_scoring_events_recent
  ON scoring_events (season, week, created_at);

-- Live substitutions (Week 7 rule): a member may swap a player mid-game
-- for another player in the same kickoff window, up to 2 per week.
-- Points are never stored for picks, but a sub needs stat snapshots:
-- out_stats / in_stats are the players' cumulative weekly stats (JSON,
-- scoring.STAT_KEYS) captured at sub time. Scoring (app/scoring.py) turns
-- picks + subs into "stints": a stint scores (end - start) x config, so
-- the subbed-out player keeps what he banked and the replacement scores
-- only from the moment he enters.
CREATE TABLE IF NOT EXISTS weekly_subs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id     INTEGER NOT NULL REFERENCES members(id),
  season        INTEGER NOT NULL,
  week          INTEGER NOT NULL,
  out_player_id TEXT NOT NULL REFERENCES players(player_id),
  in_player_id  TEXT NOT NULL REFERENCES players(player_id),
  subbed_at     TEXT NOT NULL,   -- ISO-8601 UTC
  game_label    TEXT,            -- game clock at sub time, e.g. 'Q3 · 4:12'
  out_stats     TEXT NOT NULL,   -- JSON cumulative stats of OUT player at sub time
  in_stats      TEXT NOT NULL    -- JSON cumulative stats of IN player at sub time
);
CREATE INDEX IF NOT EXISTS idx_subs_member_week
  ON weekly_subs(member_id, season, week);
