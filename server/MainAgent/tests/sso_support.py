"""Local, cryptographically signed identity-provider fixtures. No campus network."""
import base64
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from xml.sax.saxutils import escape

import httpx2
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from onelogin.saml2.utils import OneLogin_Saml2_Utils

from main_agent.cloud.oidc import challenge


class TestKeys:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_pem = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()).decode()
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Local test identity provider")])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                       .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=30))
                       .sign(self.key, hashes.SHA256()))
        self.certificate = certificate.public_bytes(serialization.Encoding.PEM).decode()
        self.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwk.update(kid="test-key", alg="RS256", use="sig")


class TestOIDC:
    def __init__(self, settings, keys):
        self.settings, self.keys, self.codes = settings, keys, {}
        self.overrides = {}
        self.invalid_signature = False
        self.signing_key_override = None
        self.subject, self.email = "student-1", "student@example.com"
        self.exchanges = 0

    def issue(self, authorization_url):
        args = parse_qs(urlsplit(authorization_url).query)
        code = "code-" + uuid4().hex
        self.codes[code] = args
        return args["state"][0], code

    def handle(self, request):
        base = self.settings.oidc_issuer
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx2.Response(200, json={"issuer": base, "authorization_endpoint": base+"/authorize",
                "token_endpoint": base+"/token", "jwks_uri": base+"/jwks", "response_types_supported": ["code"],
                "id_token_signing_alg_values_supported": ["RS256"], "code_challenge_methods_supported": ["S256"]})
        if request.url.path.endswith("/jwks"):
            return httpx2.Response(200, json={"keys": [self.keys.jwk]})
        if request.url.path.endswith("/token"):
            self.exchanges += 1
            params = parse_qs(request.content.decode())
            args = self.codes.pop(params["code"][0], None)
            if not args or challenge(params["code_verifier"][0]) != args["code_challenge"][0]:
                return httpx2.Response(400, json={"error": "invalid_grant"})
            now = int(time.time())
            claims = {"iss": base, "sub": self.subject, "aud": self.settings.oidc_client_id,
                      "iat": now, "exp": now+300, "nonce": args["nonce"][0],
                      "email": self.email, "email_verified": True, "name": "Test student", **self.overrides}
            if self.invalid_signature:
                encoded = jwt.encode(claims, "incorrect-shared-secret-"+"x"*32, algorithm="HS256", headers={"kid": "test-key"})
            else:
                encoded = jwt.encode(claims, self.signing_key_override or self.keys.key, algorithm="RS256", headers={"kid": "test-key"})
            return httpx2.Response(200, json={"id_token": encoded, "access_token": "test-upstream-access"})
        return httpx2.Response(404)


class TestCAS:
    def __init__(self):
        self.tickets = {}
        self.body_override = None

    def issue(self, authorization_url):
        service = parse_qs(urlsplit(authorization_url).query)["service"][0]
        state = parse_qs(urlsplit(service).query)["state"][0]
        ticket = "ST-" + uuid4().hex
        self.tickets[ticket] = service
        return state, ticket

    def handle(self, request):
        values = parse_qs(request.url.query.decode())
        service = self.tickets.pop(values.get("ticket", [""])[0], None)
        if self.body_override:
            return httpx2.Response(200, content=self.body_override)
        if service != values.get("service", [""])[0]:
            return httpx2.Response(200, content='<cas:serviceResponse xmlns:cas="http://www.yale.edu/tp/cas"><cas:authenticationFailure code="INVALID_TICKET"/></cas:serviceResponse>')
        return httpx2.Response(200, content='<cas:serviceResponse xmlns:cas="http://www.yale.edu/tp/cas"><cas:authenticationSuccess><cas:user>cas-student</cas:user><cas:attributes><cas:mail>cas@example.com</cas:mail></cas:attributes></cas:authenticationSuccess></cas:serviceResponse>')


def signed_saml(keys, settings, request_id, *, subject="saml-student", audience=None, recipient=None,
                expired=False, response_id=None, assertion_id=None):
    callback = settings.public_url.rstrip("/") + "/v1/auth/sso/campus/callback"
    entity = settings.saml_sp_entity_id or settings.public_url.rstrip("/") + "/v1/auth/sso/campus/metadata"
    now = datetime.now(timezone.utc)
    instant = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    before = (now-timedelta(minutes=20 if expired else 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    after = (now+timedelta(minutes=-10 if expired else 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assertion = f'''<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="{assertion_id or '_a'+uuid4().hex}" Version="2.0" IssueInstant="{instant}">
<saml:Issuer>{escape(settings.saml_idp_entity_id)}</saml:Issuer>
<saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:2.0:nameid-format:persistent">{escape(subject)}</saml:NameID>
<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer"><saml:SubjectConfirmationData InResponseTo="{request_id}" Recipient="{recipient or callback}" NotOnOrAfter="{after}"/></saml:SubjectConfirmation></saml:Subject>
<saml:Conditions NotBefore="{before}" NotOnOrAfter="{after}"><saml:AudienceRestriction><saml:Audience>{escape(audience or entity)}</saml:Audience></saml:AudienceRestriction></saml:Conditions>
<saml:AuthnStatement AuthnInstant="{instant}" SessionIndex="test-session"><saml:AuthnContext><saml:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>
<saml:AttributeStatement><saml:Attribute Name="mail"><saml:AttributeValue>saml@example.com</saml:AttributeValue></saml:Attribute></saml:AttributeStatement>
</saml:Assertion>'''
    signed_assertion = OneLogin_Saml2_Utils.add_sign(assertion, keys.private_pem, keys.certificate).decode()
    response = f'''<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="{response_id or '_r'+uuid4().hex}" Version="2.0" IssueInstant="{instant}" Destination="{callback}" InResponseTo="{request_id}">
<saml:Issuer>{escape(settings.saml_idp_entity_id)}</saml:Issuer><samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>{signed_assertion}</samlp:Response>'''
    signed = OneLogin_Saml2_Utils.add_sign(response, keys.private_pem, keys.certificate)
    return base64.b64encode(signed).decode()
