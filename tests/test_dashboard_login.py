"""Unit tests for the dashboard's pairing-key lockout.

Run from the project root: python -m unittest tests.test_dashboard_login
"""

import unittest

from fastapi.testclient import TestClient

from dashboard.server import DashboardServer


class LoginLockoutTests(unittest.TestCase):
    def setUp(self):
        self.server = DashboardServer()
        self.client = TestClient(self.server.app)

    def login(self, pin):
        return self.client.post("/login", json={"pin": pin})

    def test_guessing_keys_locks_the_address_out_even_for_the_right_key(self):
        key = self.server.new_key()
        for _ in range(10):
            self.assertEqual(self.login("WRONG0").status_code, 401)
        self.assertEqual(self.login(key).status_code, 429)
        self.assertIn(key, self.server._pending_keys)

    def test_a_few_typos_do_not_stop_the_right_key(self):
        key = self.server.new_key()
        for _ in range(3):
            self.login("WRONG0")
        response = self.login(key)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_a_qr_link_is_refused_during_a_lockout(self):
        key = self.server.new_key()
        for _ in range(10):
            self.login("WRONG0")
        page = self.client.get(f"/auto-login?key={key}")
        self.assertIn("Link Expired", page.text)
        self.assertIn(key, self.server._pending_keys)


if __name__ == "__main__":
    unittest.main()
