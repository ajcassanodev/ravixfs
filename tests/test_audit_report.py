"""Unit tests for the weekly audit digest (app/audit_report.py).

Run with: python3 tests/test_audit_report.py
"""
import sqlite3
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import audit as _audit  # noqa: E402
from app import audit_report as _ar  # noqa: E402

ET = ZoneInfo("America/New_York")


def _con():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    _audit.ensure_table(con)
    con.execute(
        "CREATE TABLE members (id INTEGER PRIMARY KEY, name TEXT)")
    con.execute(
        "CREATE TABLE players (player_id TEXT PRIMARY KEY, name TEXT)")
    con.execute(
        "CREATE TABLE games (season INTEGER, week INTEGER, kickoff_utc TEXT)")
    con.execute(
        "INSERT INTO games VALUES (2026, 8, '2030-01-01T00:00:00+00:00')")
    return con


def _ev(con, kind, week=8, member_id=None, player_id=None, stat_key=None,
        old=None, new=None, pts=0.0, feed=None, detail="{}"):
    con.execute(
        "INSERT INTO audit_events (season, week, member_id, player_id, kind, "
        "cause, stat_key, old_value, new_value, points_delta, source_feed, "
        "actor, detail, created_at) VALUES (2026, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, '', ?, "
        "'2026-10-30T12:00:00+00:00')",
        (week, member_id, player_id, kind, stat_key, old, new, pts, feed, detail))


class TestWindow(unittest.TestCase):
    def test_tuesday_905_due(self):
        # Oct 30 2026 is a Friday, so Oct 27 is the Tuesday before it.
        self.assertTrue(_ar.tuesday_report_due(
            datetime(2026, 10, 27, 9, 5, tzinfo=ET)))

    def test_tuesday_800_not_due(self):
        self.assertFalse(_ar.tuesday_report_due(
            datetime(2026, 10, 27, 8, 0, tzinfo=ET)))

    def test_monday_905_not_due(self):
        self.assertFalse(_ar.tuesday_report_due(
            datetime(2026, 10, 26, 9, 5, tzinfo=ET)))


class TestBuildReport(unittest.TestCase):
    def test_sections_and_counts(self):
        con = _con()
        con.execute("INSERT INTO members VALUES (1, 'Adam')")
        con.execute("INSERT INTO players VALUES ('p1', 'T.J. Watt')")
        _ev(con, "stat_observed", player_id="p1", stat_key="tackles",
            old=4, new=5, pts=0.1, feed="tank01")
        _ev(con, "stat_observed", player_id="p1", stat_key="tackles",
            old=5, new=6, pts=0.1, feed="tank01")
        _ev(con, "stat_corrected", player_id="p1", stat_key="sacks",
            old=1, new=0, pts=-2.0, feed="nflverse")
        _ev(con, "pick_locked", member_id=1, player_id="p1")
        _ev(con, "pick_subbed_out", member_id=1, player_id="p1",
            detail='{"in_player_id": "p2"}')
        con.execute("INSERT INTO players VALUES ('p2', 'Alex Highsmith')")
        subject, body = _ar.build_report(con, 2026, 8)
        self.assertIn("Week 8", subject)
        self.assertIn("stat_observed: 2", body)
        self.assertIn("pick_locked: 1", body)
        self.assertIn("corrected", body)
        self.assertIn("Sub out", body)
        # High-volume kinds get counts only, not per-row lines.
        self.assertNotIn("tackles 4 → 5", body)


class TestRunDedup(unittest.TestCase):
    def test_send_once_then_already_sent(self):
        con = _con()
        sent = []
        _ar._emailer.send_email = lambda to, s, b: sent.append((to, s)) or True
        try:
            # Tuesday 9:05; current pick week is 8, so closed week is 7.
            now = datetime(2026, 10, 27, 9, 5, tzinfo=ET)
            first = _ar.run_weekly_report(con, 2026, now)
            self.assertIn("week 7", first)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][0], "info@ravixfs.com")
            second = _ar.run_weekly_report(con, 2026, now)
            self.assertIn("already sent", second)
            self.assertEqual(len(sent), 1)
        finally:
            import importlib
            importlib.reload(_ar._emailer)


if __name__ == "__main__":
    unittest.main()
