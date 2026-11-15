import asyncio
import base64
from datetime import datetime, timezone
from urllib.parse import urlsplit

from cryptography import x509
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from onelogin.saml2.auth import OneLogin_Saml2_Auth
from onelogin.saml2.constants import OneLogin_Saml2_Constants as C
from onelogin.saml2.settings import OneLogin_Saml2_Settings

from .oidc import AuthorizationRequest, ExternalIdentity, sso_error
from .security import email_address


class SAMLProvider:
    protocol = "saml2"

    def __init__(self, settings):
        self.settings = settings
        self.issuer = settings.saml_idp_entity_id
        self.callback_url = settings.public_url.rstrip("/") + "/v1/auth/sso/campus/callback"
        self.entity_id = settings.saml_sp_entity_id or settings.public_url.rstrip("/") + "/v1/auth/sso/campus/metadata"
        certificate = x509.load_pem_x509_certificate(settings.saml_idp_certificate.encode("ascii"))
        self.certificate_expiry = certificate.not_valid_after_utc
        if not certificate.not_valid_before_utc <= datetime.now(timezone.utc) < certificate.not_valid_after_utc:
            raise ValueError("SAML IdP 签名证书不在有效期内")
        self.configuration = {
            "strict": True, "debug": False,
            "sp": {"entityId": self.entity_id, "assertionConsumerService": {"url": self.callback_url, "binding": C.BINDING_HTTP_POST},
                   "NameIDFormat": C.NAMEID_PERSISTENT, "x509cert": settings.saml_sp_certificate,
                   "privateKey": settings.saml_sp_private_key},
            "idp": {"entityId": self.issuer,
                    "singleSignOnService": {"url": settings.saml_idp_sso_url, "binding": C.BINDING_HTTP_REDIRECT},
                    "x509cert": settings.saml_idp_certificate},
            "security": {"wantAssertionsSigned": True, "wantMessagesSigned": True,
                         "wantNameId": True, "wantAttributeStatement": False,
                         "requestedAuthnContext": False,
                         "authnRequestsSigned": bool(settings.saml_sp_private_key),
                         "signatureAlgorithm": C.RSA_SHA256, "digestAlgorithm": C.SHA256},
        }
        OneLogin_Saml2_Settings(self.configuration)

    async def aclose(self):
        pass

    def _request(self, parameters=None):
        url = urlsplit(self.callback_url)
        return {"https": "on", "http_host": url.netloc, "script_name": url.path,
                "get_data": {}, "post_data": parameters or {}}

    async def begin(self, state, nonce, code_challenge, mode):
        def start():
            auth = OneLogin_Saml2_Auth(self._request(), old_settings=self.configuration)
            url = auth.login(return_to=state, force_authn=mode == "link")
            return AuthorizationRequest(url, auth.get_last_request_id())
        return await asyncio.to_thread(start)

    def metadata(self):
        settings = OneLogin_Saml2_Settings(self.configuration)
        document = settings.get_sp_metadata()
        if settings.validate_metadata(document):
            raise sso_error()
        return document

    async def authenticate(self, parameters, flow, verifier):
        def validate():
            if datetime.now(timezone.utc) >= self.certificate_expiry:
                raise sso_error()
            encoded = parameters.get("SAMLResponse")
            if not isinstance(encoded, str) or len(encoded) > 262144 or not flow.get("request_id"):
                raise sso_error()
            try:
                document = base64.b64decode(encoded, validate=True)
                root = ElementTree.fromstring(document, forbid_dtd=True, forbid_entities=True, forbid_external=True)
                auth = OneLogin_Saml2_Auth(self._request({"SAMLResponse": encoded}), old_settings=self.configuration)
                auth.process_response(request_id=flow["request_id"])
                if auth.get_errors() or not auth.is_authenticated():
                    raise sso_error()
                p, a, ds = "{urn:oasis:names:tc:SAML:2.0:protocol}", "{urn:oasis:names:tc:SAML:2.0:assertion}", "{http://www.w3.org/2000/09/xmldsig#}"
                assertions = root.findall(a + "Assertion")
                if (root.tag != p + "Response" or root.get("InResponseTo") != flow["request_id"]
                    or root.get("Destination") != self.callback_url or len(assertions) != 1):
                    raise sso_error()
                assertion = assertions[0]
                audiences = [node.text for node in assertion.findall(".//" + a + "Audience")]
                confirmations = assertion.findall(".//" + a + "SubjectConfirmationData")
                def valid_confirmation(node):
                    if node.get("Recipient") != self.callback_url or node.get("InResponseTo") != flow["request_id"]:
                        return False
                    try:
                        end = datetime.fromisoformat(node.get("NotOnOrAfter", "").replace("Z", "+00:00"))
                        beginning = node.get("NotBefore")
                        return end > datetime.now(timezone.utc) and (not beginning or
                            datetime.fromisoformat(beginning.replace("Z", "+00:00")) <= datetime.now(timezone.utc))
                    except (ValueError, TypeError):
                        return False
                if self.entity_id not in audiences or not any(valid_confirmation(node) for node in confirmations):
                    raise sso_error()
                if auth.get_nameid_format() != C.NAMEID_PERSISTENT:
                    raise sso_error()
                subject = auth.get_nameid()
                if not isinstance(subject, str) or not 1 <= len(subject) <= 255:
                    raise sso_error()
                algorithms = {C.RSA_SHA256, C.RSA_SHA384, C.RSA_SHA512}
                digests = {C.SHA256, C.SHA384, C.SHA512}
                if any(node.get("Algorithm") not in algorithms for node in root.iter(ds + "SignatureMethod")):
                    raise sso_error()
                if any(node.get("Algorithm") not in digests for node in root.iter(ds + "DigestMethod")):
                    raise sso_error()
                attributes = auth.get_attributes()
                emails = attributes.get(self.settings.saml_email_attribute, [])
                address = email_address(emails[0]) if len(emails) == 1 else None
                names = attributes.get("displayName", [])
                display = names[0][:50] if len(names) == 1 and isinstance(names[0], str) else None
                assertion_id = auth.get_last_assertion_id()
                if not assertion_id or len(assertion_id) > 255:
                    raise sso_error()
                return ExternalIdentity(self.issuer, subject, address,
                                        bool(address and self.settings.saml_trust_email), display, assertion_id)
            except (ValueError, TypeError, ElementTree.ParseError, DefusedXmlException):
                raise sso_error() from None
        try:
            return await asyncio.to_thread(validate)
        except Exception:
            # Toolkit errors can contain raw XML or user attributes.
            raise sso_error() from None
