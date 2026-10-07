-- Football pool game DB. Member identity lives in the shared store
-- (shared/schema.sql); pool_picks.member_id references shared members.id.
-- Scores come from the nflverse schedules feed (see poll.py); the
-- commissioner's per-week game selection is the `included` flag, which the
-- poller never overwrites.

CREATE TABLE IF NOT EXISTS pool_games (
  season      INTEGER NOT NULL,
  week        INTEGER NOT NULL,
  away_team   TEXT NOT NULL,
  home_team   TEXT NOT NULL,
  kickoff_utc TEXT NOT NULL,             -- ISO-8601 UTC
  away_score  INTEGER,                   -- NULL until final
  home_score  INTEGER,                   -- NULL until final
  is_final    INTEGER NOT NULL DEFAULT 0,
  included    INTEGER NOT NULL DEFAULT 1, -- commissioner: in the pool this week?
  PRIMARY KEY (season, week, away_team, home_team)
);
CREATE INDEX IF NOT EXISTS idx_pool_games_kickoff ON pool_games(kickoff_utc);

CREATE TABLE IF NOT EXISTS pool_picks (
  member_id    INTEGER NOT NULL,          -- shared member id
  season       INTEGER NOT NULL,
  week         INTEGER NOT NULL,
  away_team    TEXT NOT NULL,
  home_team    TEXT NOT NULL,
  picked_team  TEXT NOT NULL,
  submitted_at TEXT NOT NULL,            -- ISO-8601 UTC, enforces lock rule
  PRIMARY KEY (member_id, season, week, away_team, home_team)
);
CREATE INDEX IF NOT EXISTS idx_pool_picks_member ON pool_picks(member_id, season, week);
