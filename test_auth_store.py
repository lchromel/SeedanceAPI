import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

import web_app
from auth_store import AuthStore, IDLE_TTL, SESSION_TTL, RateLimited, password_hash, password_matches, token_hash

PASSWORD = "A long test passphrase 123!"


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = AuthStore(self.directory.name + "/private/auth.sqlite3")
        self.store.set_password("USER@example.com", PASSWORD)

    def test_hash_is_salted_and_neither_password_nor_session_bearer_is_stored(self):
        with self.store.db() as db:
            encoded = db.execute("SELECT password_hash FROM account").fetchone()[0]
        self.assertTrue(encoded.startswith("scrypt$131072$8$1$"))
        self.assertTrue(password_matches(PASSWORD, encoded))
        self.assertFalse(password_matches("wrong", encoded))
        self.assertNotEqual(encoded, password_hash(PASSWORD))
        token = self.store.login("user@example.com", PASSWORD, "127.0.0.1")
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT token_hash FROM sessions").fetchone()[0], token_hash(token))
        with open(self.store.path, "rb") as handle:
            data = handle.read()
        self.assertNotIn(PASSWORD.encode(), data)
        self.assertNotIn(token.encode(), data)
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(self.store.path)).st_mode & 0o777, 0o700)

    def test_expiry_revocation_and_password_change(self):
        token = self.store.login("user@example.com", PASSWORD, "127.0.0.1")
        self.assertEqual(self.store.session(token), "user@example.com")
        with mock.patch("auth_store.time.time", return_value=10**12):
            self.assertIsNone(self.store.session(token))
        token = self.store.login("user@example.com", PASSWORD, "127.0.0.1")
        key = self.store.signing_key()
        self.store.set_password("user@example.com", "Another strong test passphrase")
        self.assertIsNone(self.store.session(token))
        self.assertNotEqual(key, self.store.signing_key())
        self.assertIsNone(self.store.login("user@example.com", PASSWORD, "127.0.0.1"))

    def test_idle_and_absolute_expiry_are_enforced_independently(self):
        for created, seen in [(1000, 1000), (1000-SESSION_TTL, 1000+IDLE_TTL)]:
            with self.store.db() as db:
                db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?)", (token_hash("a"*43), created, seen))
            with mock.patch("auth_store.time.time", return_value=1000+IDLE_TTL+1):
                self.assertIsNone(self.store.session("a"*43))

    def test_limit_persists_across_store_instances_and_expires(self):
        with mock.patch("auth_store.password_matches", return_value=False):
            for _ in range(8):
                self.assertIsNone(self.store.login("user@example.com", "wrong", "ip"))
            with self.assertRaises(RateLimited):
                AuthStore(self.store.path).login("user@example.com", "wrong", "another-ip")
            with mock.patch("auth_store.time.time", return_value=10**12):
                self.assertIsNone(self.store.login("user@example.com", "wrong", "ip"))

    def test_bad_password_input_and_corrupt_hash_fail_closed(self):
        for value in (None, 123, "x"*1025):
            self.assertFalse(password_matches(value, "bad"))
        self.assertFalse(password_matches(PASSWORD, "scrypt$999999999$8$1$a$b"))
        with self.assertRaises(ValueError):
            self.store.set_password("user", "short")


class LoginHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.store = AuthStore(cls.directory.name + "/auth/auth.sqlite3")
        cls.store.set_password("user@example.com", PASSWORD)
        cls.store_patch = mock.patch.object(web_app, "auth_store", return_value=cls.store)
        cls.store_patch.start()
        cls.server = web_app.ThreadingHTTPServer(("127.0.0.1", 0), web_app.SeedanceHandler)
        cls.origin = "http://127.0.0.1:" + str(cls.server.server_port)
        cls.env_patch = mock.patch.dict(os.environ, {"APP_ORIGIN": cls.origin})
        cls.env_patch.start()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.env_patch.stop()
        cls.store_patch.stop()
        cls.directory.cleanup()

    def request(self, method, path, data=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        h = {"Content-Type": "application/json", "Origin": self.origin, **(headers or {})}
        client.request(method, path, json.dumps(data) if data is not None else None, h)
        response = client.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        client.close()
        return result

    def login(self):
        status, headers, _ = self.request("POST", "/api/auth/login", {"username":"user@example.com", "password":PASSWORD})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        return headers["Set-Cookie"].split(";",1)[0]

    def test_session_csrf_logout_and_no_basic_bypass(self):
        self.assertEqual(self.request("GET", "/")[0], 303)
        self.assertEqual(self.request("GET", "/login")[0], 200)
        self.assertEqual(self.request("GET", "/api/config", headers={"Authorization":"Basic dXNlcjpwYXNz"})[0], 401)
        cookie = self.login()
        status, _, body = self.request("GET", "/api/auth/me", headers={"Cookie":cookie})
        self.assertEqual(status, 200)
        csrf = json.loads(body)["csrfToken"]
        self.assertEqual(self.request("POST", "/api/auth/logout", headers={"Cookie":cookie})[0], 403)
        self.assertEqual(self.request("POST", "/api/auth/logout", headers={"Cookie":cookie,"X-CSRF-Token":csrf,"Origin":"https://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/auth/logout", headers={"Cookie":cookie,"X-CSRF-Token":csrf})[0], 200)
        self.assertEqual(self.request("GET", "/api/auth/me", headers={"Cookie":cookie})[0], 401)

    def test_login_origin_limits_and_generic_error(self):
        self.assertEqual(self.request("POST", "/api/auth/login", {}, {"Origin":"https://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/auth/login", [1])[0], 400)
        self.assertEqual(self.request("POST", "/api/auth/login", {"password":"x"*9000})[0], 400)
        results = [self.request("POST", "/api/auth/login", {"username":name,"password":"wrong"}) for name in ["user@example.com","unknown"]]
        self.assertEqual(results[0][0], 401)
        self.assertEqual(results[0][2], results[1][2])

    def test_secure_cookie_and_unsafe_origin_configuration(self):
        with mock.patch.dict(os.environ, {"APP_ORIGIN":"https://studio.example"}):
            cookie = web_app.session_cookie("test")
            self.assertTrue(cookie.startswith("__Host-studio_session="))
            self.assertIn("; Secure", cookie)
            self.assertNotIn("Domain=", cookie)
        with mock.patch.dict(os.environ, {"APP_ORIGIN":"http://studio.example"}):
            with self.assertRaises(ValueError):
                web_app.auth_origin()


if __name__ == "__main__":
    unittest.main()
