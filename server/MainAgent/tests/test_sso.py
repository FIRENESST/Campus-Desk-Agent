import asyncio
import base64
import importlib.util
import re
import secrets
import sys
import time
import unittest
import zlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from test_cloud import CloudPostgresTestCase, ENABLED, TEST_PASSWORD


SSO_ENABLED = ENABLED and all(importlib.util.find_spec(name) for name in ("jwt", "onelogin", "defusedxml", "xmlsec"))
if SSO_ENABLED:
    import httpx2
    from defusedxml import ElementTree
    from sso_support import TestCAS, TestKeys, TestOIDC, signed_saml
    from main_agent.cloud.admin_data import ProfileReview, _execute, _review_profile
    from main_agent.cloud.auth import set_user_role
    from main_agent.cloud.campus_access import RuleInput, save_rule
    from main_agent.cloud.cas import CASProvider
    from main_agent.cloud.errors import ServiceError
    from main_agent.cloud.oidc import OIDCProvider, challenge
    from main_agent.cloud.saml import SAMLProvider
    from main_agent.cloud.security import digest


@unittest.skipUnless(SSO_ENABLED, "SSO tests require cloud dependencies and CLOUD_TEST_POSTGRES=1")
class SSOTestCase(CloudPostgresTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client.base_url = self.config.public_url

    async def start(self, mode="login", headers=None, password=None):
        verifier = secrets.token_urlsafe(32)
        data = {"mode": mode, "code_challenge": challenge(verifier)}
        if password:
            data["current_password"] = password
        response = await self.client.post("/v1/auth/sso/campus/start", json=data, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return verifier, response.json()["authorization_url"]

    async def finish(self, callback, verifier):
        self.assertEqual(callback.status_code, 200, callback.text)
        return await self.client.post("/v1/auth/sso/campus/finish", json={
            "completion_code": callback.json()["completion_code"], "code_verifier": verifier})

    @staticmethod
    def bearer(response):
        return {"Authorization": "Bearer " + response.json()["access_token"]}


class OIDCIntegrationTests(SSOTestCase):
    def configure_settings(self, settings):
        return replace(settings, public_url="http://test", sso_protocol="oidc", oidc_issuer="https://idp.example.com",
                       oidc_client_id="test-client", oidc_client_secret="test-client-secret",
                       sso_state_secret="independent-test-secret-" + "x"*32)

    def extra_app_arguments(self):
        self.idp = TestOIDC(self.config, TestKeys())
        self.provider = OIDCProvider(self.config, transport=httpx2.MockTransport(self.idp.handle))
        return {"sso_provider": self.provider}

    async def callback(self, url):
        state, code = self.idp.issue(url)
        return await self.client.get("/v1/auth/sso/campus/callback", params={"state": state, "code": code})

    async def login_sso(self):
        verifier, url = await self.start()
        result = await self.finish(await self.callback(url), verifier)
        self.assertEqual(result.status_code, 200, result.text)
        return self.bearer(result)

    async def test_provider_selection_and_new_oidc_account(self):
        discovery = (await self.client.get("/v1/auth/providers")).json()
        self.assertEqual([item["type"] for item in discovery["providers"]], ["password", "oidc"])
        self.assertEqual(discovery["supported_sso_protocols"], ["oidc", "cas", "saml2"])
        self.idp.overrides.update(role="teacher", academic_title="教授")
        headers = await self.login_sso()
        me = (await self.client.get("/v1/me", headers=headers)).json()
        self.assertEqual(me["email"], "student@example.com")
        self.assertEqual((me["role"], me["academic_title"]), ("student", None))
        instances = (await self.client.get("/v1/instances", headers=headers)).json()["items"]
        self.assertEqual([(row["user_id"], row["is_default"]) for row in instances], [(me["id"], True)])
        user = await self.db.run(lambda c: c.execute("SELECT password_hash FROM users").fetchone())
        self.assertIsNone(user["password_hash"])
        sessions = (await self.client.get("/v1/auth/sessions", headers=headers)).json()["items"]
        self.assertEqual(sessions[0]["auth_method"], "campus_sso")
        identities = (await self.client.get("/v1/auth/identities", headers=headers)).json()["items"]
        self.assertEqual((identities[0]["issuer"], identities[0]["subject"]), (self.config.oidc_issuer, "student-1"))

    async def test_same_subject_keeps_owner_when_provider_email_changes(self):
        first = await self.login_sso()
        instance, _ = await self.conversation(first)
        self.idp.email = "changed@example.com"
        second = await self.login_sso()
        one = (await self.client.get("/v1/me", headers=first)).json()
        two = (await self.client.get("/v1/me", headers=second)).json()
        self.assertEqual(one["id"], two["id"])
        self.assertEqual(two["email"], "student@example.com")
        instances = (await self.client.get("/v1/instances", headers=second)).json()["items"]
        self.assertEqual(len([row for row in instances if row["is_default"]]), 1)
        self.assertIn(instance["id"], {row["id"] for row in instances})

    async def test_explicit_link_shares_existing_instances_with_both_logins(self):
        headers, _ = await self.account()
        instance, _ = await self.conversation(headers)
        self.idp.email = "alice@gmail.com"
        verifier, url = await self.start()
        result = await self.finish(await self.callback(url), verifier)
        self.assertEqual((result.status_code, result.json()["error"]["code"]), (409, "account_link_required"))
        verifier, url = await self.start("link", headers, TEST_PASSWORD)
        linked = await self.finish(await self.callback(url), verifier)
        self.assertEqual(linked.json(), {"linked": True})
        sso = await self.login_sso()
        self.assertEqual((await self.client.get("/v1/me", headers=headers)).json()["id"],
                         (await self.client.get("/v1/me", headers=sso)).json()["id"])
        def instance_ids(response):
            return {row["id"] for row in response.json()["items"]}
        password_instances = instance_ids(await self.client.get("/v1/instances", headers=headers))
        self.assertIn(instance["id"], password_instances)
        self.assertEqual(instance_ids(await self.client.get("/v1/instances", headers=sso)), password_instances)

    async def test_binding_requires_session_and_password_and_cannot_steal_identity(self):
        sso = await self.login_sso()
        bob, _ = await self.account("bob")
        data = {"mode": "link", "code_challenge": challenge(secrets.token_urlsafe(32)), "current_password": "wrong"}
        self.assertEqual((await self.client.post("/v1/auth/sso/campus/start", json=data)).status_code, 401)
        self.assertEqual((await self.client.post("/v1/auth/sso/campus/start", json=data, headers=bob)).status_code, 403)
        verifier, url = await self.start("link", bob, TEST_PASSWORD)
        result = await self.finish(await self.callback(url), verifier)
        self.assertEqual(result.status_code, 409)
        self.assertEqual((await self.client.get("/v1/auth/identities", headers=bob)).json()["items"], [])
        self.assertEqual(len((await self.client.get("/v1/auth/identities", headers=sso)).json()["items"]), 1)

    async def test_binding_rechecks_session_after_campus_callback(self):
        headers, _ = await self.account()
        verifier, url = await self.start("link", headers, TEST_PASSWORD)
        callback = await self.callback(url)
        await self.client.post("/v1/auth/logout", headers=headers)
        self.assertEqual((await self.finish(callback, verifier)).status_code, 401)
        count = await self.db.run(lambda c: c.execute("SELECT count(*) AS n FROM external_identities").fetchone())
        self.assertEqual(count["n"], 0)

    async def test_state_browser_binding_and_single_callback_consumption(self):
        verifier, url = await self.start()
        state, code = self.idp.issue(url)
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), base_url="http://test") as stranger:
            missing = await stranger.get("/v1/auth/sso/campus/callback", params={"state": state, "code": code})
            self.assertEqual(missing.status_code, 401)
        response = await self.client.get("/v1/auth/sso/campus/callback", params={"state": state, "code": code})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.finish(response, verifier)).status_code, 200)
        replay = await self.client.get("/v1/auth/sso/campus/callback", params={"state": state, "code": code})
        self.assertEqual(replay.status_code, 401)
        self.assertEqual(self.idp.exchanges, 1)

    async def test_invalid_oidc_signature_nonce_issuer_audience_time_and_azp(self):
        for overrides in ({"nonce": "incorrect"}, {"iss": "https://other.example.com"}, {"aud": "other-client"},
                          {"exp": int(time.time())-300}, {"azp": "other-client"},
                          {"aud": [self.config.oidc_client_id, "other-client"]}, {"iat": int(time.time())+600}):
            with self.subTest(fields=list(overrides)):
                self.idp.overrides = overrides
                _, url = await self.start()
                self.assertEqual((await self.callback(url)).status_code, 401)
        self.idp.overrides, self.idp.invalid_signature = {}, True
        _, url = await self.start()
        self.assertEqual((await self.callback(url)).status_code, 401)
        self.idp.invalid_signature = False
        self.idp.signing_key_override = TestKeys().key
        _, url = await self.start()
        self.assertEqual((await self.callback(url)).status_code, 401)

    async def test_unverified_email_does_not_auto_create_account(self):
        self.idp.overrides = {"email_verified": "true"}
        verifier, url = await self.start()
        self.assertEqual((await self.finish(await self.callback(url), verifier)).status_code, 409)
        count = await self.db.run(lambda c: c.execute("SELECT count(*) AS n FROM users").fetchone())
        self.assertEqual(count["n"], 0)

    async def test_completion_requires_client_pkce_and_is_single_use(self):
        verifier, url = await self.start()
        callback = await self.callback(url)
        self.assertNotIn("access_token", callback.text)
        bad = await self.finish(callback, secrets.token_urlsafe(32))
        self.assertEqual(bad.status_code, 401)
        good = await self.finish(callback, verifier)
        self.assertEqual(good.status_code, 200, good.text)
        self.assertEqual((await self.finish(callback, verifier)).status_code, 401)

    async def test_expired_state_and_completion_are_rejected(self):
        _, url = await self.start()
        await self.db.run(lambda c: c.execute("UPDATE sso_transactions SET expires_at=now()-interval '1 second'"))
        self.assertEqual((await self.callback(url)).status_code, 401)
        verifier, url = await self.start()
        callback = await self.callback(url)
        await self.db.run(lambda c: c.execute("UPDATE sso_completions SET expires_at=now()-interval '1 second'"))
        self.assertEqual((await self.finish(callback, verifier)).status_code, 401)

    async def test_unlink_requires_password_and_revokes_sso_devices(self):
        headers, _ = await self.account()
        verifier, url = await self.start("link", headers, TEST_PASSWORD)
        await self.finish(await self.callback(url), verifier)
        sso = await self.login_sso()
        identity = (await self.client.get("/v1/auth/identities", headers=headers)).json()["items"][0]
        bob, _ = await self.account("bob")
        endpoint = "/v1/auth/identities/" + identity["id"]
        self.assertEqual((await self.client.request("DELETE", endpoint, headers=bob, json={"current_password": TEST_PASSWORD})).status_code, 404)
        removed = await self.client.request("DELETE", endpoint, headers=headers, json={"current_password": TEST_PASSWORD})
        self.assertEqual(removed.status_code, 204, removed.text)
        self.assertEqual((await self.client.get("/v1/me", headers=sso)).status_code, 401)
        self.assertEqual((await self.client.get("/v1/me", headers=headers)).status_code, 200)


class CASIntegrationTests(SSOTestCase):
    def configure_settings(self, settings):
        return replace(settings, public_url="http://test", sso_protocol="cas", cas_base_url="https://cas.example.com/cas",
                       cas_trust_email=True, sso_state_secret="cas-test-secret-"+"x"*32)

    def extra_app_arguments(self):
        self.idp = TestCAS()
        self.provider = CASProvider(self.config, transport=httpx2.MockTransport(self.idp.handle))
        return {"sso_provider": self.provider}

    async def callback(self, url):
        state, ticket = self.idp.issue(url)
        return await self.client.get("/v1/auth/sso/campus/callback", params={"state": state, "ticket": ticket})

    async def test_cas_ticket_validation_and_shared_session_contract(self):
        verifier, url = await self.start()
        result = await self.finish(await self.callback(url), verifier)
        self.assertEqual(result.status_code, 200, result.text)
        identity = (await self.client.get("/v1/auth/identities", headers=self.bearer(result))).json()["items"][0]
        self.assertEqual((identity["protocol"], identity["subject"]), ("cas", "cas-student"))

    async def test_cas_rejects_ticket_issued_for_other_service(self):
        _, old_url = await self.start()
        _, old_ticket = self.idp.issue(old_url)
        _, new_url = await self.start()
        new_state, _ = self.idp.issue(new_url)
        result = await self.client.get("/v1/auth/sso/campus/callback", params={"state": new_state, "ticket": old_ticket})
        self.assertEqual(result.status_code, 401)

    async def test_cas_does_not_trust_email_attributes_by_default(self):
        self.provider.settings = replace(self.config, cas_trust_email=False)
        verifier, url = await self.start()
        self.assertEqual((await self.finish(await self.callback(url), verifier)).status_code, 409)

    async def test_cas_blocks_xml_entities_and_proxy_tickets(self):
        self.idp.body_override = b'<!DOCTYPE x [<!ENTITY data SYSTEM "file:///C:/Windows/win.ini">]><cas:serviceResponse xmlns:cas="http://www.yale.edu/tp/cas"><cas:authenticationSuccess><cas:user>&data;</cas:user></cas:authenticationSuccess></cas:serviceResponse>'
        _, url = await self.start()
        self.assertEqual((await self.callback(url)).status_code, 401)
        _, url = await self.start()
        state, _ = self.idp.issue(url)
        result = await self.client.get("/v1/auth/sso/campus/callback", params={"state": state, "ticket": "PT-not-allowed"})
        self.assertEqual(result.status_code, 401)


class LocalCampusEnrollmentTests(SSOTestCase):
    def configure_settings(self, settings):
        return replace(settings, public_url="http://127.0.0.1:8082", sso_protocol="cas", cas_local=True,
                       cas_public_sites={"https://192.168.42.10:8443": "https://192.168.42.10:8444",
                                         "https://agent.example.com": "https://cas.example.com"},
                       require_campus_profile=True, runtime_status_key="k" * 43, sso_state_secret="s" * 43)

    def extra_app_arguments(self):
        sys.path.append(str(Path(__file__).resolve().parents[3]))
        from server.CASserver.app import create_app as create_cas
        from server.CASserver.store import CASStore

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        (directory / "credential.key").write_bytes(secrets.token_bytes(32))
        self.cas = CASStore(directory)
        self.cas.add("00123456", TEST_PASSWORD)
        self.cas_app = create_cas(directory, self.config.public_url, identity_key=self.config.runtime_status_key,
                                  public_sites=self.config.cas_public_sites)
        provider = CASProvider(self.config, transport=httpx2.ASGITransport(app=self.cas_app, client=("127.0.0.1", 54321)))
        return {"sso_provider": provider, "minta_gateway": self, "teacher_credentials": SimpleNamespace(directory=directory)}

    async def request(self, method, path, payload=None):
        self.assertEqual((method, path), ("GET", "/classroom/api/availability"))
        return {"rooms": [{"roomId": "room-a101", "name": "教室 A101", "capacity": 36, "reserved": False}]}

    async def link(self, headers, number="00123456"):
        verifier, url = await self.start("link", headers, TEST_PASSWORD)
        service = parse_qs(urlsplit(url).query)["service"][0]
        state = parse_qs(urlsplit(service).query)["state"][0]
        ticket, _ = await asyncio.to_thread(self.cas.authenticate, number, TEST_PASSWORD, service, "")
        callback = await self.client.get("/v1/auth/sso/campus/callback", params={"state": state, "ticket": ticket})
        return await self.finish(callback, verifier)

    async def test_schedule_identity_tracks_the_authenticated_campus_account(self):
        headers, _ = await self.account()
        self.assertEqual((await self.link(headers)).json(), {"linked": True})
        schedule = "/v1/auth/access/schedule"
        profile = {"full_name": "测试用户", "college": "测试学院"}
        self.assertEqual((await self.client.put("/v1/profile", headers=headers, json={**profile, "grade": "ug1"})).status_code, 200)
        student = await self.client.get(schedule, headers=headers)
        self.assertEqual((student.status_code, student.headers["x-campus-schedule-role"]), (204, "student"))
        self.assertEqual(student.headers["x-campus-schedule-number"], "00123456")

        await asyncio.to_thread(set_user_role, self.admin_dsn, "alice@gmail.com", "teacher")
        self.assertEqual((await self.client.put("/v1/profile", headers=headers, json=profile)).status_code, 200)
        teacher = await self.client.get(schedule, headers=headers)
        self.assertEqual((teacher.status_code, teacher.headers["x-campus-schedule-role"]), (204, "teacher"))
        self.assertEqual(teacher.headers["x-campus-schedule-number"], "00123456")

        await asyncio.to_thread(self.cas.set_enabled, "00123456", False)
        denied = await self.client.get(schedule, headers=headers)
        self.assertEqual(denied.status_code, 403)
        self.assertNotIn("x-campus-schedule-number", denied.headers)

    async def test_published_cas_links_email_and_signs_in_to_the_same_account_from_lan_and_frp(self):
        headers, _ = await self.account()
        owner = (await self.client.get("/v1/me", headers=headers)).json()["id"]
        start_path = "/v1/auth/sso/campus/start"

        async def authenticate(website, mode):
            self.client.base_url = website
            verifier = secrets.token_urlsafe(32)
            data = {"mode": mode, "code_challenge": challenge(verifier), "website_origin": website}
            if mode == "link":
                data["current_password"] = TEST_PASSWORD
            started = await self.client.post(start_path, json=data, headers=headers if mode == "link" else None)
            self.assertEqual(started.status_code, 200, started.text)
            self.assertIn("Secure", started.headers["set-cookie"])
            url = started.json()["authorization_url"]
            cas = self.config.cas_public_sites[website]
            self.assertTrue(url.startswith(cas + "/cas/login?"))
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=self.cas_app),
                headers={"x-cas-site-origin": cas, "origin": cas},
            ) as browser:
                login = await browser.get(url)
                self.assertEqual(login.status_code, 200, login.text)
                flow = re.search(r'name="lt" value="([^"]+)"', login.text)[1]
                response = await browser.post(cas + "/cas/login", data={
                    "lt": flow, "username": "00123456", "password": TEST_PASSWORD,
                })
                self.assertEqual(response.status_code, 303, response.text)
                callback = response.headers["location"]
                self.assertTrue(callback.startswith(website + "/v1/auth/sso/campus/callback?"))
            return await self.finish(await self.client.get(callback), verifier)

        lan, frp = self.config.cas_public_sites
        before = await authenticate(frp, "login")
        self.assertEqual((before.status_code, before.json()["error"]["code"]), (409, "account_link_required"))
        self.assertEqual((await authenticate(lan, "link")).json(), {"linked": True})
        for site in (lan, frp):
            signed = await authenticate(site, "login")
            self.assertEqual(signed.status_code, 200, signed.text)
            account = await self.client.get("/v1/me", headers=self.bearer(signed))
            self.assertEqual(account.json()["id"], owner)
        blocked = await self.client.post(start_path, json={
            "code_challenge": challenge(secrets.token_urlsafe(32)), "website_origin": "https://unregistered.example.com",
        })
        self.assertEqual((blocked.status_code, blocked.json()["error"]["code"]), (409, "campus_wrong_origin"))

    async def test_cas_controls_access_and_reviewed_academic_data_controls_room_visibility(self):
        headers, _ = await self.account()
        me = "/v1/me"
        profile = "/v1/profile"
        classrooms = "/v1/minta/classrooms"
        data = {"full_name": "测试同学", "college": "测试学院", "grade": "phd_grad"}

        before = await self.client.get(me, headers=headers)
        self.assertEqual(before.json()["campus_status"], "link_required")
        self.assertEqual((await self.client.get("/v1/instances", headers=headers)).status_code, 403)
        self.assertEqual((await self.client.put(profile, headers=headers, json=data)).status_code, 403)
        self.assertEqual((await self.link(headers)).json(), {"linked": True})
        self.assertEqual((await self.client.get(me, headers=headers)).json()["campus_status"], "profile_required")
        self.assertEqual((await self.client.get(profile, headers=headers)).json()["verified_campus_number"], "00123456")
        wrong_number = await self.client.put(profile, headers=headers, json={**data, "campus_number": "00999999"})
        self.assertEqual((wrong_number.status_code, wrong_number.json()["error"]["code"]), (422, "campus_number_mismatch"))
        self.assertEqual((await self.client.get(classrooms, headers=headers)).status_code, 403)
        self.assertEqual((await self.client.put(profile, headers=headers, json={**data, "grade": "invented"})).status_code, 422)

        saved = await self.client.put(profile, headers=headers, json=data)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual((saved.json()["campus_status"], saved.json()["display_name"], saved.json()["full_name"]),
                         ("ready", None, data["full_name"]))
        self.assertEqual((await self.client.get("/v1/instances", headers=headers)).status_code, 200)

        def room_rule(connection, blocked=False):
            rule = RuleInput.model_validate({"audience": {"allow_colleges": [data["college"]], "allow_grades": [data["grade"]]},
                                            "blocked": blocked})
            return save_rule(connection, "classroom", "room-a101", rule)

        await self.db.run(room_rule)
        self.assertEqual((await self.client.get(classrooms, headers=headers)).json()["items"], [])
        row = await self.db.run(lambda c: c.execute("SELECT identity_id,revision FROM campus_profiles").fetchone())
        review = ProfileReview(revision=str(row["revision"]), approved=True)
        await self.db.run(lambda c: _review_profile(c, row["identity_id"], review, "test-operator"))
        with self.assertRaises(ServiceError):
            await self.db.run(lambda c: _review_profile(c, row["identity_id"], review, "test-operator"))
        self.assertFalse((await self.client.get(classrooms, headers=headers)).json()["items"][0]["reserved"])
        user_id = saved.json()["id"]
        deny = RuleInput.model_validate({"audience": {"allow_users": [user_id], "deny_users": [user_id]}})
        await self.db.run(lambda c: save_rule(c, "schedule", "", deny))
        schedule = await self.client.get("/v1/auth/access/schedule", headers=headers)
        self.assertEqual((schedule.status_code, schedule.json()["error"]["code"]), (403, "campus_access_denied"))
        await self.db.run(lambda c: room_rule(c, blocked=True))
        blocked_room = (await self.client.get(classrooms, headers=headers)).json()["items"][0]
        self.assertTrue(blocked_room["reserved"])
        self.assertTrue(blocked_room["booking_blocked"])
        day = (datetime.now(UTC) + timedelta(days=2)).date()
        booking = await self.client.post("/v1/minta/operations", headers={**headers, "Idempotency-Key": "test-blocked-room"}, json={
            "service": "classroom", "resource_id": "room-a101", "teacher": {"name": "王老师", "phone": "13900004321"},
            "starts_at": f"{day}T14:00", "ends_at": f"{day}T15:00",
        })
        self.assertEqual((booking.status_code, booking.json()["error"]["code"]), (409, "classroom_unavailable"))

        self.assertEqual((await self.client.put(profile, headers=headers, json={**data, "display_name": "小同学"})).status_code, 200)
        self.assertTrue((await self.client.get(profile, headers=headers)).json()["reviewed"])
        self.assertEqual((await self.client.put(profile, headers=headers, json={**data, "grade": "phd1"})).status_code, 200)
        self.assertFalse((await self.client.get(profile, headers=headers)).json()["reviewed"])
        self.assertEqual((await self.client.get(classrooms, headers=headers)).json()["items"], [])

        await asyncio.to_thread(self.cas.set_campus_number, "00123456", "00123457")
        updated = (await self.client.get(profile, headers=headers)).json()
        self.assertEqual((updated["campus_status"], updated["verified_campus_number"]), ("profile_required", "00123457"))
        saved_again = await self.client.put(profile, headers=headers, json=data)
        self.assertEqual((saved_again.status_code, saved_again.json()["id"]), (200, user_id))
        self.assertEqual((await self.client.get(profile, headers=headers)).json()["profile"]["campus_number"], "00123457")

        await asyncio.to_thread(self.cas.set_enabled, "00123457", False)
        self.assertEqual((await self.client.get(me, headers=headers)).json()["campus_status"], "link_required")
        self.assertEqual((await self.client.get("/v1/instances", headers=headers)).status_code, 403)
        self.assertEqual((await self.client.post("/v1/auth/logout", headers=headers)).status_code, 204)

    async def test_bulk_review_requires_current_active_cas_bindings_and_commits_together(self):
        await asyncio.to_thread(self.cas.add, "00123457", TEST_PASSWORD)
        profile = {"full_name": "测试同学", "college": "测试学院", "grade": "ug1"}
        for name, number in (("alice", "00123456"), ("bob", "00123457")):
            headers, _ = await self.account(name)
            self.assertEqual((await self.link(headers, number)).json(), {"linked": True})
            saved = await self.client.put("/v1/profile", headers=headers, json=profile)
            self.assertEqual(saved.status_code, 200, saved.text)

        async def read_profiles():
            return await self.db.run(lambda c: c.execute(
                "SELECT identity_id,revision,reviewed_at FROM campus_profiles ORDER BY campus_number",
            ).fetchall())

        pending = await read_profiles()
        targets = [{"id": str(row["identity_id"]), "revision": str(row["revision"])} for row in pending]

        async def approve(items):
            return await _execute(self.app_dsn, "campus_profiles_bulk_review", {
                "payload": {"items": items}, "actor": "test-operator",
            }, cas_accounts=self.cas.identity_accounts)

        with self.assertRaises(ServiceError) as duplicate:
            await approve([targets[0], targets[0]])
        self.assertEqual(duplicate.exception.code, "duplicate_profiles")
        with self.assertRaises(ServiceError) as changed:
            await approve([targets[0], {**targets[1], "revision": targets[0]["revision"]}])
        self.assertEqual(changed.exception.code, "profile_review_stale")

        await asyncio.to_thread(self.cas.set_enabled, "00123457", False)
        with self.assertRaises(ServiceError) as disabled:
            await approve(targets)
        self.assertEqual(disabled.exception.code, "profile_review_stale")
        self.assertEqual(await read_profiles(), pending)

        await asyncio.to_thread(self.cas.set_enabled, "00123457", True)
        self.assertEqual(await approve(targets), {"approved_count": 2})
        self.assertTrue(all(row["reviewed_at"] is not None for row in await read_profiles()))
        with self.assertRaises(ServiceError) as repeated:
            await approve(targets)
        self.assertEqual(repeated.exception.code, "profile_review_stale")
        recorded = await self.db.run(lambda c: c.execute(
            "SELECT count(*) AS total FROM audit_events WHERE action='campus.profile.review' AND outcome='approved'",
        ).fetchone())
        self.assertEqual(recorded["total"], 2)


class SAMLIntegrationTests(SSOTestCase):
    def configure_settings(self, settings):
        self.keys = TestKeys()
        return replace(settings, public_url="https://cloud.example.com", sso_protocol="saml2",
                       saml_idp_entity_id="https://saml.example.com/idp", saml_idp_sso_url="https://saml.example.com/login",
                       saml_idp_certificate=self.keys.certificate, saml_trust_email=True,
                       sso_state_secret="saml-test-secret-"+"x"*32)

    def extra_app_arguments(self):
        self.provider = SAMLProvider(self.config)
        return {"sso_provider": self.provider}

    def request_values(self, url):
        args = parse_qs(urlsplit(url).query)
        request = ElementTree.fromstring(zlib.decompress(base64.b64decode(args["SAMLRequest"][0]), -15))
        return args["RelayState"][0], request.get("ID")

    async def callback(self, url, **options):
        state, request_id = self.request_values(url)
        encoded = signed_saml(self.keys, self.config, options.pop("request_id", request_id), **options)
        return await self.client.post("/v1/auth/sso/campus/callback", data={"RelayState": state, "SAMLResponse": encoded})

    async def test_signed_saml_login_metadata_and_secure_browser_binding(self):
        metadata = await self.client.get("/v1/auth/sso/campus/metadata")
        self.assertEqual(metadata.status_code, 200, metadata.text)
        self.assertIn(self.provider.entity_id, metadata.text)
        verifier, url = await self.start()
        cookie = self.client.cookies.get(self.app.state.sso.cookie_name)
        self.assertIsNotNone(cookie)
        result = await self.finish(await self.callback(url), verifier)
        self.assertEqual(result.status_code, 200, result.text)
        identity = (await self.client.get("/v1/auth/identities", headers=self.bearer(result))).json()["items"][0]
        self.assertEqual((identity["protocol"], identity["subject"]), ("saml2", "saml-student"))

    async def test_saml_rejects_expired_wrong_request_audience_and_recipient(self):
        for options in ({"expired": True}, {"request_id": "_different-request"},
                        {"audience": "https://attacker.example.com"},
                        {"recipient": self.provider.callback_url + "-evil"}):
            with self.subTest(fields=list(options)):
                _, url = await self.start()
                result = await self.callback(url, **options)
                self.assertEqual(result.status_code, 401, result.text)

    async def test_saml_tampered_signature_is_rejected(self):
        _, url = await self.start()
        state, request_id = self.request_values(url)
        encoded = signed_saml(self.keys, self.config, request_id)
        tampered = base64.b64encode(base64.b64decode(encoded).replace(b"saml-student", b"intruder-user")).decode()
        result = await self.client.post("/v1/auth/sso/campus/callback", data={"RelayState": state, "SAMLResponse": tampered})
        self.assertEqual(result.status_code, 401)
        self.assertNotIn("intruder-user", result.text)

    async def test_saml_assertion_reuse_is_blocked_across_new_flows(self):
        assertion_id = "_fixed-assertion-id"
        verifier, url = await self.start()
        result = await self.finish(await self.callback(url, assertion_id=assertion_id), verifier)
        self.assertEqual(result.status_code, 200, result.text)
        verifier, url = await self.start()
        result = await self.finish(await self.callback(url, assertion_id=assertion_id), verifier)
        self.assertEqual(result.status_code, 401)


if __name__ == "__main__":
    unittest.main()
