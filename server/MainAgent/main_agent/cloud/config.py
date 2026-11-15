import json
import os
import re
from dataclasses import dataclass, field
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID


DEFAULT_CLOUD_PORT = 8081
DEFAULT_PUBLIC_URL = f"http://127.0.0.1:{DEFAULT_CLOUD_PORT}"
DEFAULT_ROUTING_EMBEDDING_PORT = 8002
MAX_ACTIVE_RUNS_PER_USER = 3


@dataclass(frozen=True)
class SMTPPreset:
    host: str
    port: int = 465
    tls: str = "ssl"
    auth: str = "password"


SMTP_PRESETS = {
    "qq": SMTPPreset("smtp.qq.com"),
    "163": SMTPPreset("smtp.163.com"),
    "126": SMTPPreset("smtp.126.com"),
    "yeah": SMTPPreset("smtp.yeah.net"),
    "gmail": SMTPPreset("smtp.gmail.com"),
    "outlook": SMTPPreset("smtp-mail.outlook.com", 587, "starttls", "oauth2"),
}
SMTP_OAUTH_ENDPOINTS = {
    "gmail": "https://oauth2.googleapis.com/token",
    "outlook": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
}


def _smtp_environment():
    provider = os.getenv("CLOUD_SMTP_PROVIDER", "").strip().lower()
    sender = os.getenv("CLOUD_SMTP_FROM", "").strip()
    user = os.getenv("CLOUD_SMTP_USER", "").strip()
    manual_host = os.getenv("CLOUD_SMTP_HOST", "").strip()
    if provider in {"", "custom"} and not manual_host:
        return {}
    preset = SMTP_PRESETS.get(provider)
    if preset is None:
        raise ValueError("CLOUD_SMTP_PROVIDER 仅支持 qq、163、126、yeah、gmail 或 outlook")
    auth = os.getenv("CLOUD_SMTP_AUTH", "").strip().lower() or preset.auth
    if auth != preset.auth and (provider, auth) != ("gmail", "oauth2"):
        raise ValueError("此邮件服务商不支持配置的 CLOUD_SMTP_AUTH")
    return dict(
        smtp_host=preset.host, smtp_port=preset.port, smtp_tls=preset.tls, smtp_auth=auth,
        smtp_user=user or parseaddr(sender)[1], smtp_from=sender, smtp_password=os.getenv("CLOUD_SMTP_PASSWORD", ""),
        smtp_oauth_token_url=SMTP_OAUTH_ENDPOINTS.get(provider, "") if auth == "oauth2" else "",
        smtp_oauth_client_id=os.getenv("CLOUD_SMTP_OAUTH_CLIENT_ID", ""),
        smtp_oauth_client_secret=os.getenv("CLOUD_SMTP_OAUTH_CLIENT_SECRET", ""),
        smtp_oauth_refresh_token=os.getenv("CLOUD_SMTP_OAUTH_REFRESH_TOKEN", ""),
    )


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    public_url: str = DEFAULT_PUBLIC_URL
    development: bool = False
    require_verified_email: bool = True
    access_seconds: int = 900
    refresh_seconds: int = 604800
    session_seconds: int = 2592000
    max_instances: int = 10
    max_active_runs_per_user: int = MAX_ACTIVE_RUNS_PER_USER
    workers: int = 2
    lease_seconds: int = 60
    run_timeout: int = 300
    pool_size: int = 12
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = field(default="", repr=False)
    smtp_from: str = ""
    smtp_tls: str = "starttls"
    smtp_auth: str = "auto"
    smtp_oauth_token_url: str = ""
    smtp_oauth_client_id: str = ""
    smtp_oauth_client_secret: str = field(default="", repr=False)
    smtp_oauth_refresh_token: str = field(default="", repr=False)
    runner_factory: str = ""
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = field(default="", repr=False)
    sso_state_secret: str = field(default="", repr=False)
    oidc_token_auth_method: str = "client_secret_basic"
    sso_protocol: str = ""
    cas_base_url: str = ""
    cas_local: bool = False
    cas_public_sites: dict[str, str] = field(default_factory=dict)
    cas_validation_path: str = "p3/serviceValidate"
    cas_email_attribute: str = "mail"
    cas_trust_email: bool = False
    saml_idp_entity_id: str = ""
    saml_idp_sso_url: str = ""
    saml_idp_certificate: str = field(default="", repr=False)
    saml_sp_entity_id: str = ""
    saml_sp_certificate: str = field(default="", repr=False)
    saml_sp_private_key: str = field(default="", repr=False)
    saml_email_attribute: str = "mail"
    saml_trust_email: bool = False
    routing_data_dir: str = ""
    routing_embedding_port: int = DEFAULT_ROUTING_EMBEDDING_PORT
    runtime_status_key: str = field(default="", repr=False)
    require_campus_profile: bool = True
    todo_client_id: str = ""
    todo_client_secret: str = field(default="", repr=False)
    todo_redirect_uri: str = ""
    todo_token_key: str = field(default="", repr=False)

    @property
    def selected_sso_protocol(self):
        return self.sso_protocol

    @property
    def smtp_auth_mode(self):
        if self.smtp_auth == "auto":
            return "password" if self.smtp_user else "none"
        return self.smtp_auth

    def __post_init__(self):
        if not self.database_url:
            raise ValueError("必须设置 CLOUD_DATABASE_URL")
        if self.runtime_status_key and not re.fullmatch(r"[A-Za-z0-9_-]{43}", self.runtime_status_key):
            raise ValueError("运行状态凭据格式无效")
        todo = (self.todo_client_id, self.todo_client_secret, self.todo_redirect_uri, self.todo_token_key)
        if any(todo):
            if not all(todo):
                raise ValueError("Microsoft To Do 需要完整设置四项 CLOUD_TODO 配置")
            try:
                UUID(self.todo_client_id)
            except ValueError:
                raise ValueError("CLOUD_TODO_CLIENT_ID 须为 Microsoft Entra 应用 ID") from None
            redirect = urlsplit(self.todo_redirect_uri)
            if (not redirect.hostname or redirect.username is not None or redirect.query or redirect.fragment
                or redirect.path != "/oauth/microsoft-todo/callback"
                or redirect.scheme != "https" and not (
                    self.development and redirect.scheme == "http" and redirect.hostname in {"localhost", "127.0.0.1"}
                )):
                raise ValueError("CLOUD_TODO_REDIRECT_URI 须为网站 HTTPS 地址下的 /oauth/microsoft-todo/callback")
        url = urlsplit(self.public_url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.query or url.fragment:
            raise ValueError("CLOUD_PUBLIC_URL 必须是服务的固定 HTTP/HTTPS 地址")
        if not self.development and (url.scheme != "https" or not self.require_verified_email or not self.require_campus_profile):
            raise ValueError("生产模式要求 HTTPS、邮箱验证和校园身份登记")
        if self.smtp_tls not in {"starttls", "ssl"}:
            raise ValueError("SMTP 必须使用 starttls 或 ssl")
        if self.smtp_auth not in {"auto", "password", "oauth2", "none"}:
            raise ValueError("CLOUD_SMTP_AUTH 必须为 auto、password、oauth2 或 none")
        for name in ("access_seconds", "refresh_seconds", "session_seconds", "max_instances",
                     "max_active_runs_per_user", "lease_seconds", "run_timeout", "pool_size", "smtp_port"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须是正整数")
        if self.max_active_runs_per_user > MAX_ACTIVE_RUNS_PER_USER:
            object.__setattr__(self, "max_active_runs_per_user", MAX_ACTIVE_RUNS_PER_USER)
        if self.smtp_port > 65535:
            raise ValueError("SMTP 端口必须在 1 到 65535 之间")
        if type(self.routing_embedding_port) is not int or not 0 <= self.routing_embedding_port <= 65535:
            raise ValueError("CLOUD_ROUTING_EMBEDDING_PORT 必须是 0 到 65535 的整数")
        if self.workers < 0 or self.workers > self.pool_size:
            raise ValueError("workers 必须介于 0 和 pool_size 之间")
        if self.lease_seconds < 6 or self.access_seconds > self.refresh_seconds or self.refresh_seconds > self.session_seconds:
            raise ValueError("租约至少 6 秒，令牌时长必须 access <= refresh <= session")
        protocol = self.selected_sso_protocol
        if protocol not in {"", "oidc", "cas", "saml2"}:
            raise ValueError("CLOUD_SSO_PROTOCOL 必须为空、oidc、cas 或 saml2")
        if protocol and len(self.sso_state_secret) < 32:
            raise ValueError("SSO_STATE_SECRET 至少 32 字符，各服务副本必须保持一致")
        if protocol == "oidc":
            if not all((self.oidc_issuer, self.oidc_client_id, self.oidc_client_secret, self.sso_state_secret)):
                raise ValueError("校园 OIDC 需要 issuer、client_id、client_secret 和 SSO_STATE_SECRET")
            issuer = urlsplit(self.oidc_issuer)
            if (not issuer.hostname or issuer.username is not None or issuer.query or issuer.fragment
                or issuer.scheme != "https" and not (
                    self.development and issuer.scheme == "http" and issuer.hostname in {"127.0.0.1", "localhost", "::1"})):
                raise ValueError("OIDC issuer 必须为 HTTPS（开发环境仅允许本机 HTTP）")
        if self.oidc_token_auth_method not in {"client_secret_basic", "client_secret_post"}:
            raise ValueError("OIDC token 端点认证方法无效")
        if protocol == "cas":
            if self.cas_local:
                self._sso_url(self.public_url)
                if url.path not in {"", "/"}:
                    raise ValueError("本地 CAS 的 CLOUD_PUBLIC_URL 必须为网站源地址")
                if not isinstance(self.cas_public_sites, dict) or len(self.cas_public_sites) > 8:
                    raise ValueError("CLOUD_CAS_PUBLIC_SITES 需要网站与 CAS 源地址的映射")
                for website, cas in self.cas_public_sites.items():
                    for address in (website, cas):
                        if not isinstance(address, str):
                            raise ValueError("校园助手和 CAS 源地址须为 HTTPS URL")
                        self._sso_url(address)
                        origin = urlsplit(address)
                        if origin.scheme != "https" or origin.path:
                            raise ValueError("校园助手和 CAS 源地址须为 HTTPS URL")
                    if website == cas:
                        raise ValueError("CAS 和校园助手需要各自的 HTTPS 源地址")
            else:
                self._sso_url(self.cas_base_url)
            if self.cas_validation_path not in {"serviceValidate", "p3/serviceValidate"}:
                raise ValueError("CAS 校验路径必须为 serviceValidate 或 p3/serviceValidate")
        if protocol == "saml2":
            self._sso_url(self.saml_idp_sso_url)
            if urlsplit(self.public_url).scheme != "https":
                raise ValueError("SAML POST 回调的浏览器绑定 Cookie 要求 HTTPS")
            if not self.saml_idp_entity_id or not self.saml_idp_certificate:
                raise ValueError("SAML 需要 IdP entity ID 和受信任签名证书")
            if bool(self.saml_sp_certificate) != bool(self.saml_sp_private_key):
                raise ValueError("SAML SP 证书和私钥必须成对设置")

    def _sso_url(self, value):
        url = urlsplit(value)
        if (not url.hostname or url.username is not None or url.query or url.fragment
            or url.scheme != "https" and not (
                self.development and url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"})):
            raise ValueError("校园身份源地址必须为 HTTPS（开发环境仅允许本机 HTTP）")

    @classmethod
    def from_env(cls):
        def flag(name, default):
            value = os.getenv(name, str(default)).lower()
            if value not in {"true", "false"}:
                raise ValueError(f"{name} 必须是 true 或 false")
            return value == "true"

        def pem(name):
            path = os.getenv(name, "")
            return Path(path).read_text(encoding="utf-8") if path and os.getenv("CLOUD_SSO_PROTOCOL") == "saml2" else ""

        return cls(
            database_url=os.getenv("CLOUD_DATABASE_URL", ""),
            public_url=os.getenv("CLOUD_PUBLIC_URL", DEFAULT_PUBLIC_URL).rstrip("/"),
            development=flag("CLOUD_DEVELOPMENT", False),
            require_verified_email=flag("CLOUD_REQUIRE_VERIFIED_EMAIL", True),
            workers=int(os.getenv("CLOUD_WORKERS", "2")),
            pool_size=int(os.getenv("CLOUD_DB_POOL_SIZE", "12")),
            max_instances=int(os.getenv("CLOUD_MAX_INSTANCES", "10")),
            max_active_runs_per_user=int(os.getenv("CLOUD_MAX_ACTIVE_RUNS", str(MAX_ACTIVE_RUNS_PER_USER))),
            **_smtp_environment(),
            runner_factory=os.getenv("CLOUD_RUNNER_FACTORY", ""),
            oidc_issuer=os.getenv("CLOUD_OIDC_ISSUER", ""),
            oidc_client_id=os.getenv("CLOUD_OIDC_CLIENT_ID", ""),
            oidc_client_secret=os.getenv("CLOUD_OIDC_CLIENT_SECRET", ""),
            sso_state_secret=os.getenv("CLOUD_SSO_STATE_SECRET", ""),
            oidc_token_auth_method=os.getenv("CLOUD_OIDC_TOKEN_AUTH_METHOD", "client_secret_basic"),
            sso_protocol=os.getenv("CLOUD_SSO_PROTOCOL", ""),
            cas_base_url=os.getenv("CLOUD_CAS_BASE_URL", ""),
            cas_local=flag("CLOUD_CAS_LOCAL", False),
            cas_public_sites=json.loads(os.getenv("CLOUD_CAS_PUBLIC_SITES", "{}")),
            cas_validation_path=os.getenv("CLOUD_CAS_VALIDATION_PATH", "p3/serviceValidate"),
            cas_email_attribute=os.getenv("CLOUD_CAS_EMAIL_ATTRIBUTE", "mail"),
            cas_trust_email=flag("CLOUD_CAS_TRUST_EMAIL", False),
            saml_idp_entity_id=os.getenv("CLOUD_SAML_IDP_ENTITY_ID", ""),
            saml_idp_sso_url=os.getenv("CLOUD_SAML_IDP_SSO_URL", ""),
            saml_idp_certificate=pem("CLOUD_SAML_IDP_CERT_FILE"),
            saml_sp_entity_id=os.getenv("CLOUD_SAML_SP_ENTITY_ID", ""),
            saml_sp_certificate=pem("CLOUD_SAML_SP_CERT_FILE"),
            saml_sp_private_key=pem("CLOUD_SAML_SP_KEY_FILE"),
            saml_email_attribute=os.getenv("CLOUD_SAML_EMAIL_ATTRIBUTE", "mail"),
            saml_trust_email=flag("CLOUD_SAML_TRUST_EMAIL", False),
            routing_data_dir=os.getenv("CLOUD_ROUTING_DATA_DIR", ""),
            routing_embedding_port=int(os.getenv("CLOUD_ROUTING_EMBEDDING_PORT", str(DEFAULT_ROUTING_EMBEDDING_PORT))),
            runtime_status_key=os.getenv("MPM_RUNTIME_STATUS_KEY", ""),
            todo_client_id=os.getenv("CLOUD_TODO_CLIENT_ID", ""),
            todo_client_secret=os.getenv("CLOUD_TODO_CLIENT_SECRET", ""),
            todo_redirect_uri=os.getenv("CLOUD_TODO_REDIRECT_URI", ""),
            todo_token_key=os.getenv("CLOUD_TODO_TOKEN_KEY", ""),
        )
