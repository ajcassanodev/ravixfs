"""Unit tests for the prod pick-link gate (main branch, app/main.py).

Run with: /tmp/gate-env/bin/python tests/test_prod_gate.py
(needs fastapi + httpx + jinja2 + python-multipart in that venv)
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

DB_PATH = os.path.join(tempfile.mkdtemp(), "prod_gate_test.db")
os.environ["RAVIXFS_DB"] = DB_PATH
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as _db  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TOKEN = "test-token-1"


def _seed():
    _db.init_db()
    con = _db.connect()
    con.execute(
        "INSERT INTO members (name, pick_token, is_test) VALUES (?,?,?)",
        ("Test Member", TOKEN, 0))
    con.commit()
    con.close()


_seed()
client = TestClient(app, follow_redirects=False)
local_client = TestClient(app, base_url="http://localhost",
                          follow_redirects=False)


class TestProdGate(unittest.TestCase):
    def test_anonymous_redirected_to_locked(self):
        for path in ("/", "/draft", "/trades", "/guide", "/week/2026/1"):
            r = client.get(path)
            self.assertEqual(r.status_code, 303, path)
            self.assertIn("/locked?next=", r.headers["location"])

    def test_locked_page_renders(self):
        r = client.get("/locked")
        self.assertEqual(r.status_code, 200)
        self.assertIn("pick link", r.text.lower())

    def test_bad_token_rejected(self):
        r = client.post("/locked", data={"pick_link": "nope", "next": "/"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("didn't match", r.text)

    def test_good_token_unlocks_and_cookie_opens_pages(self):
        r = client.post("/locked", data={
            "pick_link": f"https://prod-prime5f.ravixfs.com/submit/{TOKEN}",
            "next": "/"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("prime5_pick=", r.headers.get("set-cookie", ""))
        cookie = r.cookies.get("prime5_pick")
        self.assertEqual(cookie, TOKEN)
        r2 = client.get("/", cookies={"prime5_pick": TOKEN})
        self.assertEqual(r2.status_code, 200)

    def test_token_param_unlocks(self):
        r = client.get(f"/?token={TOKEN}")
        self.assertEqual(r.status_code, 303)
        # Starlette absolutizes "/" to the test host; the path is what matters.
        self.assertTrue(r.headers["location"].endswith("/"))
        self.assertIn("prime5_pick=", r.headers.get("set-cookie", ""))

    def test_submit_front_door(self):
        r = client.get(f"/submit/{TOKEN}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("prime5_pick=", r.headers.get("set-cookie", ""))
        r_bad = client.get("/submit/nope")
        self.assertEqual(r_bad.status_code, 404)

    def test_admin_and_static_pass_through(self):
        r = client.get("/admin/badtoken")
        self.assertEqual(r.status_code, 404)  # handler guard, not the gate
        r2 = client.get("/static/style.css")
        self.assertEqual(r2.status_code, 200)

    def test_localhost_bypasses(self):
        r = local_client.get("/")
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
