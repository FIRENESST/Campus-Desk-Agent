import asyncio
import importlib.util
import os
import shutil
import unittest
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4


TEST_PASSWORD = "campus1a"
AVAILABLE = all(importlib.util.find_spec(name) for name in ("psycopg", "argon2", "email_validator", "starlette", "jwt"))
ENABLED = AVAILABLE and os.getenv("CLOUD_TEST_POSTGRES") == "1"
if ENABLED:
    import httpx2
    import psycopg
    from cloud_support import TemporaryPostgres
    from main_agent.campus_cluster import AgentId, AgentReply, Status, build_campus_cluster
    from main_agent.cloud.api import create_app
    from main_agent.cloud.auth import set_user_role
    from main_agent.cloud.config import Settings
    from main_agent.cloud.errors import ServiceError
    from main_agent.cloud.migrate import MIGRATIONS, migrate
    from main_agent.cloud.roles import UserRole
    from main_agent.cloud.runner import CampusRunner, RunOutput
    from main_agent.cloud.security import digest
    from main_agent.cloud.worker import WorkerPool


class CaptureMailer:
    def __init__(self):
        self.messages = []

    async def send(self, recipient, purpose, secret):
        self.messages.append((recipient, purpose, secret))


class EchoRunner:
    def __init__(self):
        self.contexts = []

    async def run(self, context):
        self.contexts.append(context)
        return RunOutput("答复：" + context.query, "completed", artifacts=(
            {"kind": "test_receipt", "payload": {"owner": str(context.user_id)}},))


@unittest.skipUnless(ENABLED, "Install requirements-cloud.txt and set CLOUD_TEST_POSTGRES=1 for isolated PostgreSQL integration tests")
class CloudPostgresTestCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg = TemporaryPostgres().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.pg.__exit__(None, None, None)

    async def asyncSetUp(self):
        self.name, self.admin_dsn, self.app_dsn = await asyncio.to_thread(self.pg.create_database)
        await asyncio.to_thread(migrate, self.admin_dsn)
        self.config = self.configure_settings(Settings(self.app_dsn, development=True, require_campus_profile=False,
                                                      workers=0, lease_seconds=6,
                                                      max_instances=3, max_active_runs_per_user=2))
        self.mailer, self.runner = CaptureMailer(), EchoRunner()
        self.app = create_app(self.config, runner=self.runner, mailer=self.mailer, **self.extra_app_arguments())
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), base_url="http://test")
        self.store, self.db = self.app.state.store, self.app.state.db
        self.worker = WorkerPool(self.store, self.runner, self.config)

    def configure_settings(self, settings):
        return settings

    def extra_app_arguments(self):
        return {}

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        await asyncio.to_thread(self.pg.drop_database, self.name)

    async def account(self, name="alice"):
        email, password = name + "@gmail.com", TEST_PASSWORD
        result = await self.client.post("/v1/auth/register", json={"email": email, "password": password})
        self.assertEqual(result.status_code, 202, result.text)
        secret = next(item[2] for item in reversed(self.mailer.messages) if item[0] == email and item[1] == "verify")
        result = await self.client.post("/v1/auth/verification/confirm", json={"token": secret})
        self.assertEqual(result.status_code, 204, result.text)
        result = await self.client.post("/v1/auth/login", json={"email": email, "password": password})
        self.assertEqual(result.status_code, 200, result.text)
        tokens = result.json()
        return {"Authorization": "Bearer " + tokens["access_token"]}, tokens

    async def conversation(self, headers, name="实例"):
        response = await self.client.post("/v1/instances", json={"name": name}, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        instance = response.json()
        response = await self.client.post(f"/v1/instances/{instance['id']}/conversations", json={}, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        return instance, response.json()

    async def submit(self, headers, conversation, query="你好", key=None, targets=None):
        return await self.client.post(f"/v1/conversations/{conversation['id']}/runs",
                                      json={"query": query, "targets": targets or []},
                                      headers={**headers, "Idempotency-Key": key or str(uuid4())})


class CloudIntegrationTests(CloudPostgresTestCase):
    async def test_wechat_following_is_persisted_per_account(self):
        endpoint = "/v1/campus-digest/wechat/following"
        account_id = "a" * 64
        alice, _ = await self.account("alice")
        bob, _ = await self.account("bob")
        self.assertEqual((await self.client.get(endpoint)).status_code, 401)

        async def digest(path, *, params, runtime_key=None, payload=None):
            if path.endswith("/accounts"):
                return {"accounts": [{"id": account_id, "name": "校园新闻"}] if params["id"] == account_id else []}
            expected = {"following": [account_id]} if path.startswith("/internal/") else None
            self.assertEqual(payload, expected)
            return {"items": [], "page_size": 12}

        with patch("main_agent.cloud.wechat_following.digest_request", digest), patch.dict(os.environ, {"MPM_RUNTIME_STATUS_KEY": "fixture"}):
            change = {"account_id": account_id, "followed": True}
            for _ in range(2):
                result = await self.client.put(endpoint, headers=alice, json=change)
                self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual((await self.client.get(endpoint, headers=alice)).json()["following"], [account_id])
            self.assertEqual((await self.client.get(endpoint, headers=bob)).json()["following"], [])
            result = await self.client.put(endpoint, headers=bob, json={**change, "account_id": "b" * 64})
            self.assertEqual(result.status_code, 404)
            result = await self.client.put(endpoint, headers=alice, json={**change, "followed": False})
            self.assertEqual(result.status_code, 200)
            self.assertEqual((await self.client.get(endpoint, headers=alice)).json()["following"], [])

    async def test_wechat_following_preserves_feed_before_migration(self):
        headers, _ = await self.account()
        endpoint = "/v1/campus-digest/wechat/following"
        with psycopg.connect(self.admin_dsn) as conn:
            conn.execute("DROP TABLE user_wechat_following")

        with patch("main_agent.cloud.wechat_following.digest_request", return_value={"items": [{"id": "post"}]}):
            result = await self.client.get(endpoint, headers=headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {"items": [{"id": "post"}], "following": [], "following_enabled": False})
        result = await self.client.put(endpoint, headers=headers, json={"account_id": "a" * 64, "followed": False})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()["error"]["code"], "wechat_following_unavailable")

    async def test_sso_is_explicitly_disabled_until_configured(self):
        result = (await self.client.get("/v1/auth/providers")).json()
        self.assertEqual([item["type"] for item in result["providers"]], ["password"])
        response = await self.client.post("/v1/auth/sso/campus/start", json={"code_challenge": "x"*43})
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (503, "sso_not_configured"))

    async def test_registration_verification_login_and_hashed_secrets(self):
        response = await self.client.post("/v1/auth/register", json={"email": "ALICE@gmail.com", "password": TEST_PASSWORD})
        self.assertEqual(response.status_code, 202)
        response = await self.client.post("/v1/auth/login", json={
            "email": "alice@gmail.com", "password": TEST_PASSWORD, "remember_me": True,
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "email_not_verified")
        challenge = self.mailer.messages[-1][2]
        self.assertEqual((await self.client.post("/v1/auth/verification/confirm", json={"token": challenge})).status_code, 204)
        self.assertEqual((await self.client.post("/v1/auth/verification/confirm", json={"token": challenge})).status_code, 400)
        response = await self.client.post("/v1/auth/login", json={"email": "alice@gmail.com", "password": TEST_PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("remember_token", response.json())
        access = response.json()["access_token"]
        user = await self.db.run(lambda c: c.execute("SELECT * FROM users").fetchone())
        self.assertTrue(user["password_hash"].startswith("$argon2id$"))
        hashes = await self.db.run(lambda c: c.execute("SELECT token_hash FROM auth_tokens").fetchall())
        self.assertIn(digest(access), [row["token_hash"] for row in hashes])
        me = await self.client.get("/v1/me", headers={"Authorization": "Bearer " + access})
        self.assertNotIn("password_hash", me.text)
        self.assertEqual(me.headers["cache-control"], "no-store")
        instances = (await self.client.get("/v1/instances", headers={"Authorization": "Bearer " + access})).json()["items"]
        self.assertEqual([(row["user_id"], row["name"], row["is_default"]) for row in instances],
                         [(me.json()["id"], "默认实例", True)])

    async def test_account_role_and_title_require_a_privileged_database_connection(self):
        email = "alice@gmail.com"
        response = await self.client.post("/v1/auth/register", json={
            "email": email, "password": TEST_PASSWORD, "role": "teacher", "academic_title": "教授",
        })
        self.assertEqual(response.status_code, 422)
        headers, tokens = await self.account()

        async def profile():
            response = await self.client.get("/v1/me", headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            return data["role"], data["academic_title"]

        self.assertEqual(await profile(), ("student", None))
        with self.assertRaisesRegex(ValueError, "助教、讲师、副教授、教授"):
            await asyncio.to_thread(set_user_role, self.admin_dsn, email, UserRole.TEACHER, "自定义称呼")
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            await asyncio.to_thread(set_user_role, self.app_dsn, email, UserRole.TEACHER, "教授")

        def check_database_constraints():
            with psycopg.connect(self.app_dsn, autocommit=True) as connection:
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    connection.execute("INSERT INTO users(email,role) VALUES ('other@gmail.com','teacher')")
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    connection.execute("UPDATE users SET academic_title='讲师' WHERE email=%s", (email,))
            with psycopg.connect(self.admin_dsn, autocommit=True) as connection:
                with self.assertRaises(psycopg.errors.CheckViolation):
                    connection.execute("UPDATE users SET role='admin' WHERE email=%s", (email,))
                with self.assertRaises(psycopg.errors.CheckViolation):
                    connection.execute("UPDATE users SET academic_title='讲师' WHERE email=%s", (email,))

        await asyncio.to_thread(check_database_constraints)
        await asyncio.to_thread(set_user_role, self.admin_dsn, "ALICE@gmail.com", UserRole.TEACHER, " 教授 ")
        self.assertEqual(await profile(), ("teacher", "教授"))
        self.assertIs((await self.app.state.auth.authenticate(tokens["access_token"])).role, UserRole.TEACHER)
        for title in ("助教", "讲师", "副教授"):
            await asyncio.to_thread(set_user_role, self.admin_dsn, email, UserRole.TEACHER, title)
            self.assertEqual(await profile(), ("teacher", title))
        await asyncio.to_thread(set_user_role, self.admin_dsn, email, UserRole.STUDENT)
        self.assertEqual(await profile(), ("student", None))
        self.assertIs((await self.app.state.auth.authenticate(tokens["access_token"])).role, UserRole.STUDENT)

    async def test_registration_accepts_only_the_selected_email_domains(self):
        supported = ("qq.com", "163.com", "126.com", "yeah.net", "gmail.com",
                     "outlook.com", "hotmail.com", "live.com", "msn.com")
        for domain in ("yahoo.com", "icloud.com", "sub.qq.com", "outlook.com.example.org", *supported):
            with self.subTest(domain=domain):
                response = await self.client.post("/v1/auth/register", json={
                    "email": "test-account@" + domain.upper(), "password": TEST_PASSWORD,
                })
                if domain in supported:
                    self.assertEqual(response.status_code, 202, response.text)
                else:
                    self.assertEqual((response.status_code, response.json()["error"]["code"]),
                                     (422, "email_provider_not_supported"))
        self.assertEqual([email for email, purpose, _ in self.mailer.messages if purpose == "verify"],
                         ["test-account@" + domain for domain in supported])
        count = await self.db.run(lambda c: c.execute("SELECT count(*) AS n FROM users").fetchone()["n"])
        self.assertEqual(count, len(supported))

    async def test_registration_rejects_short_and_incomplete_passwords(self):
        for password in ("short1a", "UPPER123", "lettersOnly", "12345678"):
            result = await self.client.post("/v1/auth/register", json={
                "email": "policy@gmail.com", "password": password,
            })
            self.assertEqual(result.status_code, 422)
        self.assertFalse(self.mailer.messages)

    async def test_verification_lifetime_and_resend_cooldown(self):
        email = "resend@gmail.com"
        register = await self.client.post("/v1/auth/register", json={"email": email, "password": TEST_PASSWORD})
        self.assertEqual(register.status_code, 202)
        original = self.mailer.messages[-1][2]
        seconds = await self.db.run(lambda c: c.execute(
            "SELECT EXTRACT(EPOCH FROM expires_at-created_at) AS seconds FROM account_tokens WHERE token_hash=%s",
            (digest(original),),
        ).fetchone()["seconds"])
        self.assertEqual(seconds, 600)
        request = "/v1/auth/verification/request"
        for address in (email, "unknown@gmail.com"):
            first = await self.client.post(request, json={"email": address})
            blocked = await self.client.post(request, json={"email": address})
            self.assertEqual(first.status_code, 202)
            self.assertEqual((blocked.status_code, blocked.json()["error"]["code"]), (429, "email_cooldown"))
            self.assertTrue(1 <= int(blocked.headers["retry-after"]) <= 60)
        self.assertEqual(len(self.mailer.messages), 1)

        def move_past_cooldown(connection):
            connection.execute("UPDATE account_tokens SET created_at=now()-interval '61 seconds' WHERE token_hash=%s",
                               (digest(original),))
            connection.execute("UPDATE auth_rate_limits SET window_start=now()-interval '61 seconds' WHERE key_hash=%s",
                               (digest("mail.cooldown.verify\0" + email),))

        await self.db.run(move_past_cooldown)
        replies = await asyncio.gather(*(self.client.post(request, json={"email": email}) for _ in range(2)))
        self.assertEqual(sorted(reply.status_code for reply in replies), [202, 429])
        self.assertEqual(len(self.mailer.messages), 2)
        confirm = "/v1/auth/verification/confirm"
        self.assertEqual((await self.client.post(confirm, json={"token": original})).status_code, 400)
        replacement = self.mailer.messages[-1][2]
        await self.db.run(lambda c: c.execute(
            """UPDATE account_tokens SET created_at=now()-interval '601 seconds',
            expires_at=now()+interval '1 day' WHERE token_hash=%s""", (digest(replacement),),
        ))
        self.assertEqual((await self.client.post(confirm, json={"token": replacement})).status_code, 400)

    async def test_remembered_login_restores_access_and_honors_expiry_and_logout(self):
        await self.account()
        login = {"email": "alice@gmail.com", "password": TEST_PASSWORD, "remember_me": True}
        endpoint = "/v1/auth/remember"
        token = (await self.client.post("/v1/auth/login", json=login)).json()
        self.assertEqual(token["remember_expires_in"], 30 * 24 * 60 * 60)
        secret = token["remember_token"]
        rows = await self.db.run(lambda c: c.execute(
            "SELECT token_hash,kind FROM auth_tokens WHERE session_id=%s", (token["session_id"],),
        ).fetchall())
        self.assertIn({"token_hash": digest(secret), "kind": "remember"}, rows)
        restored = (await self.client.post(endpoint, json={"token": secret})).json()
        self.assertNotIn("remember_token", restored)
        self.assertNotIn("refresh_token", restored)
        old = {"Authorization": "Bearer " + token["access_token"]}
        active = {"Authorization": "Bearer " + restored["access_token"]}
        self.assertEqual((await self.client.get("/v1/me", headers=old)).status_code, 401)
        self.assertEqual((await self.client.get("/v1/me", headers=active)).status_code, 200)
        self.assertEqual((await self.client.post("/v1/auth/logout", headers=active)).status_code, 204)
        self.assertEqual((await self.client.post(endpoint, json={"token": secret})).status_code, 401)

        expired = (await self.client.post("/v1/auth/login", json=login)).json()["remember_token"]
        await self.db.run(lambda c: c.execute(
            "UPDATE auth_tokens SET expires_at=now()-interval '1 second' WHERE token_hash=%s", (digest(expired),),
        ))
        self.assertEqual((await self.client.post(endpoint, json={"token": expired})).status_code, 401)

    async def test_existing_account_outside_registration_domains_can_log_in_and_reset(self):
        email, password = "existing-user@example.org", "correct horse battery staple"
        encoded = self.app.state.auth.passwords.hasher.hash(password)
        await self.db.run(lambda c: c.execute(
            "INSERT INTO users(email,password_hash,verified_at) VALUES (%s,%s,now())", (email, encoded),
        ))
        login = await self.client.post("/v1/auth/login", json={"email": email, "password": password})
        self.assertEqual(login.status_code, 200, login.text)
        reset = await self.client.post("/v1/auth/password/request", json={"email": email})
        self.assertEqual(reset.status_code, 202, reset.text)
        self.assertEqual(self.mailer.messages[-1][:2], (email, "reset"))

    async def test_duplicate_registration_and_unknown_login_do_not_enumerate(self):
        await self.account()
        response = await self.client.post("/v1/auth/register", json={"email": "alice@gmail.com", "password": "another1 password"})
        self.assertEqual(response.status_code, 202)
        one = await self.client.post("/v1/auth/login", json={"email": "alice@gmail.com", "password": "wrong password"})
        two = await self.client.post("/v1/auth/login", json={"email": "unknown@example.com", "password": "wrong password"})
        self.assertEqual((one.status_code, one.json()), (two.status_code, two.json()))

    async def test_refresh_rotation_replay_revokes_entire_session(self):
        headers, tokens = await self.account()
        response = await self.client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        self.assertEqual(response.status_code, 200, response.text)
        new = response.json()
        self.assertEqual((await self.client.get("/v1/me", headers=headers)).status_code, 401)
        new_headers = {"Authorization": "Bearer " + new["access_token"]}
        self.assertEqual((await self.client.get("/v1/me", headers=new_headers)).status_code, 200)
        self.assertEqual((await self.client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})).status_code, 401)
        self.assertEqual((await self.client.get("/v1/me", headers=new_headers)).status_code, 401)

    async def test_simultaneous_refresh_detects_replay(self):
        _, tokens = await self.account()
        replies = await asyncio.gather(*(self.client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}) for _ in range(2)))
        self.assertEqual(sorted(reply.status_code for reply in replies), [200, 401])
        winner = next(reply.json() for reply in replies if reply.status_code == 200)
        self.assertEqual((await self.client.get("/v1/me", headers={"Authorization": "Bearer " + winner["access_token"]})).status_code, 401)

    async def test_logout_and_cross_user_session_revoke(self):
        alice, alice_tokens = await self.account()
        bob, _ = await self.account("bob")
        self.assertEqual((await self.client.delete("/v1/auth/sessions/" + alice_tokens["session_id"], headers=bob)).status_code, 404)
        self.assertEqual((await self.client.post("/v1/auth/logout", headers=alice)).status_code, 204)
        self.assertEqual((await self.client.get("/v1/me", headers=alice)).status_code, 401)
        self.assertEqual((await self.client.post("/v1/auth/refresh", json={"refresh_token": alice_tokens["refresh_token"]})).status_code, 401)

    async def test_password_reset_single_use_and_revokes_sessions(self):
        headers, _ = await self.account()
        response = await self.client.post("/v1/auth/password/request", json={"email": "alice@gmail.com"})
        self.assertEqual(response.status_code, 202)
        secret = self.mailer.messages[-1][2]
        weak = await self.client.post("/v1/auth/password/reset", json={"token": secret, "new_password": "letters only"})
        self.assertEqual((weak.status_code, weak.json()["error"]["code"]), (422, "weak_password"))
        data = {"token": secret, "new_password": "new1pass"}
        self.assertEqual((await self.client.post("/v1/auth/password/reset", json=data)).status_code, 204)
        self.assertEqual((await self.client.post("/v1/auth/password/reset", json=data)).status_code, 400)
        self.assertEqual((await self.client.get("/v1/me", headers=headers)).status_code, 401)
        result = await self.client.post("/v1/auth/login", json={"email": "alice@gmail.com", "password": data["new_password"]})
        self.assertEqual(result.status_code, 200, result.text)

    async def test_password_change_requires_session_bound_email_and_revokes_all_devices(self):
        headers, _ = await self.account()
        second = (await self.client.post("/v1/auth/login", json={
            "email": "alice@gmail.com", "password": TEST_PASSWORD, "remember_me": True,
        })).json()
        other_session = {"Authorization": "Bearer " + second["access_token"]}
        change, request = "/v1/auth/password/change", "/v1/auth/password/change/request"
        data = {"old_password": TEST_PASSWORD, "new_password": "new1pass"}
        self.assertEqual((await self.client.post(change, json=data, headers=headers)).status_code, 422)
        self.assertEqual((await self.client.post(request, json={"old_password": TEST_PASSWORD})).status_code, 401)
        wrong = await self.client.post(request, json={"old_password": "incorrect"}, headers=headers)
        self.assertEqual((wrong.status_code, wrong.json()["error"]["code"]), (403, "incorrect_password"))
        issue = {"old_password": TEST_PASSWORD}
        replies = await asyncio.gather(*(self.client.post(request, json=issue, headers=headers) for _ in range(2)))
        self.assertEqual(sorted(reply.status_code for reply in replies), [202, 429])
        blocked = next(reply for reply in replies if reply.status_code == 429)
        self.assertTrue(1 <= int(blocked.headers["retry-after"]) <= 60)
        self.assertEqual([purpose for _, purpose, _ in self.mailer.messages], ["verify", "change"])
        secret = self.mailer.messages[-1][2]
        lifetime = await self.db.run(lambda c: c.execute(
            "SELECT EXTRACT(EPOCH FROM expires_at-created_at) AS seconds FROM account_tokens WHERE token_hash=%s",
            (digest(secret),),
        ).fetchone()["seconds"])
        self.assertEqual(lifetime, 600)
        cross_session = await self.client.post(change, json={**data, "token": secret}, headers=other_session)
        self.assertEqual((cross_session.status_code, cross_session.json()["error"]["code"]), (400, "invalid_account_token"))
        wrong_kind = await self.client.post(change, json={**data, "token": self.mailer.messages[0][2]}, headers=headers)
        self.assertEqual(wrong_kind.status_code, 400)
        weak = await self.client.post(change, json={**data, "token": secret, "new_password": "UPPERCASE1"}, headers=headers)
        self.assertEqual((weak.status_code, weak.json()["error"]["code"]), (422, "weak_password"))
        self.assertEqual((await self.client.post(change, json={**data, "token": secret}, headers=headers)).status_code, 204)
        for session in (headers, other_session):
            self.assertEqual((await self.client.get("/v1/me", headers=session)).status_code, 401)
        self.assertEqual((await self.client.post("/v1/auth/remember", json={"token": second["remember_token"]})).status_code, 401)
        self.assertEqual((await self.client.post("/v1/auth/login", json={
            "email": "alice@gmail.com", "password": data["new_password"],
        })).status_code, 200)

    async def test_access_expiry_and_revocation_survive_new_auth_service(self):
        headers, tokens = await self.account()
        from main_agent.cloud.auth import AuthService
        another_process = AuthService(self.db, self.config, self.mailer)
        await another_process.authenticate(tokens["access_token"])
        await self.db.run(lambda c: c.execute("UPDATE auth_tokens SET expires_at=now()-interval '1 second' WHERE token_hash=%s", (digest(tokens["access_token"]),)))
        with self.assertRaises(ServiceError) as caught:
            await another_process.authenticate(tokens["access_token"])
        self.assertEqual(caught.exception.status, 401)

    async def test_authentication_input_and_database_rate_limit(self):
        self.assertEqual((await self.client.get("/v1/instances")).status_code, 401)
        for _ in range(12):
            response = await self.client.post("/v1/auth/login", json={"email": "nobody@example.com", "password": "wrong"})
            self.assertEqual(response.status_code, 401)
        response = await self.client.post("/v1/auth/login", json={"email": "nobody@example.com", "password": "wrong"})
        self.assertEqual(response.status_code, 429)
        response = await self.client.post("/v1/auth/login", content='{"email":"x@example.com","password":"secret-value","email":"y@example.com"}')
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("secret-value", response.text)

    async def test_cross_user_instance_conversation_and_run_isolation(self):
        alice, _ = await self.account()
        bob, _ = await self.account("bob")
        instance, conversation = await self.conversation(alice)
        run = (await self.submit(alice, conversation)).json()
        for method, path, payload in (
            ("GET", f"/v1/instances/{instance['id']}", None),
            ("PATCH", f"/v1/instances/{instance['id']}", {"name": "侵入"}),
            ("PATCH", f"/v1/instances/{instance['id']}", {"is_default": True}),
            ("DELETE", f"/v1/instances/{instance['id']}", None),
            ("PATCH", f"/v1/conversations/{conversation['id']}", {"title": "侵入"}),
            ("DELETE", f"/v1/conversations/{conversation['id']}", None),
            ("GET", f"/v1/conversations/{conversation['id']}/messages", None),
            ("GET", f"/v1/runs/{run['id']}", None),
            ("POST", f"/v1/runs/{run['id']}/cancel", None),
        ):
            response = await self.client.request(method, path, json=payload, headers=bob)
            self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual((await self.submit(bob, conversation)).status_code, 404)
        result = await self.client.get("/v1/instances", headers=bob)
        instances = result.json()["items"]
        self.assertEqual(len(instances), 1)
        self.assertTrue(instances[0]["is_default"])
        self.assertNotEqual(instances[0]["id"], instance["id"])
        self.assertEqual(instances[0]["user_id"], (await self.client.get("/v1/me", headers=bob)).json()["id"])

    async def test_instance_limit_is_atomic_and_pause_blocks_new_work(self):
        headers, _ = await self.account()
        replies = await asyncio.gather(*(self.client.post("/v1/instances", json={"name": str(i)}, headers=headers) for i in range(5)))
        self.assertEqual(sum(reply.status_code == 201 for reply in replies), 2)
        instances = (await self.client.get("/v1/instances", headers=headers)).json()["items"]
        self.assertEqual(len(instances), 3)
        self.assertTrue(instances[0]["is_default"])
        instance = next(reply.json() for reply in replies if reply.status_code == 201)
        conversation = (await self.client.post(f"/v1/instances/{instance['id']}/conversations", json={}, headers=headers)).json()
        await self.client.patch(f"/v1/instances/{instance['id']}", json={"status": "paused"}, headers=headers)
        self.assertEqual((await self.submit(headers, conversation)).status_code, 409)

    async def test_pausing_an_instance_stops_claims_and_resume_keeps_queued_runs(self):
        headers, _ = await self.account()
        instance, conversation = await self.conversation(headers)
        queued = (await self.submit(headers, conversation)).json()
        with psycopg.connect(self.app_dsn) as editing:
            editing.execute("UPDATE agent_instances SET status='paused' WHERE id=%s", (instance["id"],))
            self.assertIsNone(await self.store.claim())
        self.assertIsNone(await self.store.claim())
        updated = await self.client.patch(f"/v1/instances/{instance['id']}",
                                          json={"name": "继续使用", "status": "active"}, headers=headers)
        self.assertEqual((updated.json()["name"], updated.json()["status"]), ("继续使用", "active"))
        self.assertEqual(str((await self.store.claim())["id"]), queued["id"])

    async def test_conversation_management_searches_renames_and_removes_queued_data(self):
        headers, _ = await self.account()
        instance, original = await self.conversation(headers)
        collection = f"/v1/instances/{instance['id']}/conversations"
        path = f"/v1/conversations/{original['id']}"
        self.assertEqual((await self.client.patch(path, headers=headers, json={})).status_code, 422)
        renamed = await self.client.patch(path, headers=headers, json={"title": "  研讨会 ABC%  "})
        self.assertEqual((renamed.status_code, renamed.json()["title"]), (200, "研讨会 ABC%"))
        for title in ("研讨会 abc", "研讨会 abc% 二", "学习小组"):
            self.assertEqual((await self.client.post(collection, headers=headers, json={"title": title})).status_code, 201)
        first = (await self.client.get(collection, params={"q": "aBc%", "limit": 1}, headers=headers)).json()
        second = (await self.client.get(
            collection, params={"q": "aBc%", "limit": 1, "cursor": first["next_cursor"]}, headers=headers,
        )).json()
        self.assertEqual([item["title"] for item in first["items"] + second["items"]], ["研讨会 abc% 二", "研讨会 ABC%"])
        self.assertIsNone(second["next_cursor"])

        run = (await self.submit(headers, original)).json()
        self.assertEqual((await self.client.delete(path, headers=headers)).status_code, 204)
        for removed in (path + "/messages", f"/v1/runs/{run['id']}"):
            self.assertEqual((await self.client.get(removed, headers=headers)).status_code, 404)
        self.assertIsNone(await self.store.claim())
        self.assertEqual((await self.client.get(collection, params={"q": "abc%"}, headers=headers)).json()["items"], first["items"])

    async def test_running_task_protects_its_conversation_and_instance_until_finished(self):
        headers, _ = await self.account()
        instance, conversation = await self.conversation(headers)
        run = (await self.submit(headers, conversation)).json()
        claimed = await self.store.claim()
        self.assertEqual(str(claimed["id"]), run["id"])
        instance_path = f"/v1/instances/{instance['id']}"
        for path in (f"/v1/conversations/{conversation['id']}", instance_path):
            denied = await self.client.delete(path, headers=headers)
            self.assertEqual((denied.status_code, denied.json()["error"]["code"]), (409, "run_in_progress"))
        self.assertTrue(await self.store.finish(claimed, RunOutput("完成", "completed", artifacts=(
            {"kind": "test", "payload": {}},))))
        self.assertEqual((await self.client.delete(instance_path, headers=headers)).status_code, 204)
        self.assertEqual((await self.client.get(f"/v1/runs/{run['id']}", headers=headers)).status_code, 404)
        self.assertEqual(await self.db.run(lambda c: c.execute(
            "SELECT count(*) AS n FROM run_artifacts WHERE run_id=%s", (run["id"],),
        ).fetchone()["n"]), 0)

    async def test_delete_and_worker_claim_serialize(self):
        headers, _ = await self.account()
        for collection in ("conversations", "instances"):
            instance, conversation = await self.conversation(headers)
            run = (await self.submit(headers, conversation)).json()
            record = conversation if collection == "conversations" else instance
            path = f"/v1/{collection}/{record['id']}"
            deleted, claimed = await asyncio.gather(
                self.client.delete(path, headers=headers), self.store.claim(),
            )
            if claimed is None:
                self.assertEqual(deleted.status_code, 204)
            else:
                self.assertEqual(str(claimed["id"]), run["id"])
                self.assertEqual((deleted.status_code, deleted.json()["error"]["code"]), (409, "run_in_progress"))
                self.assertTrue(await self.store.finish(claimed, RunOutput("完成", "completed")))
                self.assertEqual((await self.client.delete(path, headers=headers)).status_code, 204)
            self.assertEqual((await self.client.get(f"/v1/runs/{run['id']}", headers=headers)).status_code, 404)

    async def test_default_switch_and_deletion_keep_one_default(self):
        headers, _ = await self.account()
        first, _ = await self.conversation(headers, "项目一")
        second, _ = await self.conversation(headers, "项目二")
        path = lambda instance: f"/v1/instances/{instance['id']}"

        invalid = await self.client.patch(path(first), headers=headers, json={"is_default": False})
        self.assertEqual(invalid.status_code, 422)
        replies = await asyncio.gather(*(self.client.patch(
            path(item), headers=headers, json={"is_default": True},
        ) for item in (first, second)))
        self.assertEqual([reply.status_code for reply in replies], [200, 200])
        items = (await self.client.get("/v1/instances", headers=headers)).json()["items"]
        self.assertEqual(sum(item["is_default"] for item in items), 1)
        self.assertIn(items[0]["id"], (first["id"], second["id"]))
        for item in items:
            self.assertEqual((await self.client.delete(path(item), headers=headers)).status_code, 204)
            remaining = (await self.client.get("/v1/instances", headers=headers)).json()["items"]
            self.assertEqual(sum(instance["is_default"] for instance in remaining), 1)
        self.assertEqual(len(remaining), 1)
        self.assertNotIn(remaining[0]["id"], {item["id"] for item in items})
        self.assertEqual((remaining[0]["name"], remaining[0]["status"]), ("默认实例", "active"))

    async def test_default_migration_backfills_without_moving_existing_conversations(self):
        name, dsn, _ = await asyncio.to_thread(self.pg.create_database)
        prior = self.pg.directory / name
        prior.mkdir()
        for path in MIGRATIONS.glob("00[12]_*.sql"):
            shutil.copyfile(path, prior / path.name)
        try:
            await asyncio.to_thread(migrate, dsn, prior)
            with psycopg.connect(dsn) as connection:
                alice = connection.execute("INSERT INTO users(email) VALUES ('alice@example.com') RETURNING id").fetchone()[0]
                bob = connection.execute("INSERT INTO users(email) VALUES ('bob@example.com') RETURNING id").fetchone()[0]
                original = connection.execute(
                    "INSERT INTO agent_instances(user_id,name,status,created_at) VALUES (%s,'原空间','paused',now()-interval '1 day') RETURNING id",
                    (alice,)).fetchone()[0]
                other = connection.execute("INSERT INTO agent_instances(user_id,name) VALUES (%s,'另一个空间') RETURNING id",
                                           (alice,)).fetchone()[0]
                conversation = connection.execute("INSERT INTO conversations(user_id,instance_id) VALUES (%s,%s) RETURNING id",
                                                  (alice, other)).fetchone()[0]
            self.assertEqual(await asyncio.to_thread(migrate, dsn), [
                "003_user_default_instance.sql", "004_email_password_challenge.sql", "005_user_roles.sql",
                "006_minta_operations.sql", "007_chat_turn_actions.sql", "008_chat_effect_tracking_default.sql",
                "009_campus_profiles.sql", "010_campus_access_rules.sql", "011_campus_photos.sql",
                "012_library_loan_periods.sql", "013_campus_photo_moderation.sql", "014_gym_slots.sql",
                "015_calendar_subscriptions.sql", "016_microsoft_todo.sql", "017_personal_schedule.sql",
                "018_wechat_following.sql",
            ])
            self.assertEqual(await asyncio.to_thread(migrate, dsn), [])
            with psycopg.connect(dsn) as connection:
                rows = connection.execute("SELECT id,name,status,is_default FROM agent_instances WHERE user_id=%s ORDER BY is_default DESC",
                                          (alice,)).fetchall()
                self.assertEqual(rows, [(original, "原空间", "paused", True), (other, "另一个空间", "active", False)])
                self.assertEqual(connection.execute("SELECT instance_id FROM conversations WHERE id=%s", (conversation,)).fetchone()[0], other)
                self.assertEqual(connection.execute("SELECT name,is_default FROM agent_instances WHERE user_id=%s",
                                                    (bob,)).fetchall(), [("默认实例", True)])
                self.assertEqual(connection.execute("SELECT role,academic_title FROM users ORDER BY email").fetchall(),
                                 [("student", None), ("student", None)])
        finally:
            await asyncio.to_thread(self.pg.drop_database, name)

    async def test_local_readiness_without_smtp_password_preserves_email_verification(self):
        config = replace(self.config, smtp_host="smtp.qq.com", smtp_from="sender@qq.com",
                         smtp_user="sender@qq.com", smtp_auth="password")
        app = create_app(config, runner=self.runner)
        async with app.router.lifespan_context(app), httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test",
        ) as client:
            self.assertEqual((await client.get("/ready")).status_code, 200)
            response = await client.post("/v1/auth/register", json={
                "email": "not-created@qq.com", "password": TEST_PASSWORD,
            })
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (503, "mail_unavailable"))
            rejected = await client.post("/v1/auth/register", json={
                "email": "not-created@example.com", "password": TEST_PASSWORD,
            })
            self.assertEqual((rejected.status_code, rejected.json()["error"]["code"]),
                             (422, "email_provider_not_supported"))
        self.assertEqual(await self.db.run(lambda c: c.execute("SELECT count(*) AS n FROM users").fetchone()["n"]), 0)

    async def test_idempotent_turn_and_atomic_active_run_conflict(self):
        headers, _ = await self.account()
        _, conversation = await self.conversation(headers)
        key = str(uuid4())
        replies = await asyncio.gather(*(self.submit(headers, conversation, key=key) for _ in range(4)))
        self.assertTrue(all(reply.status_code == 202 for reply in replies), [reply.text for reply in replies])
        self.assertEqual(len({reply.json()["id"] for reply in replies}), 1)
        self.assertEqual((await self.submit(headers, conversation, query="不同内容", key=key)).status_code, 409)
        self.assertEqual((await self.submit(headers, conversation)).status_code, 409)
        messages = (await self.client.get(f"/v1/conversations/{conversation['id']}/messages", headers=headers)).json()["items"]
        self.assertEqual(len(messages), 1)

    async def test_claim_is_exclusive_and_completion_is_atomic(self):
        headers, _ = await self.account()
        _, conversation = await self.conversation(headers)
        run = (await self.submit(headers, conversation, query="我的问题")).json()
        claims = await asyncio.gather(self.store.claim(), self.store.claim())
        self.assertEqual(sum(item is not None for item in claims), 1)
        claim = next(item for item in claims if item)
        await self.worker.process(claim)
        result = (await self.client.get(f"/v1/runs/{run['id']}", headers=headers)).json()
        self.assertEqual((result["status"], result["result_status"]), ("succeeded", "completed"))
        self.assertFalse(await self.store.finish(claim, RunOutput("重复答案", "completed")))
        messages = (await self.client.get(f"/v1/conversations/{conversation['id']}/messages", headers=headers)).json()["items"]
        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
        artifact = await self.db.run(lambda c: c.execute("SELECT count(*) AS n FROM run_artifacts").fetchone())
        self.assertEqual(artifact["n"], 1)

    async def test_active_agents_follow_running_steps(self):
        headers, _ = await self.account()
        _, conversation = await self.conversation(headers)
        targets = (AgentId.CAMPUS_INTRODUCTION, AgentId.INFORMATION_SEARCH)
        started = {target: asyncio.Event() for target in targets}
        release_agents, composing, publish = (asyncio.Event() for _ in range(3))

        class WaitingBackend:
            async def run(self, task):
                started[task.profile.id].set()
                await release_agents.wait()
                return AgentReply(Status.COMPLETED, "测试资料")

        cluster = build_campus_cluster(backends={target: WaitingBackend() for target in targets})
        runner = CampusRunner(lambda _: cluster)

        class WaitingRunner:
            async def run(self, context):
                output = await runner.run(context)
                composing.set()
                await publish.wait()
                return output

        run = (await self.submit(headers, conversation, query="查询校园资料", targets=list(targets))).json()
        url = f"/v1/runs/{run['id']}"
        self.assertEqual((await self.client.get(url, headers=headers)).json()["active_agents"], [])
        worker = WorkerPool(self.store, WaitingRunner(), self.config)
        task = asyncio.create_task(worker.process(await self.store.claim()))
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 5)
            active = (await self.client.get(url, headers=headers)).json()
            self.assertEqual(active["active_agents"], [target.value for target in targets])
            self.assertEqual(active["steps"], [])
            release_agents.set()
            await asyncio.wait_for(composing.wait(), 5)
            active = (await self.client.get(url, headers=headers)).json()
            self.assertEqual((active["status"], active["active_agents"]), ("running", []))
        finally:
            release_agents.set()
            publish.set()
            await task

        completed = (await self.client.get(url, headers=headers)).json()
        self.assertEqual((completed["status"], completed["active_agents"]), ("succeeded", []))
        self.assertEqual([step["agent_id"] for step in completed["steps"]], [target.value for target in targets])

    async def test_only_latest_turn_can_retry_or_withdraw_even_with_an_old_plan(self):
        headers, _ = await self.account()
        instance, conversation = await self.conversation(headers)
        first = (await self.submit(headers, conversation, query="第一问")).json()
        await self.worker.process(await self.store.claim())
        withdrawal = f"/v1/runs/{first['id']}/withdrawal"
        self.assertEqual((await self.client.get(withdrawal, headers=headers)).status_code, 200)
        second = (await self.submit(headers, conversation, query="第二问")).json()
        await self.client.post(f"/v1/runs/{second['id']}/cancel", headers=headers)

        payload = {"attempt": 1}
        retry_headers = {**headers, "Idempotency-Key": str(uuid4())}
        for response in (
            await self.client.get(withdrawal, headers=headers),
            await self.client.post(withdrawal, headers=headers, json={"attempt": 1, "approvals": []}),
            await self.client.post(f"/v1/runs/{first['id']}/retry", headers=retry_headers, json=payload),
        ):
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (409, "turn_not_latest"))

        retry = f"/v1/runs/{second['id']}/retry"
        for _ in range(2):
            response = await self.client.post(retry, headers=retry_headers, json=payload)
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual((response.json()["id"], response.json()["attempt"]), (second["id"], 2))
        chats = await self.client.get(f"/v1/instances/{instance['id']}/conversations", headers=headers)
        self.assertEqual(chats.json()["items"][0]["active_run_id"], second["id"])
        await self.worker.process(await self.store.claim())
        self.assertEqual([message["content"] for message in self.runner.contexts[-1].history], ["第一问", "答复：第一问"])

        messages = f"/v1/conversations/{conversation['id']}/messages"
        latest = (await self.client.get(messages, headers=headers, params={"limit": 2})).json()
        earlier = (await self.client.get(messages, headers=headers,
                                         params={"limit": 2, "cursor": latest["next_cursor"]})).json()
        self.assertEqual([row["content"] for row in earlier["items"] + latest["items"]],
                         ["第一问", "答复：第一问", "第二问", "答复：第二问"])

    async def test_withdraw_running_turn_fences_worker_and_removes_turn_from_history(self):
        headers, _ = await self.account()
        other, _ = await self.account("bob")
        _, conversation = await self.conversation(headers)
        first = (await self.submit(headers, conversation, query="这条会撤回")).json()
        claim = await self.store.claim()
        path = f"/v1/runs/{first['id']}/withdrawal"
        plan = await self.client.get(path, headers=headers)
        self.assertEqual(plan.json(), {"attempt": 1, "running": True, "operations": []})
        confirmation = {"attempt": 1, "approvals": []}
        self.assertEqual((await self.client.post(path, headers=other, json=confirmation)).status_code, 404)
        self.assertEqual(await self.store.heartbeat(claim), "ok")
        self.assertEqual((await self.client.post(path, headers=headers, json=confirmation)).status_code, 204)
        self.assertEqual(await self.store.heartbeat(claim), "lost")
        self.assertFalse(await self.store.finish(claim, RunOutput("迟到答案", "completed")))
        messages = f"/v1/conversations/{conversation['id']}/messages"
        self.assertEqual((await self.client.get(messages, headers=headers)).json()["items"], [])

        self.assertEqual((await self.submit(headers, conversation, query="新问题")).status_code, 202)
        await self.worker.process(await self.store.claim())
        self.assertEqual(self.runner.contexts[-1].history, ())

    async def test_histories_and_execution_context_do_not_cross_users_or_instances(self):
        alice, _ = await self.account()
        bob, _ = await self.account("bob")
        instance, first = await self.conversation(alice, "A")
        response = await self.client.post(f"/v1/instances/{instance['id']}/conversations", json={}, headers=alice)
        self.assertEqual(response.status_code, 201)
        second = response.json()
        _, third = await self.conversation(alice, "另一实例")
        _, fourth = await self.conversation(bob, "另一用户")
        for headers, conversation, text in (
            (alice, first, "A的秘密"), (alice, second, "B的秘密"),
            (alice, third, "C的秘密"), (bob, fourth, "D的秘密"),
        ):
            await self.submit(headers, conversation, query=text)
            await self.worker.process(await self.store.claim())
        self.assertTrue(all(not context.history for context in self.runner.contexts))

        await asyncio.gather(
            self.submit(alice, first, query="继续A"), self.submit(alice, second, query="继续B"),
        )
        claims = await asyncio.gather(self.store.claim(), self.store.claim())
        claimed = {str(run["conversation_id"]): run for run in claims}
        altered = claimed[first["id"]] | {
            "conversation_id": claimed[second["id"]]["conversation_id"],
            "input": claimed[second["id"]]["input"],
        }
        context = await self.store.execution_context(altered)
        self.assertEqual(str(context["conversation_id"]), first["id"])
        self.assertEqual(context["query"], "继续A")
        self.assertFalse(await self.store.finish(altered, RunOutput("写到另一会话", "completed")))

        await asyncio.gather(*(self.worker.process(run) for run in claims))
        histories = {context.query: [item["content"] for item in context.history]
                     for context in self.runner.contexts[-2:]}
        self.assertEqual(histories, {
            "继续A": ["A的秘密", "答复：A的秘密"], "继续B": ["B的秘密", "答复：B的秘密"],
        })

    async def test_expired_lease_fences_old_worker_and_never_replays(self):
        headers, _ = await self.account()
        _, conversation = await self.conversation(headers)
        run = (await self.submit(headers, conversation)).json()
        claim = await self.store.claim()
        await self.store.record_step_activity(claim, "search", AgentId.INFORMATION_SEARCH.value, True)
        await self.db.run(lambda c: c.execute("UPDATE runs SET lease_expires_at=now()-interval '1 second' WHERE id=%s", (claim["id"],)))
        url = f"/v1/runs/{run['id']}"
        self.assertEqual((await self.client.get(url, headers=headers)).json()["active_agents"], [])
        self.assertIsNone(await self.store.claim())
        self.assertFalse(await self.store.finish(claim, RunOutput("迟到答案", "completed")))
        result = (await self.client.get(url, headers=headers)).json()
        self.assertEqual((result["status"], result["result_status"]), ("failed", "uncertain"))
        self.assertEqual(result["steps"], [])
        self.assertEqual(len(self.runner.contexts), 0)

    async def test_queued_and_running_cancellation(self):
        headers, _ = await self.account()
        _, conversation = await self.conversation(headers)
        run = (await self.submit(headers, conversation)).json()
        self.assertEqual((await self.client.post(f"/v1/runs/{run['id']}/cancel", headers=headers)).json()["status"], "cancelled")
        self.assertIsNone(await self.store.claim())
        run = (await self.submit(headers, conversation)).json()
        claim = await self.store.claim()
        await self.client.post(f"/v1/runs/{run['id']}/cancel", headers=headers)
        await self.worker.process(claim)
        result = (await self.client.get(f"/v1/runs/{run['id']}", headers=headers)).json()
        self.assertEqual(result["status"], "cancelled")
        rows = await self.db.run(lambda c: c.execute("SELECT count(*) AS n FROM messages WHERE role='assistant'").fetchone())
        self.assertEqual(rows["n"], 0)
        self.assertEqual(self.runner.contexts, [])

    async def test_user_run_limit_is_shared_across_conversations(self):
        headers, _ = await self.account()
        self.store.settings = replace(self.config, max_active_runs_per_user=4)
        self.assertEqual(self.store.settings.max_active_runs_per_user, 3)
        instance, finished = await self.conversation(headers)
        other_instance, first = await self.conversation(headers, "另一实例")
        ended = (await self.submit(headers, finished)).json()
        await self.client.post(f"/v1/runs/{ended['id']}/cancel", headers=headers)

        conversations = [first]
        for item in (instance, other_instance, instance):
            conversations.append((await self.client.post(
                f"/v1/instances/{item['id']}/conversations", json={}, headers=headers)).json())
        replies = await asyncio.gather(*(self.submit(headers, conversation) for conversation in conversations))
        self.assertEqual(sorted(reply.status_code for reply in replies), [202, 202, 202, 429])
        rejected = next(reply for reply in replies if reply.status_code == 429)
        self.assertEqual(rejected.json()["error"]["code"], "run_limit")
        accepted = next(reply for reply in replies if reply.status_code == 202).json()
        retry_path = f"/v1/runs/{ended['id']}/retry"
        retry_headers = {**headers, "Idempotency-Key": str(uuid4())}
        retry = await self.client.post(retry_path, json={"attempt": 1}, headers=retry_headers)
        self.assertEqual(retry.status_code, 429)

        other_headers, _ = await self.account("bob")
        _, independent = await self.conversation(other_headers)
        self.assertEqual((await self.submit(other_headers, independent)).status_code, 202)
        await self.client.post(f"/v1/runs/{accepted['id']}/cancel", headers=headers)
        self.assertEqual((await self.client.post(retry_path, json={"attempt": 1}, headers=retry_headers)).status_code, 202)

    async def test_running_cancel_interrupts_runner_and_leaves_no_final_answer(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class WaitingRunner:
            async def run(self, context):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        headers, _ = await self.account()
        _, conversation = await self.conversation(headers)
        run = (await self.submit(headers, conversation)).json()
        claim = await self.store.claim()
        task = asyncio.create_task(WorkerPool(self.store, WaitingRunner(), self.config).process(claim))
        await asyncio.wait_for(entered.wait(), 3)
        await self.client.post(f"/v1/runs/{run['id']}/cancel", headers=headers)
        await asyncio.wait_for(task, 5)
        self.assertTrue(cancelled.is_set())
        result = (await self.client.get(f"/v1/runs/{run['id']}", headers=headers)).json()
        self.assertEqual(result["status"], "cancelled")

    async def test_runner_timeout_is_persisted(self):
        class WaitingRunner:
            async def run(self, context):
                await asyncio.Event().wait()

        headers, _ = await self.account()
        _, conversation = await self.conversation(headers)
        run = (await self.submit(headers, conversation)).json()
        worker = WorkerPool(self.store, WaitingRunner(), replace(self.config, run_timeout=1))
        await asyncio.wait_for(worker.process(await self.store.claim()), 4)
        result = (await self.client.get(f"/v1/runs/{run['id']}", headers=headers)).json()
        self.assertEqual((result["status"], result["error_message"]), ("failed", "execution_timeout"))

    async def test_default_cluster_persists_configuration_outcome_and_four_steps(self):
        headers, _ = await self.account()
        _, conversation = await self.conversation(headers)
        from main_agent.campus_cluster import AgentId
        run = (await self.submit(headers, conversation, targets=[agent.value for agent in AgentId])).json()
        worker = WorkerPool(self.store, CampusRunner(), self.config)
        await worker.process(await self.store.claim())
        result = (await self.client.get(f"/v1/runs/{run['id']}", headers=headers)).json()
        self.assertEqual(result["result_status"], "needs_configuration")
        self.assertEqual(len(result["steps"]), 4)

    async def test_untrusted_identity_and_system_messages_are_rejected(self):
        headers, _ = await self.account()
        _, conversation = await self.conversation(headers)
        response = await self.client.post(f"/v1/conversations/{conversation['id']}/runs", json={
            "query": "hi", "user_id": str(uuid4()), "role": "system", "history": []},
            headers={**headers, "Idempotency-Key": str(uuid4())})
        self.assertEqual(response.status_code, 422)
        response = await self.client.post("/v1/instances", json={"name": "x", "tools": ["shell"]}, headers=headers)
        self.assertEqual(response.status_code, 422)

    async def test_migration_is_repeatable_and_runtime_role_cannot_perform_ddl(self):
        self.assertEqual(await asyncio.to_thread(migrate, self.admin_dsn), [])
        with self.assertRaises(ServiceError):
            await self.db.run(lambda c: c.execute("CREATE TABLE should_not_exist(id integer)"))

    async def test_upgrade_preserves_existing_conversation_and_messages(self):
        name, owner_dsn, _ = await asyncio.to_thread(self.pg.create_database)

        def upgrade():
            baseline = (self.pg.project / "DataBase/PostgreSQL/schema.sql").read_text(encoding="utf-8")
            baseline = baseline.replace("ON DATABASE cloud_agent", "ON DATABASE " + name)
            with psycopg.connect(owner_dsn, autocommit=True) as connection:
                connection.execute(baseline, prepare=False)
                user = connection.execute("INSERT INTO users(email,password_hash) VALUES ('old@example.com','old-hash') RETURNING id").fetchone()[0]
                conversation = connection.execute("INSERT INTO conversations(user_id,title) VALUES (%s,'旧会话') RETURNING id", (user,)).fetchone()[0]
                connection.execute("INSERT INTO messages(conversation_id,role,content) VALUES (%s,'user','旧消息')", (conversation,))
            migrate(owner_dsn)
            with psycopg.connect(owner_dsn) as connection:
                row = connection.execute(
                    "SELECT c.title,m.content,i.user_id=c.user_id FROM conversations c JOIN messages m ON m.conversation_id=c.id JOIN agent_instances i ON i.id=c.instance_id WHERE c.id=%s",
                    (conversation,),
                ).fetchone()
                self.assertEqual(row, ("旧会话", "旧消息", True))

        try:
            await asyncio.to_thread(upgrade)
        finally:
            await asyncio.to_thread(self.pg.drop_database, name)

    async def test_background_workers_serve_multiple_users(self):
        alice, _ = await self.account()
        bob, _ = await self.account("bob")
        _, first = await self.conversation(alice)
        _, second = await self.conversation(bob)
        runs = [(alice, (await self.submit(alice, first)).json()),
                (bob, (await self.submit(bob, second)).json())]
        pool = WorkerPool(self.store, self.runner, replace(self.config, workers=2))
        await pool.start()
        try:
            async with asyncio.timeout(6):
                while True:
                    states = [(await self.client.get(f"/v1/runs/{run['id']}", headers=headers)).json()["status"]
                              for headers, run in runs]
                    if states == ["succeeded", "succeeded"]:
                        break
                    await asyncio.sleep(0.03)
        finally:
            await pool.close()
        self.assertEqual(len({context.user_id for context in self.runner.contexts}), 2)


if __name__ == "__main__":
    unittest.main()
