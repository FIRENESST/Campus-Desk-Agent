"""Local grants for verified website accounts and revocable administrator sessions."""

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from uuid import UUID


TOKEN = re.compile(r"oa_[A-Za-z0-9_-]{43}\Z")
SESSION_SECONDS = 4 * 60 * 60
IDLE_SECONDS = 15 * 60
SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_grants (
    user_id TEXT PRIMARY KEY, email TEXT NOT NULL, granted_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS email_admin_sessions (
    hash BLOB PRIMARY KEY, user_id TEXT NOT NULL REFERENCES admin_grants(user_id) ON DELETE CASCADE,
    origin TEXT NOT NULL, site_session TEXT NOT NULL, expires_at INTEGER NOT NULL, last_seen INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS email_admin_sessions_user ON email_admin_sessions(user_id);
CREATE INDEX IF NOT EXISTS email_admin_sessions_expiry ON email_admin_sessions(expires_at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, time INTEGER NOT NULL, actor TEXT NOT NULL,
    action TEXT NOT NULL, target TEXT NOT NULL, outcome TEXT NOT NULL
);
"""


def admin_user_id(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("请输入有效的用户 UUID") from None


def session_hash(token):
    return hashlib.sha256(token.encode("ascii")).digest() if TOKEN.fullmatch(token) else None


def csrf_token(token):
    return hmac.digest(token.encode("ascii"), b"online-admin-csrf-v1", "sha256").hex()


class OnlineAdminStore:
    def __init__(self, directory, *, clock=time.time):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "web-admin.sqlite3"
        self.clock = clock
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _transaction(self):
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def _event(self, conn, actor, action, target, outcome):
        return conn.execute(
            "INSERT INTO events(time,actor,action,target,outcome) VALUES (?,?,?,?,?)",
            (int(self.clock()), actor, action, target, outcome),
        ).lastrowid

    def grant(self, actor, user_id, email):
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO admin_grants VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET email=excluded.email",
                (user_id, email, int(self.clock())),
            )
            self._event(conn, "local:" + actor, "admin.grant", user_id, "completed")

    def revoke(self, actor, user_id):
        with self._transaction() as conn:
            deleted = conn.execute("DELETE FROM admin_grants WHERE user_id=?", (user_id,)).rowcount
            self._event(conn, "local:" + actor, "admin.revoke", user_id, "completed" if deleted else "not_granted")
            return bool(deleted)

    def accounts(self):
        with closing(self._connect()) as conn:
            return conn.execute("SELECT user_id,email,granted_at FROM admin_grants ORDER BY email,user_id").fetchall()

    def issue(self, user_id, origin, site_session, previous):
        now = int(self.clock())
        with self._transaction() as conn:
            if not conn.execute("SELECT 1 FROM admin_grants WHERE user_id=?", (user_id,)).fetchone():
                self._event(conn, user_id, "login", "", "denied")
                return None
            conn.execute("DELETE FROM email_admin_sessions WHERE expires_at<=? OR last_seen<=?",
                         (now, now - IDLE_SECONDS))
            conn.execute("DELETE FROM email_admin_sessions WHERE hash=? AND origin=?", (session_hash(previous), origin))
            token = "oa_" + secrets.token_urlsafe(32)
            conn.execute("INSERT INTO email_admin_sessions VALUES (?,?,?,?,?,?)",
                         (session_hash(token), user_id, origin, site_session, now + SESSION_SECONDS, now))
            self._event(conn, user_id, "login", "", "completed")
            return token

    def authenticate(self, token, origin, site_session, *, touch=True):
        hashed = session_hash(token)
        if not hashed:
            return None
        now = int(self.clock())
        if not touch:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    """SELECT user_id FROM email_admin_sessions WHERE hash=? AND origin=? AND site_session=?
                    AND expires_at>? AND last_seen>?""", (hashed, origin, site_session, now, now - IDLE_SECONDS),
                ).fetchone()
                return row["user_id"] if row else None
        with self._transaction() as conn:
            row = conn.execute(
                """UPDATE email_admin_sessions SET last_seen=? WHERE hash=? AND origin=? AND site_session=?
                AND expires_at>? AND last_seen>? RETURNING user_id""",
                (now, hashed, origin, site_session, now, now - IDLE_SECONDS),
            ).fetchone()
            return row["user_id"] if row else None

    def logout(self, token, origin, site_session):
        with self._transaction() as conn:
            row = conn.execute("DELETE FROM email_admin_sessions WHERE hash=? AND origin=? AND site_session=? RETURNING user_id",
                               (session_hash(token), origin, site_session)).fetchone()
            if row:
                self._event(conn, row["user_id"], "logout", "", "completed")

    def event(self, actor, action, target, outcome):
        with self._transaction() as conn:
            return self._event(conn, actor, action, target, outcome)

    def finish_event(self, event_id, outcome):
        with self._transaction() as conn:
            conn.execute("UPDATE events SET outcome=? WHERE id=?", (outcome, event_id))

    def audit(self):
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT id,time,actor,action,target,outcome FROM events ORDER BY id DESC LIMIT 100"
            )]
