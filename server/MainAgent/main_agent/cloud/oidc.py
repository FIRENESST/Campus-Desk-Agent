"""OIDC authorization-code adapter; HTTP and JWT validation stay outside storage."""
import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx2
import jwt

from .errors import ServiceError
from .security import digest, email_address


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def challenge(verifier):
    return b64(hashlib.sha256(verifier.encode("ascii")).digest())


def sso_error():
    return ServiceError(401, "invalid_sso_response", "校园认证响应无效或已过期，请重新发起登录")


@dataclass(frozen=True)
class ExternalIdentity:
    issuer: str
    subject: str
    email: str | None = None
    email_verified: bool = False
    display_name: str | None = None
    assertion_id: str | None = None


@dataclass(frozen=True)
class AuthorizationRequest:
    url: str
    request_id: str | None = None


class OIDCProvider:
    protocol = "oidc"
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.issuer = settings.oidc_issuer
        self.client = httpx2.AsyncClient(timeout=15, follow_redirects=False, trust_env=False,
                                       transport=transport)

    @property
    def callback_url(self):
        return self.settings.public_url.rstrip("/") + "/v1/auth/sso/campus/callback"

    async def aclose(self):
        await self.client.aclose()

    def _url(self, value):
        if not isinstance(value, str):
            raise sso_error()
        url = urlsplit(value)
        if (not url.hostname or url.username is not None or url.query or url.fragment
            or url.scheme != "https" and not (
                self.settings.development and url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"})):
            raise sso_error()
        return value

    async def _json(self, method, url, **kwargs):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        try:
            async with self.client.stream(method, self._url(url), **kwargs) as response:
                if response.status_code != 200:
                    raise sso_error()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 262144:
                        raise sso_error()
                    chunks.append(chunk)
            data = json.loads(b"".join(chunks), object_pairs_hook=unique,
                              parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
            if not isinstance(data, dict):
                raise sso_error()
            return data
        except (httpx2.HTTPError, ValueError, UnicodeError):
            raise sso_error() from None

    async def metadata(self):
        data = await self._json("GET", self.issuer.rstrip("/") + "/.well-known/openid-configuration")
        if (data.get("issuer") != self.issuer or "code" not in data.get("response_types_supported", [])
            or "RS256" not in data.get("id_token_signing_alg_values_supported", [])
            or "S256" not in data.get("code_challenge_methods_supported", [])
            or self.settings.oidc_token_auth_method not in data.get("token_endpoint_auth_methods_supported", ["client_secret_basic"])):
            raise ServiceError(503, "incompatible_oidc_provider", "校园 OIDC 配置不匹配或不支持要求的认证流程")
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            self._url(data.get(name))
        return data

    async def authorization_url(self, state, nonce, code_challenge, mode):
        data = await self.metadata()
        values = {"client_id": self.settings.oidc_client_id, "response_type": "code",
                  "redirect_uri": self.callback_url, "scope": "openid email profile", "state": state,
                  "nonce": nonce, "code_challenge": code_challenge, "code_challenge_method": "S256",
                  "response_mode": "query"}
        if mode == "link":
            values["prompt"] = "login"
        return data["authorization_endpoint"] + "?" + urlencode(values)

    async def begin(self, state, nonce, code_challenge, mode):
        return AuthorizationRequest(await self.authorization_url(state, nonce, code_challenge, mode))

    async def authenticate(self, parameters, flow, verifier):
        code = parameters.get("code")
        if (parameters.get("error") or not isinstance(code, str) or not 1 <= len(code) <= 4096
            or "iss" in parameters and parameters["iss"] != self.issuer):
            raise sso_error()
        return await self.exchange(code, verifier, flow["nonce_hash"])

    async def exchange(self, code, verifier, nonce_hash):
        data = await self.metadata()
        parameters = {"grant_type": "authorization_code", "code": code,
                      "redirect_uri": self.callback_url, "code_verifier": verifier}
        auth = None
        if self.settings.oidc_token_auth_method == "client_secret_basic":
            # OAuth Basic credentials use form encoding before base64 encoding.
            from urllib.parse import quote_plus
            auth = httpx2.BasicAuth(quote_plus(self.settings.oidc_client_id), quote_plus(self.settings.oidc_client_secret))
        else:
            parameters.update(client_id=self.settings.oidc_client_id, client_secret=self.settings.oidc_client_secret)
        response = await self._json("POST", data["token_endpoint"], data=parameters, auth=auth)
        encoded = response.get("id_token")
        if not isinstance(encoded, str) or len(encoded) > 16384:
            raise sso_error()
        keys = await self._json("GET", data["jwks_uri"])
        return self._validate(encoded, keys, nonce_hash, code, response.get("access_token"))

    def _validate(self, encoded, jwks, nonce_hash, code, access_token):
        try:
            header = jwt.get_unverified_header(encoded)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str) or len(header["kid"]) > 256:
                raise sso_error()
            keys = jwks.get("keys")
            if not isinstance(keys, list) or len(keys) > 32:
                raise sso_error()
            matches = [key for key in keys if isinstance(key, dict) and key.get("kid") == header["kid"]
                       and key.get("kty") == "RSA" and key.get("use", "sig") == "sig"
                       and key.get("alg", "RS256") == "RS256" and "verify" in key.get("key_ops", ["verify"])]
            if len(matches) != 1:
                raise sso_error()
            key = jwt.PyJWK.from_dict(matches[0], algorithm="RS256")
            claims = jwt.decode(encoded, key.key, algorithms=["RS256"], audience=self.settings.oidc_client_id,
                                issuer=self.issuer, leeway=30,
                                options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]})
            if (not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 255
                or type(claims["exp"]) is not int or type(claims["iat"]) is not int
                or not isinstance(claims["nonce"], str)
                or not hmac.compare_digest(digest(claims["nonce"]), nonce_hash)):
                raise sso_error()
            audience = claims["aud"]
            if (isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != self.settings.oidc_client_id
                or "azp" in claims and claims["azp"] != self.settings.oidc_client_id):
                raise sso_error()
            for name, value in (("at_hash", access_token), ("c_hash", code)):
                if name in claims:
                    if not isinstance(value, str) or not isinstance(claims[name], str):
                        raise sso_error()
                    expected = b64(hashlib.sha256(value.encode("ascii")).digest()[:16])
                    if not hmac.compare_digest(expected, claims[name]):
                        raise sso_error()
            address = claims.get("email")
            if address is not None:
                address = email_address(address)
            display = claims.get("name")
            return ExternalIdentity(self.issuer, claims["sub"], address, claims.get("email_verified") is True,
                                    display[:50] if isinstance(display, str) else None)
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, UnicodeError, AttributeError):
            raise sso_error() from None
