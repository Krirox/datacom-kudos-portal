"""Rules from SPECIFICATION.md. Service tests use a temporary database."""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kudos import DEMO_PASSWORD, KudosError, KudosService
from server import serve


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
        self.service = KudosService(
            Path(self._tmp.name) / "kudos.db",
            clock=lambda: self.now,
            iterations=1000,
            hourly_limit=100,
            seed=False,
        )
        self.alex = self.service.create_user("Alex Chen", "alex@example.com", "secret")
        self.sam = self.service.create_user("Sam Patel", "sam@example.com", "secret")
        self.admin = self.service.create_user(
            "Riley Morgan", "riley@example.com", "secret", role="admin"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_password_failure_does_not_reveal_which_part_was_wrong(self):
        with self.assertRaises(KudosError) as unknown:
            self.service.authenticate("missing@example.com", "secret")
        with self.assertRaises(KudosError) as wrong:
            self.service.authenticate("alex@example.com", "nope")
        self.assertEqual(unknown.exception.status, 401)
        self.assertEqual(unknown.exception.message, wrong.exception.message)

    def test_colleague_list_excludes_the_caller(self):
        names = [person["name"] for person in self.service.list_colleagues(self.alex["id"])]
        self.assertEqual(names, ["Riley Morgan", "Sam Patel"])

    def test_self_kudos_blank_message_and_overlong_message_are_rejected(self):
        with self.assertRaises(KudosError) as self_kudos:
            self.service.give_kudos(self.alex["id"], self.alex["id"], "Thanks")
        self.assertEqual(self_kudos.exception.status, 400)
        with self.assertRaises(KudosError):
            self.service.give_kudos(self.alex["id"], self.sam["id"], "   ")
        with self.assertRaises(KudosError):
            self.service.give_kudos(self.alex["id"], self.sam["id"], "a" * 501)
        with self.assertRaises(KudosError):
            self.service.give_kudos(self.alex["id"], 999, "Thanks")

    def test_new_kudos_is_visible_by_default_and_script_text_is_stored(self):
        created = self.service.give_kudos(
            self.alex["id"], self.sam["id"], "  Nice work <script>alert(1)</script>  "
        )
        self.assertTrue(created["is_visible"])
        self.assertEqual(created["message"], "Nice work <script>alert(1)</script>")
        self.assertIsNone(created["moderated_by"])
        feed = self.service.public_feed(1, 10)
        self.assertEqual(feed["total"], 1)
        self.assertEqual(feed["items"][0]["message"], created["message"])

    def test_duplicate_is_rejected_until_the_window_passes(self):
        self.service.give_kudos(self.alex["id"], self.sam["id"], "Thanks")
        with self.assertRaises(KudosError) as duplicate:
            self.service.give_kudos(self.alex["id"], self.sam["id"], "thanks")
        self.assertEqual(duplicate.exception.status, 409)
        self.service.give_kudos(self.alex["id"], self.sam["id"], "A different note")
        self.now += timedelta(hours=25)
        again = self.service.give_kudos(self.alex["id"], self.sam["id"], "Thanks")
        self.assertTrue(again["is_visible"])

    def test_hourly_limit_counts_hidden_kudos(self):
        self.service.hourly_limit = 3
        first = self.service.give_kudos(self.alex["id"], self.sam["id"], "One")
        self.service.hide_kudos(self.admin["id"], first["id"], "Spam")
        self.service.give_kudos(self.alex["id"], self.sam["id"], "Two")
        self.service.give_kudos(self.alex["id"], self.sam["id"], "Three")
        with self.assertRaises(KudosError) as limited:
            self.service.give_kudos(self.alex["id"], self.sam["id"], "Four")
        self.assertEqual(limited.exception.status, 429)

    def test_public_feed_hides_moderated_rows_and_pages(self):
        for index in range(12):
            self.service.give_kudos(self.alex["id"], self.sam["id"], f"Note {index}")
            self.now += timedelta(minutes=1)
        hidden = self.service.public_feed(1, 10)["items"][0]
        self.service.hide_kudos(self.admin["id"], hidden["id"], "Off topic")
        first = self.service.public_feed(1, 10)
        second = self.service.public_feed(2, 10)
        self.assertEqual(first["total"], 11)
        self.assertEqual(len(first["items"]), 10)
        self.assertEqual(len(second["items"]), 1)
        self.assertTrue(all(item["is_visible"] for item in first["items"]))
        self.assertNotIn(hidden["id"], [item["id"] for item in first["items"] + second["items"]])

    def test_employee_cannot_moderate(self):
        created = self.service.give_kudos(self.alex["id"], self.sam["id"], "Hello")
        with self.assertRaises(KudosError) as denied:
            self.service.hide_kudos(self.alex["id"], created["id"], "No")
        self.assertEqual(denied.exception.status, 403)
        self.assertTrue(self.service.public_feed(1, 10)["items"][0]["is_visible"])

    def test_hide_requires_a_reason_and_restore_returns_it_to_the_feed(self):
        created = self.service.give_kudos(self.alex["id"], self.sam["id"], "Hello")
        with self.assertRaises(KudosError):
            self.service.hide_kudos(self.admin["id"], created["id"], "  ")
        hidden = self.service.hide_kudos(self.admin["id"], created["id"], "Inappropriate")
        self.assertFalse(hidden["is_visible"])
        self.assertEqual(hidden["moderated_by"], self.admin["id"])
        self.assertEqual(hidden["reason_for_moderation"], "Inappropriate")
        self.assertEqual(self.service.public_feed(1, 10)["total"], 0)
        admin_items = self.service.admin_feed(self.admin["id"], 1, 10)["items"]
        self.assertEqual(admin_items[0]["id"], created["id"])
        restored = self.service.restore_kudos(self.admin["id"], created["id"])
        self.assertTrue(restored["is_visible"])
        self.assertIsNone(restored["reason_for_moderation"])
        with self.assertRaises(KudosError):
            self.service.restore_kudos(self.admin["id"], created["id"])

    def test_delete_removes_the_row_and_keeps_an_audit_snapshot(self):
        created = self.service.give_kudos(self.alex["id"], self.sam["id"], "Please remove")
        self.service.delete_kudos(self.admin["id"], created["id"])
        self.assertEqual(self.service.public_feed(1, 10)["total"], 0)
        with self.assertRaises(KudosError) as missing:
            self.service.delete_kudos(self.admin["id"], created["id"])
        self.assertEqual(missing.exception.status, 404)
        events = self.service.moderation_events()
        self.assertEqual(events[-1]["action"], "delete")
        self.assertEqual(events[-1]["message_snapshot"], "Please remove")
        self.assertEqual(events[-1]["actor_id"], self.admin["id"])

    def test_seeded_database_has_an_admin_and_visible_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            seeded = KudosService(
                Path(tmp) / "kudos.db",
                iterations=1000,
                seed=True,
            )
            admin = seeded.authenticate("riley.morgan@datacom.example", DEMO_PASSWORD)
            self.assertEqual(admin["role"], "admin")
            feed = seeded.public_feed(1, 10)
            self.assertEqual(feed["total"], 2)
            self.assertTrue(all(item["is_visible"] for item in feed["items"]))


class HttpTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        service = KudosService(
            Path(self._tmp.name) / "kudos.db",
            iterations=1000,
            seed=True,
        )
        self.server = serve(service, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"
        self.cookies = urllib.request.HTTPCookieProcessor()
        self.opener = urllib.request.build_opener(self.cookies)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    def test_sign_in_post_feed_hide_and_delete(self):
        page = self.request("GET", "/")
        self.assertIn(b"Give kudos", page)
        self.assertIn(b"width=device-width", page)
        styles = self.request("GET", "/static/styles.css")
        self.assertIn(b"@media", styles)
        script = self.request("GET", "/static/app.js")
        self.assertIn(b"textContent", script)
        self.assertNotIn(b"innerHTML", script)

        denied = self.request("GET", "/api/kudos", expected=401)
        self.assertEqual(denied["error"], "Sign in required")

        alex = self.request(
            "POST",
            "/api/login",
            {"email": "alex.chen@datacom.example", "password": DEMO_PASSWORD},
        )
        self.assertEqual(alex["role"], "employee")
        colleagues = self.request("GET", "/api/users")
        self.assertTrue(all(person["email"] != alex["email"] for person in colleagues))
        sam = next(person for person in colleagues if person["name"] == "Sam Patel")
        created = self.request(
            "POST",
            "/api/kudos",
            {"recipient_id": sam["id"], "message": "Sharp review on the spec."},
            expected=201,
        )
        self.assertTrue(created["is_visible"])
        feed = self.request("GET", "/api/kudos")
        self.assertEqual(feed["items"][0]["id"], created["id"])
        forbidden = self.request(
            "POST",
            f"/api/kudos/{created['id']}/hide",
            {"reason": "Not my job"},
            expected=403,
        )
        self.assertIn("permission", forbidden["error"])

        self.request("POST", "/api/logout", {})
        self.request(
            "POST",
            "/api/login",
            {"email": "riley.morgan@datacom.example", "password": DEMO_PASSWORD},
        )
        hidden = self.request(
            "POST",
            f"/api/kudos/{created['id']}/hide",
            {"reason": "Posted in the wrong channel"},
        )
        self.assertFalse(hidden["is_visible"])
        public = self.request("GET", "/api/kudos")
        self.assertNotIn(created["id"], [item["id"] for item in public["items"]])
        self.request("DELETE", f"/api/kudos/{created['id']}")
        self.request("DELETE", f"/api/kudos/{created['id']}", expected=404)

    def request(self, method, path, body=None, expected=200):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
            exc.close()
        self.assertEqual(status, expected, raw)
        if path == "/" or path.startswith("/static/"):
            return raw
        return json.loads(raw.decode("utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
