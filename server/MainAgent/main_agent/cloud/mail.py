import asyncio
import re
import smtplib
import ssl
from email.message import EmailMessage
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

import httpx2

from .security import EMAIL_TOKEN_SECONDS


class _OAuthTokens:
    def __init__(self, config):
        url = urlsplit(config.smtp_oauth_token_url)
        if url.scheme != "https" or not url.hostname or url.username is not None or url.query or url.fragment:
            raise ValueError("SMTP OAuth 需要固定的 HTTPS 令牌端点")
        self._config = config
        self._refresh = config.smtp_oauth_refresh_token
        self._access = ""
        self._expires_at = 0.0
        self._lock = Lock()

    def access_token(self):
        with self._lock:
            now = monotonic()
            if now < self._expires_at:
                return self._access
            config = self._config
            data = {"grant_type": "refresh_token", "client_id": config.smtp_oauth_client_id, "refresh_token": self._refresh}
            if config.smtp_oauth_client_secret:
                data["client_secret"] = config.smtp_oauth_client_secret
            try:
                with httpx2.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
                    response = client.post(config.smtp_oauth_token_url, data=data)
                    response.raise_for_status()
                    result = response.json()
                token = result.get("access_token")
                lifetime = int(result.get("expires_in", 0))
                if (not isinstance(token, str) or not re.fullmatch(r"[\x21-\x7e]{1,16384}", token)
                    or str(result.get("token_type", "")).lower() != "bearer" or not 1 <= lifetime <= 86400):
                    raise ValueError("invalid token response")
            except (httpx2.HTTPError, AttributeError, TypeError, ValueError, OverflowError):
                raise RuntimeError("SMTP OAuth 令牌续期失败") from None
            replacement = result.get("refresh_token")
            if isinstance(replacement, str) and replacement:
                self._refresh = replacement
            self._access = token
            self._expires_at = now + lifetime - min(60, lifetime / 5)
            return token


class SMTPMailer:
    @staticmethod
    def missing_configuration(settings):
        if not settings.smtp_host or not settings.smtp_from:
            return "邮箱认证需要配置 CLOUD_SMTP_PROVIDER 和 CLOUD_SMTP_FROM"
        mode = settings.smtp_auth_mode
        if mode in {"password", "oauth2"} and not settings.smtp_user:
            return "SMTP 认证需要登录账号"
        if mode == "password" and not settings.smtp_password:
            return "SMTP 密码认证需要 CLOUD_SMTP_PASSWORD"
        if mode == "oauth2" and (not settings.smtp_oauth_client_id or not settings.smtp_oauth_refresh_token):
            return "SMTP OAuth 需要 CLOUD_SMTP_OAUTH_CLIENT_ID 和 CLOUD_SMTP_OAUTH_REFRESH_TOKEN"
        return ""

    def __init__(self, settings):
        self.settings = settings
        missing = self.missing_configuration(settings)
        if missing:
            raise ValueError(missing)
        self._auth = settings.smtp_auth_mode
        self._oauth = None
        if self._auth == "oauth2":
            if not re.fullmatch(r"[\x21-\x7e]{1,320}", settings.smtp_user):
                raise ValueError("SMTP OAuth 需要有效的 CLOUD_SMTP_USER")
            self._oauth = _OAuthTokens(settings)

    async def send(self, recipient: str, purpose: str, secret: str):
        def deliver():
            config = self.settings
            message = EmailMessage()
            message["From"], message["To"] = config.smtp_from, recipient
            message["Subject"] = {"verify": "验证邮箱", "reset": "重置密码", "change": "修改密码的邮箱验证"}[purpose]
            # Deliberately avoid query-string tokens (proxy logs, referrers).
            message.set_content(
                f"请在 {config.public_url} 提交下面的一次性令牌。\n\n{secret}\n\n"
                f"有效期 {EMAIL_TOKEN_SECONDS[purpose] // 60} 分钟。\n"
                "如果这封邮件来得意外，请忽略。"
            )
            token = self._oauth.access_token() if self._oauth is not None else None
            context = ssl.create_default_context()
            if config.smtp_tls == "ssl":
                client = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=15, context=context)
            else:
                client = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=15)
            with client:
                if config.smtp_tls == "starttls":
                    client.starttls(context=context)
                if self._auth == "password":
                    client.login(config.smtp_user, config.smtp_password)
                elif token is not None:
                    client.ehlo_or_helo_if_needed()
                    sasl = f"user={config.smtp_user}\x01auth=Bearer {token}\x01\x01"
                    client.auth("XOAUTH2", lambda challenge=None: sasl if challenge is None else "")
                client.send_message(message)

        await asyncio.to_thread(deliver)


class DisabledMailer:
    async def send(self, recipient, purpose, secret):
        raise RuntimeError("SMTP 未配置")
