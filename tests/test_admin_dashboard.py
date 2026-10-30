"""Unit tests for the admin dashboard build (2026-10-30.3).

Per-member auto-pick method resolution, the deactivated-member
exclusion from standings, and the admin-Off skip in the member
auto-pick fill. Run with: python3 tests/test_admin_dashboard.py
"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import cpu_picks as _cpu  # noqa: E402
from app import scoring  # noqa: E402


def _con():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE members (id INTEGER PRIMARY KEY, name TEXT, "
        "pick_token TEXT, is_test INTEGER DEFAULT 0, "
        "auto_picks INTEGER DEFAULT 0, member_auto TEXT, "
        "is_active INTEGER DEFAULT 1)")
    con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    con.execute(
        "CREATE TABLE weekly_picks (member_id INTEGER, player_id TEXT, "
        "season INTEGER, week INTEGER, submitted_at TEXT, source TEXT)")
    con.execute(
        "CREATE TABLE weekly_subs (id INTEGER PRIMARY KEY, member_id INTEGER, "
        "season INTEGER, week INTEGER, out_player_id TEXT, in_player_id TEXT, "
        "out_stats TEXT, in_stats TEXT, subbed_at TEXT)")
    con.execute(
        "CREATE TABLE players (player_id TEXT PRIMARY KEY, name TEXT, team TEXT)")
    con.execute(
        "CREATE TABLE scoring_config (stat_key TEXT PRIMARY KEY, points REAL)")
    for k, v in [("tackles", 0.1), ("sacks", 2), ("forced_fumbles", 3),
                 ("fumble_recoveries", 3), ("interceptions", 3),
                 ("safeties", 4), ("def_tds", 6)]:
        con.execute("INSERT INTO scoring_config VALUES (?, ?)", (k, v))
    con.execute(
        "CREATE TABLE games (season INTEGER, week INTEGER, kickoff_utc TEXT)")
    # A future kickoff so current_pick_week returns week 8.
    con.execute(
        "INSERT INTO games VALUES (2026, 8, '2030-01-01T00:00:00+00:00')")
    return con


class TestEffectiveMethod(unittest.TestCase):
    def test_explicit_wins_over_global(self):
        con = _con()
        con.execute("INSERT INTO settings VALUES ('auto_pick_method', 'smart')")
        row = {"member_auto": "off"}
        self.assertEqual(_cpu.effective_member_method(con, row), "off")
        row = {"member_auto": "random"}
        self.assertEqual(_cpu.effective_member_method(con, row), "random")

    def test_null_follows_global(self):
        con = _con()
        con.execute("INSERT INTO settings VALUES ('auto_pick_method', 'smart')")
        self.assertEqual(_cpu.effective_member_method(con, {"member_auto": None}),
                         "smart")

    def test_null_no_global_defaults_random(self):
        con = _con()
        self.assertEqual(_cpu.effective_member_method(con, {"member_auto": None}),
                         "random")


class TestDeactivatedExcluded(unittest.TestCase):
    def test_standings_skips_deactivated(self):
        con = _con()
        con.execute("INSERT INTO members (name, is_test, is_active) VALUES ('A', 0, 1)")
        con.execute("INSERT INTO members (name, is_test, is_active) VALUES ('B', 0, 0)")
        con.execute("INSERT INTO members (name, is_test, is_active) VALUES ('C', 1, 1)")
        table, test_table = scoring.standings(con, 2026, through_week=0)
        self.assertEqual([r[1] for r in table], ["A"])
        self.assertEqual([r[1] for r in test_table], ["C"])


class TestMemberFillOff(unittest.TestCase):
    def test_admin_off_skips_fill(self):
        con = _con()
        con.execute(
            "INSERT INTO members (name, auto_picks, member_auto, is_active) "
            "VALUES ('M', 1, 'off', 1)")
        out = _cpu.run_member_auto_picks(con, 2026)
        self.assertIn("nothing open", out)
        n = con.execute("SELECT COUNT(*) c FROM weekly_picks").fetchone()["c"]
        self.assertEqual(n, 0)

    def test_deactivated_skipped(self):
        con = _con()
        con.execute(
            "INSERT INTO members (name, auto_picks, member_auto, is_active) "
            "VALUES ('M', 1, 'smart', 0)")
        out = _cpu.run_member_auto_picks(con, 2026)
        self.assertIn("nothing open", out)


if __name__ == "__main__":
    unittest.main()
