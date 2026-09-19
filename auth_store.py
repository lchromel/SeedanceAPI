"""Single-workspace authentication. Passwords and session bearers are never stored.

This is deliberately one account: existing media/API resources are workspace-wide.
Do not enable multi-tenant registration without adding resource ownership checks.
"""
import base64
import contextlib
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time

SESSION_TTL = 12 * 3600
IDLE_TTL = 30 * 60
HASH_SLOTS = threading.BoundedSemaphore(2)


def password_hash(password):
    if not isinstance(password, str) or not 15 <= len(password) <= 1024:
        raise ValueError("Пароль должен содержать от 15 до 1024 символов.")
    salt = secrets.token_bytes(16)
    with HASH_SLOTS:
        digest = hashlib.scrypt(password.encode(), salt=salt, n=131072, r=8, p=1,
                                maxmem=256 * 1024 * 1024, dklen=32)
    return "scrypt$131072$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def password_matches(password, encoded):
    if not isinstance(password, str) or len(password) > 1024:
        return False
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if (algorithm, n, r, p) != ("scrypt", "131072", "8", "1"):
            return False
        salt, expected = base64.b64decode(salt, validate=True), base64.b64decode(expected, validate=True)
        if len(salt) != 16 or len(expected) != 32:
            return False
        with HASH_SLOTS:
            actual = hashlib.scrypt(password.encode(), salt=salt, n=131072, r=8, p=1,
                                    maxmem=256 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class RateLimited(Exception):
    pass


class AuthStore:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL,
                    password_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, created REAL NOT NULL, seen REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    key TEXT PRIMARY KEY, started REAL NOT NULL, count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO secrets VALUES ('signing', ?)", (secrets.token_hex(32),))

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def configured(self):
        with self.db() as db:
            return db.execute("SELECT 1 FROM account").fetchone() is not None

    def set_password(self, username, password):
        if not isinstance(username, str) or not username.strip() or len(username) > 254:
            raise ValueError("Укажите логин длиной до 254 символов.")
        username = username.strip().casefold()
        encoded = password_hash(password)
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO account VALUES (1, ?, ?)", (username, encoded))
            db.execute("DELETE FROM sessions")
            db.execute("UPDATE secrets SET value=? WHERE name='signing'", (secrets.token_hex(32),))

    def signing_key(self):
        if not self.configured():
            raise PermissionError("Authentication is not configured")
        with self.db() as db:
            return bytes.fromhex(db.execute("SELECT value FROM secrets WHERE name='signing'").fetchone()[0])

    def login(self, username, password, client_ip):
        now = time.time()
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 254 or len(password) > 1024:
            return None
        username = username.strip().casefold()
        # Consume slots before the expensive hash; concurrent requests cannot bypass limits.
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE started < ?", (now - 900,))
            keys = [("ip:" + client_ip, 20), ("account:" + username, 8)]
            for key, limit in keys:
                row = db.execute("SELECT count FROM attempts WHERE key=?", (key,)).fetchone()
                if row and row[0] >= limit:
                    raise RateLimited()
            for key, _ in keys:
                db.execute("INSERT INTO attempts VALUES (?, ?, 1) ON CONFLICT(key) DO UPDATE SET count=count+1", (key, now))
            account = db.execute("SELECT username,password_hash FROM account WHERE id=1").fetchone()
        if not account:
            return None
        # Always verify the hash, including for an unknown login (no cheap user enumeration).
        matches = password_matches(password, account[1])
        if not matches or not hmac.compare_digest(username.encode(), account[0].encode()):
            return None
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT password_hash FROM account WHERE id=1").fetchone()[0] != account[1]:
                return None  # Password changed while this request was verifying.
            db.execute("DELETE FROM sessions WHERE created < ? OR seen < ?", (now-SESSION_TTL, now-IDLE_TTL))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (token_hash(token), now, now))
            db.execute("DELETE FROM attempts WHERE key=?", ("account:" + username,))
        return token

    def session(self, token):
        if not isinstance(token, str) or len(token) != 43:
            return None
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT created,seen FROM sessions WHERE token_hash=?", (token_hash(token),)).fetchone()
            if not row:
                return None
            if row[0] + SESSION_TTL <= now or row[1] + IDLE_TTL <= now:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))
                return None
            db.execute("UPDATE sessions SET seen=? WHERE token_hash=?", (now, token_hash(token)))
            return db.execute("SELECT username FROM account WHERE id=1").fetchone()[0]

    def logout(self, token):
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))

    def csrf(self, token):
        return hmac.new(self.signing_key(), ("csrf:" + token).encode(), hashlib.sha256).hexdigest()


def default_auth_path():
    root = os.environ.get("SEEDANCE_DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    return os.path.join(root, ".auth", "auth.sqlite3")


if __name__ == "__main__":
    import argparse
    import getpass
    parser = argparse.ArgumentParser(description="Create/replace the single workspace account; revokes all sessions and signed upload links.")
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    password = getpass.getpass("Новый пароль (не менее 15 символов): ")
    if password != getpass.getpass("Повторите пароль: "):
        parser.error("Пароли не совпадают")
    AuthStore(default_auth_path()).set_password(args.username, password)
    print("Пароль сохранён как scrypt-хеш. Предыдущие сессии и ссылки отозваны.")
