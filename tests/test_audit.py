"""Unit tests for the unified audit log (app/audit.py).

Pure helpers only: cause-label formatting, the last-value dedupe rule,
pick_locked idempotency, and tripwire orphan detection. Run with:
    python3 tests/test_audit.py
"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import audit  # noqa: E402


def _con():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    audit.ensure_table(con)
    return con


def _ev(kind, **kw):
    e = {"kind": kind, "cause": kw.pop("cause", "test"),
         "player_id": None, "member_id": None, "stat_key": None,
         "old_value": None, "new_value": None, "points_delta": None,
         "source_feed": None, "actor": None, "detail": {},
         "created_at": "2026-10-30T12:00:00+00:00"}
    e.update(kw)
    return e


class TestFormatEvent(unittest.TestCase):
    def test_pick_submitted(self):
        f = audit.format_event(_ev("pick_submitted", player_id="p1"),
                               {"p1": "T.J. Watt"})
        self.assertEqual(f["text"], "Picked T.J. Watt")
        self.assertEqual(f["tag"], "PICK")
        self.assertEqual(f["points"], "0.0")

    def test_pick_submitted_cpu(self):
        f = audit.format_event(_ev("pick_submitted", player_id="p1",
                                   detail={"cpu_method": "Smart"}),
                               {"p1": "T.J. Watt"})
        self.assertEqual(f["text"], "Picked T.J. Watt (CPU Smart)")

    def test_stat_corrected_sack(self):
        f = audit.format_event(_ev("stat_corrected", player_id="p1",
                                   stat_key="sacks", old_value=1, new_value=0,
                                   points_delta=-2.0),
                               {"p1": "T.J. Watt"})
        self.assertEqual(f["text"], "T.J. Watt · sacks corrected 1 → 0")
        self.assertEqual(f["points"], "−2.0")
        self.assertEqual(f["tag"], "CORRECTION")

    def test_tackle_drop_is_a_correction(self):
        f = audit.format_event(_ev("stat_corrected", player_id="p1",
                                   stat_key="tackles", old_value=13,
                                   new_value=12, points_delta=-0.1),
                               {"p1": "Fred Warner"})
        self.assertEqual(f["tag"], "CORRECTION")
        self.assertEqual(f["points"], "−0.1")

    def test_feed_switched(self):
        f = audit.format_event(_ev("feed_switched", player_id="p1",
                                   stat_key="tackles", old_value=13,
                                   new_value=12, points_delta=-0.1,
                                   detail={"from_feed": "tank01",
                                           "to_feed": "nflverse"}),
                               {"p1": "Fred Warner"})
        self.assertEqual(f["text"],
                         "Fred Warner · tackles 13 → 12 (final overrode live)")
        self.assertEqual(f["tag"], "FINAL")

    def test_repick(self):
        f = audit.format_event(_ev("repick", player_id="p2",
                                   detail={"out_player_id": "p1"}),
                               {"p1": "Old Pick", "p2": "New Pick"})
        self.assertEqual(f["text"], "Smart re-pick: Old Pick → New Pick")

    def test_admin_scoring_change(self):
        f = audit.format_event(_ev("admin_action",
                                   detail={"action": "scoring_change",
                                           "stat_key": "sacks",
                                           "points": 2}))
        self.assertEqual(f["text"], "Scoring change: sacks = 2 pts")
        self.assertEqual(f["tag"], "ADMIN")

    def test_unknown_kind_generic(self):
        f = audit.format_event(_ev("mystery_kind", player_id="p1"),
                               {"p1": "T.J. Watt"})
        self.assertEqual(f["text"], "T.J. Watt · mystery_kind")
        self.assertEqual(f["tag"], "PICK")


class TestDedupe(unittest.TestCase):
    def test_same_value_skipped_drop_then_reclimb_logged(self):
        con = _con()
        self.assertTrue(audit.log_stat_change(
            con, 2026, 8, "stat_observed", "p1", "tackles", 0, 12, 1.2,
            "tank01", "poll"))
        self.assertFalse(audit.log_stat_change(
            con, 2026, 8, "stat_observed", "p1", "tackles", 12, 12, 0.0,
            "tank01", "poll"))
        self.assertTrue(audit.log_stat_change(
            con, 2026, 8, "stat_corrected", "p1", "tackles", 12, 11, -0.1,
            "tank01", "poll"))
        self.assertTrue(audit.log_stat_change(
            con, 2026, 8, "stat_observed", "p1", "tackles", 11, 12, 0.1,
            "tank01", "poll"))
        n = con.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        self.assertEqual(n, 3)
        con.close()


class TestRecordLocks(unittest.TestCase):
    def _setup(self, con):
        con.execute("CREATE TABLE players (player_id TEXT, name TEXT, team TEXT)")
        con.execute("CREATE TABLE weekly_picks (member_id INT, player_id TEXT,"
                    " season INT, week INT)")
        con.execute("CREATE TABLE games (season INT, week INT, home_team TEXT,"
                    " away_team TEXT, kickoff_utc TEXT)")
        con.execute("INSERT INTO players VALUES ('p1', 'T.J. Watt', 'PIT')")
        con.execute("INSERT INTO weekly_picks VALUES (7, 'p1', 2026, 8)")
        con.execute("INSERT INTO games VALUES (2026, 8, 'PIT', 'BAL',"
                    " '2026-10-29T00:15:00+00:00')")

    def test_locks_once(self):
        con = _con()
        self._setup(con)
        self.assertEqual(audit.record_locks(con), 1)
        self.assertEqual(audit.record_locks(con), 0)
        rows = con.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        self.assertEqual(rows, 1)
        con.close()

    def test_future_kickoff_not_locked(self):
        con = _con()
        self._setup(con)
        con.execute("UPDATE games SET kickoff_utc = '2999-01-01T00:00:00+00:00'")
        self.assertEqual(audit.record_locks(con), 0)
        con.close()


class TestTripwire(unittest.TestCase):
    def _setup(self, con):
        con.execute("CREATE TABLE scoring_events (player_id TEXT, season INT,"
                    " week INT, stat_key TEXT, new_total REAL, created_at TEXT)")
        con.execute("CREATE TABLE stat_corrections (player_id TEXT, season INT,"
                    " week INT, stat_key TEXT, new_total REAL, detected_at TEXT)")
        con.execute("CREATE TABLE weekly_subs (member_id INT, in_player_id TEXT,"
                    " season INT, week INT, subbed_at TEXT)")

    def test_orphans_and_pre_era_exclusion(self):
        con = _con()
        self._setup(con)
        # Era starts at the first audit row.
        audit.log(con, 2026, 8, "stat_observed", "poll", player_id="p9",
                  stat_key="tackles", old_value=0, new_value=5,
                  source_feed="tank01")
        con.commit()
        # Orphan: score write after era start, no audit row.
        # (Far-future stamps: the era timestamp is "now".)
        con.execute("INSERT INTO scoring_events VALUES ('p1', 2026, 8,"
                    " 'sacks', 1, '2999-01-01T00:00:00+00:00')")
        # Pre-era write: excluded.
        con.execute("INSERT INTO scoring_events VALUES ('p2', 2026, 8,"
                    " 'sacks', 1, '2020-01-01T00:00:00+00:00')")
        # Covered write: matching audit row (same new_value) exists.
        con.execute("INSERT INTO scoring_events VALUES ('p9', 2026, 8,"
                    " 'tackles', 5, '2999-01-01T00:00:00+00:00')")
        # Orphan sub.
        con.execute("INSERT INTO weekly_subs VALUES (7, 'p3', 2026, 8,"
                    " '2999-01-01T00:00:00+00:00')")
        con.commit()
        started, orphans = audit.tripwire_orphans(con)
        self.assertTrue(started)
        sources = {(o["source"], o["player_id"]) for o in orphans}
        self.assertIn(("scoring_events", "p1"), sources)
        self.assertIn(("weekly_subs", "p3"), sources)
        self.assertNotIn(("scoring_events", "p2"), sources)
        self.assertNotIn(("scoring_events", "p9"), sources)
        con.close()

    def test_no_audit_rows_means_unarmed(self):
        con = _con()
        self._setup(con)
        started, orphans = audit.tripwire_orphans(con)
        self.assertFalse(started)
        self.assertEqual(orphans, [])
        con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
