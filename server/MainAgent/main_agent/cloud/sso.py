import hashlib
import hmac
import re
from dataclasses import asdict

from psycopg.types.json import Jsonb

from .auth import audit
from .errors import ServiceError, not_found, unauthorized
from .oidc import ExternalIdentity, OIDCProvider, b64, challenge, sso_error
from .security import digest, token, token_hash


def create_provider(settings):
    protocol = settings.selected_sso_protocol
    if protocol == "oidc":
        return OIDCProvider(settings)
    if protocol == "cas":
        from .cas import CASProvider
        return CASProvider(settings)
    if protocol == "saml2":
        from .saml import SAMLProvider
        return SAMLProvider(settings)
    return None


class SSOService:
    def __init__(self, database, auth, settings, provider=None):
        self.db, self.auth, self.settings = database, auth, settings
        self.provider = provider

    def require_provider(self):
        if self.provider is None:
            raise ServiceError(503, "sso_not_configured", "校园 SSO 尚未配置")
        return self.provider

    def _verifier(self, state):
        return b64(hmac.new(self.settings.sso_state_secret.encode(), b"sso-pkce\0" + state.encode(), hashlib.sha256).digest())

    @property
    def cookie_name(self):
        return "__Host-cloud-sso" if not self.settings.development else "cloud-sso-dev"

    async def start(self, mode, client_challenge, ip, principal=None, password=None, *, website_origin=None):
        provider = self.require_provider()
        await self.auth.rate_limit("sso.start.ip", ip, 30, 900)
        if mode not in {"login", "link"} or not re.fullmatch(r"[A-Za-z0-9_-]{43}", client_challenge):
            raise ServiceError(422, "invalid_sso_request", "SSO 模式或客户端 PKCE challenge 无效")
        expected_password = None
        if mode == "link":
            if principal is None or not password:
                raise unauthorized()
            user = await self.db.run(lambda c: c.execute("SELECT password_hash FROM users WHERE id=%s", (principal.user_id,)).fetchone())
            if not user or not user["password_hash"] or not await self.auth.passwords.verify(user["password_hash"], password):
                raise ServiceError(403, "campus_password_incorrect", "当前邮箱密码不正确")
            expected_password = user["password_hash"]
        state, nonce, browser = token("st"), token("nn"), token("sb")
        options = {}
        if website_origin is not None and provider.protocol == "cas" and self.settings.cas_local:
            options["website_origin"] = website_origin
        authorization = await provider.begin(state, nonce, challenge(self._verifier(state)), mode, **options)

        def save(connection):
            if mode == "link":
                user = connection.execute("SELECT password_hash,is_active,verified_at FROM users WHERE id=%s FOR UPDATE", (principal.user_id,)).fetchone()
                session = connection.execute("SELECT 1 FROM auth_sessions WHERE id=%s AND user_id=%s AND revoked_at IS NULL AND expires_at>now()",
                                             (principal.session_id, principal.user_id)).fetchone()
                if not user or not user["is_active"] or not user["verified_at"] or user["password_hash"] != expected_password or not session:
                    raise unauthorized()
            connection.execute(
                """INSERT INTO sso_transactions(state_hash,issuer,protocol,request_id,nonce_hash,browser_hash,
                client_challenge,mode,link_user_id,link_session_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (digest(state), provider.issuer, provider.protocol, authorization.request_id, digest(nonce), digest(browser),
                 client_challenge, mode, principal.user_id if mode == "link" else None,
                 principal.session_id if mode == "link" else None),
            )
        await self.db.run(save)
        result = {"authorization_url": authorization.url, "expires_in": 600}
        if provider.protocol == "cas" and self.settings.cas_local:
            result["local_cas"] = True
        return result, browser

    async def callback(self, state, browser, parameters, ip):
        provider = self.require_provider()
        await self.auth.rate_limit("sso.callback.ip", ip, 60, 900)
        if not browser or not state:
            raise sso_error()
        flow = await self.db.run(lambda c: c.execute(
            """UPDATE sso_transactions SET consumed_at=now() WHERE state_hash=%s AND browser_hash=%s
            AND issuer=%s AND protocol=%s AND consumed_at IS NULL AND expires_at>now() RETURNING *""",
            (token_hash(state, "st"), token_hash(browser, "sb"), provider.issuer, provider.protocol),
        ).fetchone())
        if not flow:
            raise sso_error()
        flow["state"] = state
        identity = await provider.authenticate(parameters, flow, self._verifier(state))
        if (not isinstance(identity, ExternalIdentity) or identity.issuer != provider.issuer
            or not isinstance(identity.subject, str) or not 1 <= len(identity.subject) <= 255):
            raise sso_error()
        completion = token("sc")

        def save(connection):
            connection.execute(
                """INSERT INTO sso_completions(code_hash,user_id,protocol,identity,client_challenge,mode,link_session_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (digest(completion), flow["link_user_id"], provider.protocol, Jsonb(asdict(identity)),
                 flow["client_challenge"], flow["mode"], flow["link_session_id"]),
            )
        await self.db.run(save)
        return {"completion_code": completion, "expires_in": 60, "mode": flow["mode"]}

    def _resolve(self, connection, flow, identity):
        protocol = flow["protocol"]
        material = protocol + "\0" + identity.issuer + "\0" + identity.subject
        lock = int.from_bytes(hashlib.sha256(material.encode()).digest()[:8], "big", signed=True)
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
        existing = connection.execute("SELECT user_id FROM external_identities WHERE protocol=%s AND issuer=%s AND subject=%s",
                                      (protocol, identity.issuer, identity.subject)).fetchone()
        if flow["mode"] == "link":
            user = connection.execute("SELECT * FROM users WHERE id=%s FOR UPDATE", (flow["user_id"],)).fetchone()
            session = connection.execute("SELECT 1 FROM auth_sessions WHERE id=%s AND user_id=%s AND revoked_at IS NULL AND expires_at>now() FOR UPDATE",
                                         (flow["link_session_id"], flow["user_id"])).fetchone()
            if not user or not user["is_active"] or not user["verified_at"] or not session:
                raise unauthorized()
            if existing and existing["user_id"] != user["id"]:
                raise ServiceError(409, "identity_already_linked", "该校园身份已绑定其他账户")
            if self.settings.require_campus_profile and not existing and connection.execute(
                "SELECT 1 FROM external_identities WHERE user_id=%s AND protocol=%s AND issuer=%s",
                (user["id"], protocol, identity.issuer),
            ).fetchone():
                raise ServiceError(409, "campus_identity_exists", "该邮箱已绑定校园账号，需要换绑请联系管理员")
        elif existing:
            user = connection.execute("SELECT * FROM users WHERE id=%s FOR UPDATE", (existing["user_id"],)).fetchone()
        else:
            if not identity.email or not identity.email_verified:
                raise ServiceError(409, "account_link_required", "请先注册并验证邮箱账号，再显式绑定校园身份")
            # Never merge with a matching email; require ownership of the existing account.
            user = connection.execute(
                """INSERT INTO users(email,password_hash,display_name,verified_at) VALUES (%s,NULL,%s,now())
                ON CONFLICT(email) DO NOTHING RETURNING *""",
                (identity.email, None if self.settings.require_campus_profile else identity.display_name),
            ).fetchone()
            if not user:
                raise ServiceError(409, "account_link_required", "请先登录已有邮箱账号，再显式绑定校园身份")
        if not user or not user["is_active"] or self.settings.require_verified_email and not user["verified_at"]:
            raise unauthorized()
        if not existing:
            connection.execute("INSERT INTO external_identities(user_id,protocol,issuer,subject) VALUES (%s,%s,%s,%s)",
                               (user["id"], protocol, identity.issuer, identity.subject))
        if identity.assertion_id:
            replay = connection.execute(
                "INSERT INTO sso_assertion_replays(issuer,assertion_hash) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING issuer",
                (identity.issuer, digest(identity.assertion_id)),
            ).fetchone()
            if not replay:
                raise sso_error()
        return user["id"]

    async def finish(self, secret, verifier, ip, label):
        provider = self.require_provider()
        await self.auth.rate_limit("sso.finish.ip", ip, 60, 900)
        if not isinstance(verifier, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
            raise sso_error()

        def finish(connection):
            flow = connection.execute(
                "SELECT * FROM sso_completions WHERE code_hash=%s AND consumed_at IS NULL AND expires_at>now() FOR UPDATE",
                (token_hash(secret, "sc"),),
            ).fetchone()
            if not flow or flow["protocol"] != provider.protocol or not hmac.compare_digest(flow["client_challenge"], challenge(verifier)):
                raise sso_error()
            identity = ExternalIdentity(**flow["identity"])
            if identity.issuer != provider.issuer:
                raise sso_error()
            user_id = self._resolve(connection, flow, identity)
            connection.execute("UPDATE sso_completions SET consumed_at=now() WHERE code_hash=%s", (token_hash(secret, "sc"),))
            if flow["mode"] == "link":
                audit(connection, user_id, "sso.link", "completed")
                return {"linked": True}
            session = connection.execute(
                "INSERT INTO auth_sessions(user_id,label,auth_method,expires_at) VALUES (%s,%s,'campus_sso',now()+%s*interval '1 second') RETURNING id,expires_at",
                (user_id, label[:200], self.settings.session_seconds),
            ).fetchone()
            audit(connection, user_id, "sso.login", "accepted")
            return self.auth._issue(connection, session["id"], session["expires_at"])
        return await self.db.run(finish)

    async def identities(self, principal):
        return await self.db.run(lambda c: c.execute(
            "SELECT id,protocol,issuer,subject,created_at FROM external_identities WHERE user_id=%s ORDER BY created_at,id",
            (principal.user_id,),
        ).fetchall())

    async def unlink(self, principal, identity_id, password):
        user = await self.db.run(lambda c: c.execute("SELECT password_hash FROM users WHERE id=%s", (principal.user_id,)).fetchone())
        if not user or not user["password_hash"] or not await self.auth.passwords.verify(user["password_hash"], password):
            raise unauthorized()

        def unlink(connection):
            current = connection.execute("SELECT password_hash FROM users WHERE id=%s FOR UPDATE", (principal.user_id,)).fetchone()
            session = connection.execute("SELECT 1 FROM auth_sessions WHERE id=%s AND user_id=%s AND revoked_at IS NULL AND expires_at>now()",
                                         (principal.session_id, principal.user_id)).fetchone()
            if not current or current["password_hash"] != user["password_hash"] or not session:
                raise unauthorized()
            removed = connection.execute("DELETE FROM external_identities WHERE id=%s AND user_id=%s RETURNING id",
                                         (identity_id, principal.user_id)).fetchone()
            if not removed:
                raise not_found()
            connection.execute("UPDATE auth_sessions SET revoked_at=now() WHERE user_id=%s AND auth_method='campus_sso' AND revoked_at IS NULL", (principal.user_id,))
            audit(connection, principal.user_id, "sso.unlink", "completed")
        await self.db.run(unlink)
