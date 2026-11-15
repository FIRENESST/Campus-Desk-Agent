"""SQLite storage for local CAS credentials, login flows and one-use tickets."""

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager


SESSION_SECONDS = 8 * 60 * 60
FLOW_SECONDS = 300
TICKET_SECONDS = 60
RATE_WINDOW = 300
STATE_SUBDIR = "cas"
ADMIN_USERNAME = "admin"
ADMIN_SESSION_SECONDS = 3600
ADMIN_SESSION_PREFIX = "CA-"
USERNAME = re.compile(r"[a-z0-9][a-z0-9_.-]{1,63}\Z")
CAMPUS_NUMBER = re.compile(r"(?=[a-z0-9_.-]*[0-9])[a-z0-9][a-z0-9_.-]{1,31}\Z")
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    username TEXT PRIMARY KEY,
    subject TEXT NOT NULL UNIQUE,
    salt BLOB NOT NULL,
    password_hash BLOB NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    is_campus_number INTEGER NOT NULL DEFAULT 0 CHECK (is_campus_number IN (0,1))
);
CREATE TABLE IF NOT EXISTS sessions (
    hash BLOB PRIMARY KEY,
    subject TEXT NOT NULL REFERENCES accounts(subject),
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_subject ON sessions(subject);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS tickets (
    hash BLOB PRIMARY KEY,
    session_hash BLOB NOT NULL REFERENCES sessions(hash) ON DELETE CASCADE,
    service TEXT NOT NULL,
    fresh INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS tickets_session ON tickets(session_hash);
CREATE INDEX IF NOT EXISTS tickets_expiry ON tickets(expires_at);
CREATE TABLE IF NOT EXISTS login_flows (
    hash BLOB PRIMARY KEY,
    service TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS flows_expiry ON login_flows(expires_at);
CREATE TABLE IF NOT EXISTS rate_limits (
    bucket TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS limits_expiry ON rate_limits(expires_at);
CREATE TABLE IF NOT EXISTS administrator (
    id INTEGER PRIMARY KEY CHECK (id=1),
    salt BLOB NOT NULL,
    password_hash BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS administrator_sessions (
    hash BLOB PRIMARY KEY,
    expires_at INTEGER NOT NULL
);
"""


class CASAccountExists(ValueError):
    def __init__(self, username):
        super().__init__("该学工号已被现有 CAS 账号占用")
        self.username = username


class CASAccountNotFound(ValueError):
    pass


def canonical_username(value):
    name = value.strip().lower()
    if not USERNAME.fullmatch(name) or not value.isascii():
        raise ValueError("CAS 账号须为 2 到 64 位英文字母、数字、点、下划线或连字符")
    return name


def canonical_campus_number(value):
    number = value.strip().lower()
    if not value.isascii() or not CAMPUS_NUMBER.fullmatch(number):
        raise ValueError("学工号须为 2 到 32 位且至少包含一个数字，可使用英文字母、点、下划线和连字符")
    return number


def _password_hash(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**15, r=8, p=3, maxmem=64 * 1024**2)


def validate_password(password):
    if (not isinstance(password, str) or not 8 <= len(password) <= 128
        or not re.search(r"[a-z]", password) or not re.search(r"[0-9]", password)
        or re.search(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]", password)):
        raise ValueError("CAS 密码须为 8 到 128 个可输入字符，包含数字和小写英文字母")


def _new_password(password):
    validate_password(password)
    salt = secrets.token_bytes(16)
    return salt, _password_hash(password, salt)


def _token(prefix):
    return prefix + secrets.token_urlsafe(32)


def token_is_valid(value, prefix):
    return re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]{43}", value or "") is not None


def _digest(value):
    return hashlib.sha256(value.encode("ascii")).digest()


class CASStore:
    def __init__(self, directory, *, clock=time.time):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "cas.sqlite3"
        self.clock = clock
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            if "is_campus_number" not in self._account_columns(conn):
                self._migrate_campus_numbers()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _account_columns(conn):
        return {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}

    def _migrate_campus_numbers(self):
        with self._transaction() as conn:
            if "is_campus_number" in self._account_columns(conn):
                return
            conn.execute("ALTER TABLE accounts ADD COLUMN is_campus_number INTEGER NOT NULL DEFAULT 0 CHECK (is_campus_number IN (0,1))")
            conn.execute("UPDATE accounts SET is_campus_number=1 WHERE length(username) BETWEEN 2 AND 32 AND username NOT GLOB '*[^0-9]*'")
            conn.execute("DELETE FROM sessions WHERE subject IN (SELECT subject FROM accounts WHERE is_campus_number=0)")

    def active_campus_number(self, subject):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT username FROM accounts WHERE subject=? AND enabled=1 AND is_campus_number=1",
                               (subject,)).fetchone()
            return row["username"] if row else None

    def identity_accounts(self, subjects):
        if not subjects:
            return {}
        placeholders = ",".join("?" for _ in subjects)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT subject,username,is_campus_number,enabled FROM accounts WHERE subject IN ({placeholders})", subjects,
            )
            return {row["subject"]: row for row in rows}

    @contextmanager
    def _transaction(self):
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def accounts(self, *, search="", limit=-1, offset=0, username=None):
        with closing(self._connect()) as conn:
            return conn.execute(
                """SELECT a.username,a.enabled,a.is_campus_number,COUNT(s.hash) AS active_sessions
                FROM accounts a LEFT JOIN sessions s ON s.subject=a.subject AND s.expires_at>? AND a.enabled=1 AND a.is_campus_number=1
                WHERE (? IS NULL OR a.username=?) AND instr(a.username, ?)>0
                GROUP BY a.username,a.enabled,a.is_campus_number ORDER BY a.username LIMIT ? OFFSET ?""",
                (int(self.clock()), username, username, search, limit, offset),
            ).fetchall()

    def change_administrator_password(self, password):
        salt, hashed = _new_password(password)
        with self._transaction() as conn:
            conn.execute("""INSERT INTO administrator VALUES (1,?,?) ON CONFLICT(id) DO UPDATE
                            SET salt=excluded.salt,password_hash=excluded.password_hash""", (salt, hashed))
            conn.execute("DELETE FROM administrator_sessions")
            conn.execute("DELETE FROM rate_limits WHERE bucket='admin.login'")

    def administrator_login(self, username, password, previous_session):
        now = int(self.clock())
        with self._transaction() as conn:
            conn.execute("DELETE FROM rate_limits WHERE expires_at<=?", (now,))
            retry = self._limit(conn, "admin.login", 10, now)
            account = conn.execute("SELECT salt,password_hash FROM administrator WHERE id=1").fetchone()
        if retry:
            return None, retry
        if account is None:
            return None, 0
        matched = hmac.compare_digest(_password_hash(password, account["salt"]), account["password_hash"])
        if not matched or username != ADMIN_USERNAME:
            return None, 0
        session = _token(ADMIN_SESSION_PREFIX)
        with self._transaction() as conn:
            if not conn.execute("SELECT 1 FROM administrator WHERE id=1 AND password_hash=?",
                                (account["password_hash"],)).fetchone():
                return None, 0
            conn.execute("DELETE FROM administrator_sessions WHERE expires_at<=?", (now,))
            if token_is_valid(previous_session, ADMIN_SESSION_PREFIX):
                conn.execute("DELETE FROM administrator_sessions WHERE hash=?", (_digest(previous_session),))
            conn.execute("INSERT INTO administrator_sessions VALUES (?,?)", (_digest(session), now + ADMIN_SESSION_SECONDS))
            conn.execute("DELETE FROM rate_limits WHERE bucket='admin.login'")
        return session, 0

    def administrator_session_active(self, session):
        if not token_is_valid(session, ADMIN_SESSION_PREFIX):
            return False
        with closing(self._connect()) as conn:
            return conn.execute("SELECT 1 FROM administrator_sessions WHERE hash=? AND expires_at>?",
                                (_digest(session), int(self.clock()))).fetchone() is not None

    def administrator_logout(self, session):
        if token_is_valid(session, ADMIN_SESSION_PREFIX):
            with self._transaction() as conn:
                conn.execute("DELETE FROM administrator_sessions WHERE hash=?", (_digest(session),))

    def add(self, username, password):
        self.add_many(((username, password),))

    def add_many(self, accounts):
        rows = [(canonical_campus_number(name), uuid.uuid4().hex, *_new_password(password))
                for name, password in accounts]
        with self._transaction() as conn:
            for row in rows:
                inserted = conn.execute(
                    """INSERT INTO accounts(username,subject,salt,password_hash,is_campus_number) VALUES (?,?,?,?,1)
                    ON CONFLICT(username) DO NOTHING RETURNING username""",
                    row,
                ).fetchone()
                if not inserted:
                    raise CASAccountExists(row[0])

    def set_campus_number(self, username, campus_number):
        name, number = canonical_username(username), canonical_campus_number(campus_number)
        with self._transaction() as conn:
            row = conn.execute("SELECT subject,is_campus_number FROM accounts WHERE username=?", (name,)).fetchone()
            if row is None:
                raise CASAccountNotFound("CAS 账号不存在")
            if name != number or not row["is_campus_number"]:
                if name != number and conn.execute("SELECT 1 FROM accounts WHERE username=?", (number,)).fetchone():
                    raise CASAccountExists(number)
                conn.execute("UPDATE accounts SET username=?,is_campus_number=1 WHERE subject=?", (number, row["subject"]))
                self._revoke(conn, row)
        return number

    def change_password(self, username, password):
        salt, hashed = _new_password(password)
        with self._transaction() as conn:
            row = conn.execute(
                "UPDATE accounts SET salt=?,password_hash=? WHERE username=? RETURNING subject",
                (salt, hashed, canonical_username(username)),
            ).fetchone()
            self._revoke(conn, row)

    def set_enabled(self, username, enabled):
        name = canonical_username(username)
        with self._transaction() as conn:
            row = conn.execute("SELECT subject,enabled FROM accounts WHERE username=?", (name,)).fetchone()
            if row is None:
                raise CASAccountNotFound("CAS 账号不存在")
            if bool(row["enabled"]) != enabled:
                conn.execute("UPDATE accounts SET enabled=? WHERE username=?", (enabled, name))
                if not enabled:
                    self._revoke(conn, row)

    @staticmethod
    def _revoke(conn, row):
        if row is None:
            raise CASAccountNotFound("CAS 账号不存在")
        conn.execute("DELETE FROM sessions WHERE subject=?", (row["subject"],))

    def _limit(self, conn, bucket, maximum, now):
        attempts, expires = conn.execute(
            """INSERT INTO rate_limits(bucket,attempts,expires_at) VALUES (?,1,?)
            ON CONFLICT(bucket) DO UPDATE SET attempts=attempts+1
            RETURNING attempts,expires_at""",
            (bucket, now + RATE_WINDOW),
        ).fetchone()
        return max(1, expires - now) if attempts > maximum else 0

    def start_login(self, service, renew, session, address):
        now = int(self.clock())
        with self._transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            conn.execute("DELETE FROM tickets WHERE expires_at<=?", (now,))
            conn.execute("DELETE FROM login_flows WHERE expires_at<=?", (now,))
            conn.execute("DELETE FROM rate_limits WHERE expires_at<=?", (now,))
            retry = self._limit(conn, "page:" + address, 120, now)
            if retry:
                return None, None, retry
            if not renew and token_is_valid(session, "TGT-"):
                session_hash = _digest(session)
                if conn.execute("""SELECT 1 FROM sessions s JOIN accounts a ON a.subject=s.subject
                    WHERE s.hash=? AND a.enabled=1 AND a.is_campus_number=1""", (session_hash,)).fetchone():
                    return self._issue(conn, session_hash, service, False, now), None, 0
            flow = _token("LT-")
            conn.execute("INSERT INTO login_flows VALUES (?,?,?)", (_digest(flow), service, now + FLOW_SECONDS))
            return None, flow, 0

    def consume_login(self, flow, username, address):
        now = int(self.clock())
        with self._transaction() as conn:
            request = conn.execute(
                "DELETE FROM login_flows WHERE hash=? AND expires_at>? RETURNING service",
                (_digest(flow), now),
            ).fetchone()
            if not request:
                return None, 0
            conn.execute("DELETE FROM rate_limits WHERE expires_at<=?", (now,))
            retry = self._limit(conn, "login-ip:" + address, 40, now)
            if not retry:
                retry = self._limit(conn, "login-user:" + username, 8, now)
            return request["service"], retry

    def authenticate(self, username, password, service, previous_session):
        with closing(self._connect()) as conn:
            account = conn.execute("SELECT * FROM accounts WHERE username=? AND is_campus_number=1", (username,)).fetchone()
        salt = account["salt"] if account else bytes(16)
        expected = account["password_hash"] if account else bytes(32)
        matched = hmac.compare_digest(_password_hash(password, salt), expected)
        if not (matched and account and account["enabled"]):
            return None

        now = int(self.clock())
        session = _token("TGT-")
        session_hash = _digest(session)
        with self._transaction() as conn:
            # Recheck after hashing in case the number, password or account status changed.
            current = conn.execute(
                "SELECT 1 FROM accounts WHERE subject=? AND username=? AND password_hash=? AND enabled=1 AND is_campus_number=1",
                (account["subject"], username, expected),
            ).fetchone()
            if not current:
                return None
            if token_is_valid(previous_session, "TGT-"):
                conn.execute("DELETE FROM sessions WHERE hash=?", (_digest(previous_session),))
            conn.execute("INSERT INTO sessions VALUES (?,?,?)", (session_hash, account["subject"], now + SESSION_SECONDS))
            return self._issue(conn, session_hash, service, True, now), session

    @staticmethod
    def _issue(conn, session_hash, service, fresh, now):
        ticket = _token("ST-")
        conn.execute("INSERT INTO tickets VALUES (?,?,?,?,?)",
                     (_digest(ticket), session_hash, service, fresh, now + TICKET_SECONDS))
        return ticket

    def validate(self, ticket, service, renew):
        now = int(self.clock())
        with self._transaction() as conn:
            row = conn.execute(
                "DELETE FROM tickets WHERE hash=? RETURNING session_hash,service,fresh,expires_at",
                (_digest(ticket),),
            ).fetchone()
            if not row or row["expires_at"] <= now:
                return None, "INVALID_TICKET"
            if service != row["service"]:
                return None, "INVALID_SERVICE"
            if renew and not row["fresh"]:
                return None, "INVALID_TICKET"
            account = conn.execute(
                """SELECT a.subject,a.username FROM sessions s JOIN accounts a ON a.subject=s.subject
                WHERE s.hash=? AND s.expires_at>? AND a.enabled=1 AND a.is_campus_number=1""",
                (row["session_hash"], now),
            ).fetchone()
            return (account, None) if account else (None, "INVALID_TICKET")

    def logout(self, session):
        if token_is_valid(session, "TGT-"):
            with self._transaction() as conn:
                conn.execute("DELETE FROM sessions WHERE hash=?", (_digest(session),))
