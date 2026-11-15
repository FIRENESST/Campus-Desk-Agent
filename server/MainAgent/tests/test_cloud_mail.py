import asyncio
import os
import ssl
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

import httpx2

from main_agent.cloud.config import Settings
from main_agent.cloud.mail import SMTPMailer


def mail_settings(provider, **env):
    variables = {
        "CLOUD_DATABASE_URL": "postgresql://unused",
        "CLOUD_DEVELOPMENT": "true",
        "CLOUD_SMTP_PROVIDER": provider,
        "CLOUD_SMTP_FROM": "sender@example.com",
        **env,
    }
    with patch.dict(os.environ, variables, clear=True):
        return Settings.from_env()


class SMTPSettingsTests(unittest.TestCase):
    def test_known_providers_choose_transport_and_default_username(self):
        cases = [
            ("qq", "smtp.qq.com", 465, "ssl", "password"),
            ("163", "smtp.163.com", 465, "ssl", "password"),
            ("126", "smtp.126.com", 465, "ssl", "password"),
            ("yeah", "smtp.yeah.net", 465, "ssl", "password"),
            ("gmail", "smtp.gmail.com", 465, "ssl", "password"),
            ("outlook", "smtp-mail.outlook.com", 587, "starttls", "oauth2"),
        ]
        for provider, host, port, tls, auth in cases:
            with self.subTest(provider=provider):
                settings = mail_settings(provider, CLOUD_SMTP_PORT="2525", CLOUD_SMTP_HOST="unused.example.com")
                self.assertEqual((settings.smtp_host, settings.smtp_port, settings.smtp_tls, settings.smtp_auth),
                                 (host, port, tls, auth))
                self.assertEqual(settings.smtp_user, "sender@example.com")

    def test_other_providers_and_custom_hosts_are_rejected(self):
        for provider in ("yahoo", "icloud", "microsoft365", "aliyun-mail", "tencent-enterprise", "custom", ""):
            with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, "CLOUD_SMTP_PROVIDER"):
                mail_settings(provider, CLOUD_SMTP_HOST="relay.example.com")
        self.assertFalse(mail_settings("custom").smtp_host)

    def test_only_supported_oauth_modes_and_endpoints_are_used(self):
        self.assertEqual(mail_settings("gmail", CLOUD_SMTP_AUTH="oauth2").smtp_oauth_token_url,
                         "https://oauth2.googleapis.com/token")
        self.assertEqual(mail_settings("outlook", CLOUD_SMTP_OAUTH_TOKEN_URL="https://unused.example.com/token").smtp_oauth_token_url,
                         "https://login.microsoftonline.com/consumers/oauth2/v2.0/token")
        for provider, auth in (("outlook", "password"), ("qq", "none"), ("163", "oauth2")):
            with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, "CLOUD_SMTP_AUTH"):
                mail_settings(provider, CLOUD_SMTP_AUTH=auth)


class SMTPDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.enterContext(patch("main_agent.cloud.mail.ssl.create_default_context", return_value=context))

    async def test_app_password_sends_to_a_different_mail_provider(self):
        config = mail_settings("qq", CLOUD_SMTP_PASSWORD="example-app-password")
        with patch("main_agent.cloud.mail.smtplib.SMTP_SSL") as transport:
            await SMTPMailer(config).send("student@outlook.com", "verify", "test-verification-code")
        client = transport.return_value
        transport.assert_called_once()
        self.assertEqual(transport.call_args.args, ("smtp.qq.com", 465))
        client.login.assert_called_once_with("sender@example.com", "example-app-password")
        message = client.send_message.call_args.args[0]
        self.assertEqual(str(message["To"]), "student@outlook.com")
        self.assertIn("test-verification-code", message.get_content())
        self.assertIn("有效期 10 分钟", message.get_content())

    async def test_oauth_uses_xoauth2_after_tls_and_serializes_token_refresh(self):
        config = mail_settings("outlook", CLOUD_SMTP_OAUTH_CLIENT_ID="client", CLOUD_SMTP_OAUTH_CLIENT_SECRET="client-secret",
                               CLOUD_SMTP_OAUTH_REFRESH_TOKEN="first-refresh")
        clock = 1000.0
        requested = []

        def token_endpoint(request):
            requested.append(parse_qs(request.content.decode("utf-8")))
            self.assertEqual(str(request.url), "https://login.microsoftonline.com/consumers/oauth2/v2.0/token")
            return httpx2.Response(200, json={
                "access_token": f"access-{len(requested)}", "token_type": "Bearer",
                "expires_in": 3600, "refresh_token": "next-refresh",
            })

        real_client = httpx2.Client
        smtp = MagicMock()
        with (
            patch("main_agent.cloud.mail.monotonic", side_effect=lambda: clock),
            patch("main_agent.cloud.mail.httpx2.Client",
                  side_effect=lambda **options: real_client(transport=httpx2.MockTransport(token_endpoint), **options)),
            patch("main_agent.cloud.mail.smtplib.SMTP", return_value=smtp),
        ):
            mailer = SMTPMailer(config)
            await asyncio.gather(
                mailer.send("student@qq.com", "verify", "one"),
                mailer.send("student@gmail.com", "reset", "two"),
            )
            self.assertEqual(len(requested), 1)
            self.assertEqual(requested[0], {
                "client_id": ["client"], "client_secret": ["client-secret"],
                "grant_type": ["refresh_token"], "refresh_token": ["first-refresh"],
            })
            clock += 3601
            await mailer.send("student@163.com", "verify", "three")
        self.assertEqual(requested[1]["refresh_token"], ["next-refresh"])
        self.assertEqual(smtp.auth.call_count, 3)
        mechanism, reply = smtp.auth.call_args.args
        self.assertEqual(mechanism, "XOAUTH2")
        self.assertEqual(reply(), "user=sender@example.com\x01auth=Bearer access-2\x01\x01")
        self.assertEqual(reply(b"challenge"), "")
        self.assertLess([call[0] for call in smtp.method_calls].index("starttls"),
                        [call[0] for call in smtp.method_calls].index("auth"))
        smtp.login.assert_not_called()

    async def test_failed_token_refresh_stops_before_smtp(self):
        config = mail_settings("outlook", CLOUD_SMTP_OAUTH_CLIENT_ID="client",
                               CLOUD_SMTP_OAUTH_REFRESH_TOKEN="private-refresh-token")
        with (
            patch("main_agent.cloud.mail.smtplib.SMTP") as transport,
            patch("main_agent.cloud.mail.httpx2.Client") as http,
        ):
            reply = http.return_value.__enter__.return_value.post.return_value
            reply.raise_for_status.side_effect = httpx2.HTTPError("private-provider-response")
            with self.assertRaisesRegex(RuntimeError, "^SMTP OAuth 令牌续期失败$"):
                await SMTPMailer(config).send("student@qq.com", "verify", "ignored")
        transport.assert_not_called()
