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
  is_test    INTEGER NOT NULL DEFAULT 0 -- 1 = test member, hidden from real standings
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
