-- Shared member store (one per environment: staging apps share one DB,
-- prod apps share another). Game-specific data lives in each game's own DB;
-- this file holds only identity + which games each member is in.

CREATE TABLE IF NOT EXISTS members (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,
  pick_token    TEXT NOT NULL UNIQUE,  -- legacy private pick-link token; preserved forever
  email         TEXT,                  -- reserved for invite-only password login (post test pass)
  password_hash TEXT,                  -- reserved for invite-only password login (post test pass)
  is_test       INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS game_memberships (
  member_id  INTEGER NOT NULL REFERENCES members(id),
  game_key   TEXT NOT NULL,            -- 'pick5f', 'pool', ...
  created_at TEXT NOT NULL,
  PRIMARY KEY (member_id, game_key)
);
CREATE INDEX IF NOT EXISTS idx_game_memberships_game ON game_memberships(game_key);
