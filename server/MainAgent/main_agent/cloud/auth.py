import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from uuid import UUID

import psycopg

from .errors import ServiceError, not_found, unauthorized
from .mail import DisabledMailer
from .roles import AcademicTitle, UserRole
from .security import (
    EMAIL_RESEND_SECONDS, EMAIL_TOKEN_PREFIXES, EMAIL_TOKEN_SECONDS, REMEMBER_SECONDS,
    Passwords, digest, email_address, token, token_hash,
)


REGISTRATION_EMAIL_DOMAINS = frozenset({
    "qq.com",
    "163.com", "126.com", "yeah.net",
    "gmail.com",
    "outlook.com", "hotmail.com", "live.com", "msn.com",
})


def user_view(row):
    return {key: row[key] for key in ("id", "email", "display_name", "role", "academic_title", "created_at", "verified_at")}


def audit(connection, user_id, action, outcome, subject=None):
    connection.execute(
        "INSERT INTO audit_events(user_id,action,outcome,subject_hash) VALUES (%s,%s,%s,%s)",
        (user_id, action, outcome, digest(subject) if subject else None),
    )


def set_user_role(dsn: str, email: str, role: UserRole | str, academic_title: str | None = None):
    """Set the account's role and title together using an operator connection."""
    role = UserRole(role)
    try:
        email = email_address(email)
    except ServiceError as exc:
        raise ValueError(exc.message) from None
    if academic_title is not None:
        if role is not UserRole.TEACHER:
            raise ValueError("只有老师账号可以设置职称")
        try:
            academic_title = AcademicTitle(academic_title.strip()).value
        except ValueError:
            raise ValueError(f"职称请选择{'、'.join(AcademicTitle)}之一") from None
    with psycopg.connect(dsn, connect_timeout=10) as connection:
        account = connection.execute(
            "UPDATE public.users SET role=%s,academic_title=%s,updated_at=now() WHERE email=%s RETURNING id",
            (role.value, academic_title, email),
        ).fetchone()
        if account is None:
            raise ValueError("找不到该邮箱对应的账号")
        audit(connection, account[0], "account.role.set", role.value)


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    session_id: UUID
    role: UserRole


def email_cooldown(seconds):
    return ServiceError(429, "email_cooldown", "请等待冷却结束后再发邮件", retry_after=seconds)


class AuthService:
    def __init__(self, database, settings, mailer):
        self.db, self.settings, self.mailer = database, settings, mailer
        self.passwords = Passwords()

    async def rate_limit(self, scope, subject, maximum, seconds, *, cooldown=False):
        def update(connection):
            return connection.execute(
                """INSERT INTO auth_rate_limits(key_hash) VALUES (%s)
                ON CONFLICT(key_hash) DO UPDATE SET
                  hits = CASE WHEN auth_rate_limits.window_start <= now() - %s * interval '1 second'
                              THEN 1 ELSE auth_rate_limits.hits + 1 END,
                  window_start = CASE WHEN auth_rate_limits.window_start <= now() - %s * interval '1 second'
                                      THEN now() ELSE auth_rate_limits.window_start END
                RETURNING hits,
                  GREATEST(1, CEIL(EXTRACT(EPOCH FROM
                    (window_start + %s * interval '1 second' - clock_timestamp()))))::int AS retry_after""",
                (digest(scope + "\0" + subject), seconds, seconds, seconds),
            ).fetchone()

        limit = await self.db.run(update)
        if limit["hits"] > maximum:
            if cooldown:
                raise email_cooldown(limit["retry_after"])
            raise ServiceError(429, "rate_limited", "请求过于频繁，请稍后重试", retry_after=limit["retry_after"])

    async def _deliver(self, user_id, email, purpose, secret):
        try:
            await self.mailer.send(email, purpose, secret)
        except Exception as exc:
            # Neither SMTP errors nor recipient/token values enter API responses/logs.
            logging.getLogger(__name__).warning("Account mail delivery failed (%s)", type(exc).__name__)
            await self.db.run(lambda c: audit(c, user_id, "mail." + purpose, "delivery_failed"))

    def _require_mailer(self):
        if isinstance(self.mailer, DisabledMailer):
            raise ServiceError(503, "mail_unavailable", "邮件服务未配置，请联系站点维护人员")

    @staticmethod
    def _challenge(connection, user_id, purpose, secret, session_id=None):
        connection.execute(
            "UPDATE account_tokens SET consumed_at=now() WHERE user_id=%s AND purpose=%s AND consumed_at IS NULL",
            (user_id, purpose),
        )
        connection.execute(
            """INSERT INTO account_tokens(token_hash,user_id,purpose,session_id,created_at,expires_at)
            VALUES (%s,%s,%s,%s,statement_timestamp(),statement_timestamp()+%s*interval '1 second')""",
            (digest(secret), user_id, purpose, session_id, EMAIL_TOKEN_SECONDS[purpose]),
        )

    @staticmethod
    def _resend_remaining(connection, user_id, purpose):
        row = connection.execute(
            """SELECT GREATEST(0, COALESCE(CEIL(EXTRACT(EPOCH FROM
                (MAX(created_at) + %s * interval '1 second' - clock_timestamp()))), 0))::int AS remaining
            FROM account_tokens WHERE user_id=%s AND purpose=%s""",
            (EMAIL_RESEND_SECONDS, user_id, purpose),
        ).fetchone()
        return row["remaining"]

    async def register(self, email, password, display_name, ip):
        email = email_address(email)
        if email.rsplit("@", 1)[1] not in REGISTRATION_EMAIL_DOMAINS:
            raise ServiceError(422, "email_provider_not_supported", "仅支持 QQ、网易、Gmail 和 Outlook 邮箱注册")
        if self.settings.require_verified_email:
            self._require_mailer()
        await self.rate_limit("register.ip", ip, 10, 3600)
        encoded = await self.passwords.hash(password)
        secret = token(EMAIL_TOKEN_PREFIXES["verify"])

        def create(connection):
            row = connection.execute(
                """INSERT INTO users(email,password_hash,display_name) VALUES (%s,%s,%s)
                ON CONFLICT(email) DO NOTHING RETURNING id""", (email, encoded, display_name),
            ).fetchone()
            if row:
                self._challenge(connection, row["id"], "verify", secret)
                audit(connection, row["id"], "register", "created")
            return row

        row = await self.db.run(create)
        if row:
            await self._deliver(row["id"], email, "verify", secret)
        # Same response for existing accounts. Resending is a separate throttled endpoint.

    @staticmethod
    def _store_auth_token(connection, session_id, kind, prefix, expires_at):
        secret = token(prefix)
        connection.execute(
            "INSERT INTO auth_tokens(token_hash,session_id,kind,expires_at) VALUES (%s,%s,%s,%s)",
            (digest(secret), session_id, kind, expires_at),
        )
        return secret

    def _issue(self, connection, session_id, expires_at):
        now = datetime.now(timezone.utc)
        access = self._store_auth_token(
            connection, session_id, "access", "ac", min(expires_at, now + timedelta(seconds=self.settings.access_seconds)),
        )
        refresh = self._store_auth_token(
            connection, session_id, "refresh", "rf", min(expires_at, now + timedelta(seconds=self.settings.refresh_seconds)),
        )
        return {"access_token": access, "refresh_token": refresh, "token_type": "Bearer",
                "expires_in": max(0, min(self.settings.access_seconds, int((expires_at - now).total_seconds()))),
                "session_id": session_id}

    async def login(self, email, password, ip, label, *, remember_me=False):
        email = email_address(email)
        await self.rate_limit("login.ip", ip, 60, 900)
        await self.rate_limit("login.email", email, 12, 900)
        row = await self.db.run(lambda c: c.execute("SELECT * FROM users WHERE email=%s", (email,)).fetchone())
        valid = await self.passwords.verify(row["password_hash"] if row else None, password)
        if not row or not row["password_hash"] or not valid or not row["is_active"]:
            await self.db.run(lambda c: audit(c, None, "login", "rejected", email))
            raise unauthorized()

        def issue(connection):
            current = connection.execute("SELECT * FROM users WHERE id=%s FOR UPDATE", (row["id"],)).fetchone()
            if not current or not current["is_active"] or current["password_hash"] != row["password_hash"]:
                raise unauthorized()
            if (self.settings.require_verified_email or remember_me) and not current["verified_at"]:
                raise ServiceError(403, "email_not_verified", "请先验证邮箱")
            session = connection.execute(
                """INSERT INTO auth_sessions(user_id,label,expires_at)
                VALUES (%s,%s,now()+%s*interval '1 second') RETURNING id,expires_at""",
                (row["id"], label[:200], self.settings.session_seconds),
            ).fetchone()
            audit(connection, row["id"], "login", "accepted")
            result = self._issue(connection, session["id"], session["expires_at"])
            if remember_me:
                now = datetime.now(timezone.utc)
                expires = min(session["expires_at"], now + timedelta(seconds=REMEMBER_SECONDS))
                result["remember_token"] = self._store_auth_token(connection, session["id"], "remember", "rm", expires)
                result["remember_expires_in"] = max(1, ceil((expires - now).total_seconds()))
            return result

        return await self.db.run(issue)

    async def remember(self, secret, ip):
        await self.rate_limit("remember.ip", ip, 120, 900)
        hashed = token_hash(secret, "rm")

        def restore(connection):
            session = connection.execute(
                """SELECT s.id,LEAST(s.expires_at,t.expires_at) AS expires_at
                FROM auth_sessions s JOIN auth_tokens t ON t.session_id=s.id JOIN users u ON u.id=s.user_id
                WHERE t.token_hash=%s AND t.kind='remember' AND t.consumed_at IS NULL
                  AND t.expires_at>clock_timestamp() AND s.expires_at>clock_timestamp() AND s.revoked_at IS NULL
                  AND u.is_active AND u.verified_at IS NOT NULL FOR UPDATE OF s""", (hashed,),
            ).fetchone()
            if not session:
                raise unauthorized()
            connection.execute(
                "UPDATE auth_tokens SET consumed_at=now() WHERE session_id=%s AND kind='access' AND consumed_at IS NULL",
                (session["id"],),
            )
            connection.execute("UPDATE auth_sessions SET last_seen_at=now() WHERE id=%s", (session["id"],))
            now = datetime.now(timezone.utc)
            expires = session["expires_at"]
            access = self._store_auth_token(
                connection, session["id"], "access", "ac", min(expires, now + timedelta(seconds=self.settings.access_seconds)),
            )
            return {"access_token": access, "token_type": "Bearer",
                    "remember_expires_in": max(1, ceil((expires - now).total_seconds()))}

        return await self.db.run(restore)

    async def authenticate(self, secret):
        row = await self.db.run(lambda c: c.execute(
            """SELECT s.id AS session_id,s.user_id,u.role FROM auth_tokens t
            JOIN auth_sessions s ON s.id=t.session_id JOIN users u ON u.id=s.user_id
            WHERE t.token_hash=%s AND t.kind='access' AND t.consumed_at IS NULL
              AND t.expires_at>now() AND s.expires_at>now() AND s.revoked_at IS NULL AND u.is_active
              AND (%s = FALSE OR u.verified_at IS NOT NULL)""",
            (token_hash(secret, "ac"), self.settings.require_verified_email),
        ).fetchone())
        if not row:
            raise unauthorized()
        return Principal(row["user_id"], row["session_id"], UserRole(row["role"]))

    async def refresh(self, secret, ip):
        await self.rate_limit("refresh.ip", ip, 120, 900)
        hashed = token_hash(secret, "rf")

        def rotate(connection):
            session = connection.execute(
                """SELECT s.* FROM auth_sessions s JOIN auth_tokens t ON t.session_id=s.id
                WHERE t.token_hash=%s AND t.kind='refresh' FOR UPDATE OF s""", (hashed,),
            ).fetchone()
            if not session:
                return None
            # Read again after locking the session, so simultaneous refreshes see consumption.
            old = connection.execute(
                "SELECT *,expires_at>now() AS valid FROM auth_tokens WHERE token_hash=%s", (hashed,),
            ).fetchone()
            if old["consumed_at"]:
                connection.execute("UPDATE auth_sessions SET revoked_at=COALESCE(revoked_at,now()) WHERE id=%s", (session["id"],))
                audit(connection, session["user_id"], "refresh", "replay_revoked")
                return None  # Commit revocation before returning 401.
            active = connection.execute(
                "SELECT 1 FROM users WHERE id=%s AND is_active AND (%s=FALSE OR verified_at IS NOT NULL)",
                (session["user_id"], self.settings.require_verified_email),
            ).fetchone()
            if not active or session["revoked_at"] or session["expires_at"] <= datetime.now(timezone.utc) or not old["valid"]:
                return None
            connection.execute("UPDATE auth_tokens SET consumed_at=now() WHERE token_hash=%s", (hashed,))
            connection.execute(
                "UPDATE auth_tokens SET consumed_at=now() WHERE session_id=%s AND kind='access' AND consumed_at IS NULL",
                (session["id"],),
            )
            connection.execute("UPDATE auth_sessions SET last_seen_at=now() WHERE id=%s", (session["id"],))
            return self._issue(connection, session["id"], session["expires_at"])

        result = await self.db.run(rotate)
        if not result:
            raise unauthorized()
        return result

    async def request_challenge(self, email, purpose, ip):
        self._require_mailer()
        email = email_address(email)
        await self.rate_limit("mail.ip", ip, 30, 3600)
        # Apply the public cooldown regardless of whether the address exists.
        await self.rate_limit("mail.cooldown." + purpose, email, 1, EMAIL_RESEND_SECONDS, cooldown=True)
        await self.rate_limit("mail.email." + purpose, email, 5, 3600)
        secret = token(EMAIL_TOKEN_PREFIXES[purpose])

        def create(connection):
            user = connection.execute("SELECT * FROM users WHERE email=%s AND is_active FOR UPDATE", (email,)).fetchone()
            if not user or purpose == "verify" and user["verified_at"]:
                return None
            # Registration also issues a verification token. Keep the public response generic.
            if self._resend_remaining(connection, user["id"], purpose):
                return None
            self._challenge(connection, user["id"], purpose, secret)
            audit(connection, user["id"], "challenge." + purpose, "created")
            return user["id"]

        user_id = await self.db.run(create)
        if user_id:
            await self._deliver(user_id, email, purpose, secret)

    async def redeem_challenge(self, secret, purpose, new_password=None):
        hashed = token_hash(secret, EMAIL_TOKEN_PREFIXES[purpose])
        encoded = await self.passwords.hash(new_password) if purpose == "reset" else None

        def redeem(connection):
            owner = connection.execute(
                "SELECT user_id FROM account_tokens WHERE token_hash=%s AND purpose=%s", (hashed, purpose),
            ).fetchone()
            if not owner:
                return False
            user = connection.execute("SELECT id FROM users WHERE id=%s AND is_active FOR UPDATE", (owner["user_id"],)).fetchone()
            row = connection.execute(
                """SELECT 1 FROM account_tokens WHERE token_hash=%s AND purpose=%s AND consumed_at IS NULL
                AND expires_at>clock_timestamp() AND created_at+%s*interval '1 second'>clock_timestamp()""",
                (hashed, purpose, EMAIL_TOKEN_SECONDS[purpose]),
            ).fetchone()
            if not user or not row:
                return False
            if purpose == "verify":
                connection.execute("UPDATE users SET verified_at=COALESCE(verified_at,now()),updated_at=now() WHERE id=%s", (user["id"],))
            else:
                connection.execute("UPDATE users SET password_hash=%s,updated_at=now() WHERE id=%s", (encoded, user["id"],))
                connection.execute("UPDATE auth_sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL", (user["id"],))
            connection.execute(
                "UPDATE account_tokens SET consumed_at=now() WHERE user_id=%s AND purpose=%s AND consumed_at IS NULL",
                (user["id"], purpose),
            )
            audit(connection, user["id"], purpose, "completed")
            return True

        if not await self.db.run(redeem):
            raise ServiceError(400, "invalid_account_token", "一次性令牌无效或已过期")

    async def _verify_current_password(self, principal, password):
        user = await self.db.run(lambda c: c.execute(
            "SELECT password_hash FROM users WHERE id=%s AND is_active", (principal.user_id,),
        ).fetchone())
        if not user:
            raise unauthorized()
        if not user["password_hash"]:
            raise ServiceError(409, "password_not_set", "请通过找回密码为该邮箱设置密码")
        if not await self.passwords.verify(user["password_hash"], password):
            raise ServiceError(403, "incorrect_password", "当前密码不正确")
        return user["password_hash"]

    @staticmethod
    def _password_change_account(connection, principal, expected_password):
        current = connection.execute(
            "SELECT email,password_hash FROM users WHERE id=%s AND is_active FOR NO KEY UPDATE", (principal.user_id,),
        ).fetchone()
        if not current or current["password_hash"] != expected_password:
            raise unauthorized()
        session = connection.execute(
            """SELECT 1 FROM auth_sessions WHERE id=%s AND user_id=%s AND revoked_at IS NULL
            AND expires_at>clock_timestamp() FOR UPDATE""", (principal.session_id, principal.user_id),
        ).fetchone()
        if not session:
            raise unauthorized()
        return current

    async def request_password_change(self, principal, old_password, ip):
        self._require_mailer()
        await self.rate_limit("change.request.user", str(principal.user_id), 10, 900)
        expected = await self._verify_current_password(principal, old_password)
        await self.rate_limit("mail.ip", ip, 30, 3600)
        secret = token(EMAIL_TOKEN_PREFIXES["change"])

        def create(connection):
            account = self._password_change_account(connection, principal, expected)
            remaining = self._resend_remaining(connection, principal.user_id, "change")
            if remaining:
                raise email_cooldown(remaining)
            self._challenge(connection, principal.user_id, "change", secret, principal.session_id)
            audit(connection, principal.user_id, "challenge.change", "created")
            return account["email"]

        email = await self.db.run(create)
        await self._deliver(principal.user_id, email, "change", secret)

    async def change_password(self, principal, old_password, new_password, secret):
        expected = await self._verify_current_password(principal, old_password)
        encoded = await self.passwords.hash(new_password)
        hashed = token_hash(secret, EMAIL_TOKEN_PREFIXES["change"])

        def change(connection):
            self._password_change_account(connection, principal, expected)
            challenge = connection.execute(
                """SELECT 1 FROM account_tokens WHERE token_hash=%s AND user_id=%s AND session_id=%s
                AND purpose='change' AND consumed_at IS NULL AND expires_at>clock_timestamp()""",
                (hashed, principal.user_id, principal.session_id),
            ).fetchone()
            if not challenge:
                raise ServiceError(400, "invalid_account_token", "修改密码的邮箱验证令牌无效或已过期")
            connection.execute("UPDATE users SET password_hash=%s,updated_at=now() WHERE id=%s", (encoded, principal.user_id))
            connection.execute("UPDATE auth_sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL", (principal.user_id,))
            connection.execute("UPDATE account_tokens SET consumed_at=now() WHERE user_id=%s AND consumed_at IS NULL", (principal.user_id,))
            audit(connection, principal.user_id, "password.change", "completed")

        await self.db.run(change)

    async def user(self, principal):
        row = await self.db.run(lambda c: c.execute("SELECT * FROM users WHERE id=%s", (principal.user_id,)).fetchone())
        return user_view(row)

    async def sessions(self, principal):
        return await self.db.run(lambda c: c.execute(
            """SELECT id,label,auth_method,created_at,last_seen_at,expires_at FROM auth_sessions
            WHERE user_id=%s AND revoked_at IS NULL AND expires_at>now() ORDER BY created_at DESC,id DESC""",
            (principal.user_id,),
        ).fetchall())

    async def revoke(self, principal, session_id=None, all_sessions=False):
        def update(connection):
            if all_sessions:
                connection.execute("UPDATE auth_sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL", (principal.user_id,))
            else:
                row = connection.execute(
                    "UPDATE auth_sessions SET revoked_at=COALESCE(revoked_at,now()) WHERE id=%s AND user_id=%s RETURNING id",
                    (session_id or principal.session_id, principal.user_id),
                ).fetchone()
                if not row:
                    raise not_found()
            audit(connection, principal.user_id, "logout.all" if all_sessions else "logout", "completed")

        await self.db.run(update)
