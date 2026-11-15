import re
from urllib.parse import quote, urlencode

import httpx2
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from .oidc import AuthorizationRequest, ExternalIdentity, sso_error
from .errors import ServiceError
from .security import email_address


class CASProvider:
    protocol = "cas"

    def __init__(self, settings, *, transport=None):
        self.settings = settings
        if settings.cas_local:
            self.issuer = "urn:campus-desk:cas:local"
            self.browser_base = settings.public_url.rstrip("/") + "/cas"
            self.validation_base = "http://127.0.0.1:8193/cas"
        else:
            self.issuer = settings.cas_base_url.rstrip("/")
            self.browser_base = self.validation_base = self.issuer
        self.client = httpx2.AsyncClient(timeout=15, follow_redirects=False, trust_env=False, transport=transport)

    async def aclose(self):
        await self.client.aclose()

    async def active_campus_number(self, subject):
        if self.settings.cas_local and self.settings.runtime_status_key:
            try:
                reply = await self.client.get(
                    self.validation_base + "/internal/identities/" + quote(subject, safe=""),
                    headers={"X-Campus-Runtime-Key": self.settings.runtime_status_key}, timeout=3,
                )
                if reply.status_code == 404:
                    return None
                if reply.status_code == 200:
                    payload = reply.json()
                    number = payload.get("campus_number") if isinstance(payload, dict) else None
                    if isinstance(number, str) and re.fullmatch(r"(?=[a-z0-9_.-]*[0-9])[a-z0-9][a-z0-9_.-]{1,31}", number):
                        return number
            except (httpx2.HTTPError, ValueError):
                pass
        raise ServiceError(503, "cas_status_unavailable", "暂时无法核验校园账号状态，请联系管理员")

    def _service(self, state, website=None):
        return (website or self.settings.public_url).rstrip("/") + "/v1/auth/sso/campus/callback?" + urlencode({"state": state})

    async def begin(self, state, nonce, code_challenge, mode, *, website_origin=None):
        browser_base, website = self.browser_base, None
        if self.settings.cas_local and website_origin:
            if cas_url := self.settings.cas_public_sites.get(website_origin):
                website = website_origin
                browser_base = cas_url + "/cas"
            elif website_origin != self.settings.public_url.rstrip("/"):
                raise ServiceError(409, "campus_wrong_origin", "请从已配置的校园助手地址发起校园登录。")
        parameters = {"service": self._service(state, website)}
        if mode == "link":
            parameters["renew"] = "true"
        return AuthorizationRequest(browser_base + "/login?" + urlencode(parameters), request_id=website)

    async def authenticate(self, parameters, flow, verifier):
        ticket = parameters.get("ticket", "")
        if not isinstance(ticket, str) or not ticket.startswith("ST-") or len(ticket) > 4096:
            raise sso_error()
        website = flow.get("request_id") if self.settings.cas_local else None
        if website and website not in self.settings.cas_public_sites:
            raise sso_error()
        values = {"ticket": ticket, "service": self._service(flow["state"], website)}
        if flow["mode"] == "link":
            values["renew"] = "true"
        try:
            async with self.client.stream("GET", self.validation_base + "/" + self.settings.cas_validation_path, params=values) as response:
                if response.status_code != 200:
                    raise sso_error()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 262144:
                        raise sso_error()
                    chunks.append(chunk)
            root = ElementTree.fromstring(b"".join(chunks), forbid_dtd=True, forbid_entities=True, forbid_external=True)
            ns = "{http://www.yale.edu/tp/cas}"
            successes = root.findall(ns + "authenticationSuccess")
            if root.tag != ns + "serviceResponse" or len(successes) != 1 or root.find(ns + "authenticationFailure") is not None:
                raise sso_error()
            users = successes[0].findall(ns + "user")
            if len(users) != 1 or not users[0].text or not 1 <= len(users[0].text) <= 255:
                raise sso_error()
            attributes = successes[0].find(ns + "attributes")
            address, display = None, None
            if attributes is not None:
                emails = [item.text for item in attributes if item.tag == ns + self.settings.cas_email_attribute]
                if len(emails) == 1 and emails[0]:
                    address = email_address(emails[0])
                names = [item.text for item in attributes if item.tag == ns + "displayName"]
                if len(names) == 1 and names[0]:
                    display = names[0][:50]
            return ExternalIdentity(self.issuer, users[0].text, address,
                                    bool(address and self.settings.cas_trust_email), display)
        except (httpx2.HTTPError, ElementTree.ParseError, DefusedXmlException, ValueError):
            raise sso_error() from None
