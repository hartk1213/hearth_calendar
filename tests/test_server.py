"""Smoke tests: start the real server with JSON-file storage and exercise the API.

Run with:  python -m unittest discover -s tests -v
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HearthServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = free_port()
        env = dict(os.environ,
                   HEARTH_PORT=str(cls.port),
                   HEARTH_DATA=os.path.join(cls.tmp.name, "data.json"),
                   HEARTH_SETTINGS=os.path.join(cls.tmp.name, "settings.json"),
                   DB_HOST="")
        cls.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(50):
            try:
                cls.call("GET", "/api/health")
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        cls.proc.stdout.close()
        cls.tmp.cleanup()

    @classmethod
    def call(cls, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_01_health_and_defaults(self):
        status, body = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, cfg = self.call("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertFalse(cfg["setup_done"])
        self.assertEqual(cfg["members"], [])
        self.assertNotIn("anthropic_api_key", cfg)
        self.assertNotIn("spoonacular_api_key", cfg)
        self.assertNotIn("pin_hash", cfg)

    def test_02_members_and_validation(self):
        status, body = self.call("PUT", "/api/settings", {
            "members": [{"name": "Ann", "color": "#e07a5f"}, {"name": "Ben", "color": "#3d85c6"}],
            "setup_done": True})
        self.assertEqual(status, 200)
        self.assertEqual([m["name"] for m in body["config"]["members"]], ["Ann", "Ben"])
        status, body = self.call("PUT", "/api/settings", {"members": [{"name": "A"}, {"name": "a"}]})
        self.assertEqual(status, 400)
        status, body = self.call("PUT", "/api/settings", {"members": [{"name": "Everyone"}]})
        self.assertEqual(status, 400)

    def test_03_state_revisions(self):
        status, state = self.call("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIn("pantry", state)
        rev = state["rev"]
        state["lists"][0]["items"].append({"id": "x1", "text": "Milk", "done": False})
        status, saved = self.call("PUT", "/api/state", state)
        self.assertEqual(status, 200)
        self.assertEqual(saved["rev"], rev + 1)
        # A second screen saving from the old revision is told to re-apply on top.
        stale = dict(state, rev=rev)
        status, current = self.call("PUT", "/api/state", stale)
        self.assertEqual(status, 409)
        self.assertEqual(current["rev"], rev + 1)

    def test_04_bad_calendar_link(self):
        status, body = self.call("POST", "/api/calendars", {"member": "Ann", "url": "not a link"})
        self.assertEqual(status, 400)
        self.assertIn("calendar link", body["error"])

    def test_05_events_without_calendars(self):
        status, events = self.call("GET", "/api/events?start=2026-01-01&end=2026-01-31")
        self.assertEqual(status, 200)
        self.assertEqual(events, [])
        status, _ = self.call("GET", "/api/events?start=2026-01-01&end=2027-01-01")
        self.assertEqual(status, 400)

    def test_06_pin_lock(self):
        status, _ = self.call("PUT", "/api/settings", {"pin": "4321"})
        self.assertEqual(status, 200)
        status, body = self.call("PUT", "/api/settings", {"title": "Locked out"})
        self.assertEqual(status, 403)
        status, body = self.call("PUT", "/api/settings", {"title": "Wrong"}, {"X-Hearth-Pin": "0000"})
        self.assertEqual(status, 403)
        status, body = self.call("PUT", "/api/settings", {"title": "The Testers", "pin": ""}, {"X-Hearth-Pin": "4321"})
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["title"], "The Testers")
        self.assertFalse(body["config"]["pin"])

    def test_07_optional_features_off(self):
        status, body = self.call("POST", "/api/recipes", {"meal": "dinner"})
        self.assertEqual(status, 400)
        self.assertIn("Spoonacular", body["error"])
        status, body = self.call("POST", "/api/meal-ideas", {"meal": "dinner"})
        self.assertEqual(status, 400)
        self.assertIn("Claude", body["error"])

    def test_09_pin_check_and_day_view(self):
        status, _ = self.call("POST", "/api/pin-check", {})
        self.assertEqual(status, 200)  # no PIN set
        status, body = self.call("PUT", "/api/settings", {"default_view": "day"})
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["default_view"], "day")
        status, _ = self.call("PUT", "/api/settings", {"default_view": "year"})
        self.assertEqual(status, 400)
        self.call("PUT", "/api/settings", {"pin": "2468"})
        status, _ = self.call("POST", "/api/pin-check", {}, {"X-Hearth-Pin": "1111"})
        self.assertEqual(status, 403)
        status, _ = self.call("POST", "/api/pin-check", {}, {"X-Hearth-Pin": "2468"})
        self.assertEqual(status, 200)
        self.call("PUT", "/api/settings", {"pin": ""}, {"X-Hearth-Pin": "2468"})

    def test_10_import_needs_claude(self):
        status, body = self.call("POST", "/api/import-events", {"text": "Picture day is Dec 3"})
        self.assertEqual(status, 400)
        self.assertIn("Claude", body["error"])

    def test_11_state_has_rewards(self):
        status, state = self.call("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["rewards"], [])
        self.assertEqual(state["star_log"], [])

    def test_12_sign_in_away_from_home(self):
        away = {"X-Real-IP": "8.8.8.8"}            # what Nginx Proxy Manager sends for an outside visitor
        status, _ = self.call("PUT", "/api/settings", {"auth_mode": "away"})
        self.assertEqual(status, 400)              # needs a password first
        status, body = self.call("PUT", "/api/settings", {"auth_password": "letmein1", "auth_mode": "away"})
        self.assertEqual(status, 200)
        self.assertTrue(body["config"]["at_home"])
        # At home: no sign-in needed.
        self.assertEqual(self.call("GET", "/api/state")[0], 200)
        # Away: API refuses, and the page is the sign-in page.
        self.assertEqual(self.call("GET", "/api/state", headers=away)[0], 401)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/", headers=away)
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertIn(b"Family password", resp.read())
        # A proxy that forwards without saying who the visitor is counts as away.
        self.assertEqual(self.call("GET", "/api/state", headers={"X-Forwarded-Proto": "https"})[0], 401)
        # Faking a home address in X-Forwarded-For doesn't work: only the entry our proxy added counts.
        self.assertEqual(self.call("GET", "/api/state", headers={"X-Forwarded-For": "192.168.1.5, 8.8.8.8"})[0], 401)
        # Wrong password, then the right one.
        status, body = self.call("POST", "/api/login", {"password": "nope"}, away)
        self.assertEqual(status, 401)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/login", method="POST",
                                     data=json.dumps({"password": "letmein1"}).encode(),
                                     headers={"Content-Type": "application/json", "X-Forwarded-Proto": "https", **away})
        with urllib.request.urlopen(req, timeout=10) as resp:
            cookie = resp.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        token = cookie.split(";")[0]
        signed_in = {**away, "Cookie": token}
        status, cfg = self.call("GET", "/api/config", headers=signed_in)
        self.assertEqual(status, 200)
        self.assertTrue(cfg["signed_in"])
        self.assertFalse(cfg["at_home"])
        self.assertEqual(len(cfg["sessions"]), 1)
        self.assertNotIn("hash", cfg["sessions"][0])
        # Sign out: the cookie stops working.
        self.assertEqual(self.call("POST", "/api/logout", {}, signed_in)[0], 200)
        self.assertEqual(self.call("GET", "/api/state", headers=signed_in)[0], 401)

    def test_13_sign_in_rate_limit(self):
        attacker = {"X-Real-IP": "203.0.113.9"}
        codes = [self.call("POST", "/api/login", {"password": f"guess{i}"}, attacker)[0] for i in range(9)]
        self.assertEqual(codes[:8], [401] * 8)
        self.assertEqual(codes[8], 429)
        # The right password is refused too while locked out.
        self.assertEqual(self.call("POST", "/api/login", {"password": "letmein1"}, attacker)[0], 429)

    def test_14_sign_in_always_and_off(self):
        status, _ = self.call("PUT", "/api/settings", {"auth_mode": "always"})
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", "/api/state")[0], 401)      # even at home now
        self.assertEqual(self.call("GET", "/api/health")[0], 200)     # the Pi's start-up check still works
        # Turning it off needs a signed-in device now.
        status, _ = self.call("PUT", "/api/settings", {"auth_mode": "off"})
        self.assertEqual(status, 401)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/login", method="POST",
                                     data=json.dumps({"password": "letmein1"}).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            token = resp.headers["Set-Cookie"].split(";")[0]
        status, body = self.call("PUT", "/api/settings", {"auth_mode": "off"}, {"Cookie": token})
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", "/api/state")[0], 200)

    def test_08_serves_app(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/?kiosk=1", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Cache-Control"), "no-cache")
            self.assertIn(b"<title>Hearth</title>", resp.read())


if __name__ == "__main__":
    unittest.main()
