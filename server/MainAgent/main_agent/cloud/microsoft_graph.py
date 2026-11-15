"""Delegated Microsoft To Do requests for one campus account."""

from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

import httpx2


AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
GRAPH = "https://graph.microsoft.com/v1.0"
LISTS = GRAPH + "/me/todo/lists"
SCOPES = "https://graph.microsoft.com/Tasks.ReadWrite https://graph.microsoft.com/User.Read offline_access"
SYNC_PROPERTY = "String {94a24ddd-3995-4674-a4d0-488f39e138c2} Name CampusDeskCalendar"
SYNC_FIELDS = {"$expand": f"singleValueExtendedProperties($filter=id eq '{SYNC_PROPERTY}')"}


class GraphError(Exception):
    def __init__(self, code="microsoft_unavailable", retry_after=60):
        self.code = code
        self.retry_after = retry_after
        super().__init__(code)


@dataclass(frozen=True)
class Tokens:
    access: str = field(repr=False)
    refresh: str = field(repr=False)
    expires_in: int


def with_marker(payload, value):
    return {**payload, "singleValueExtendedProperties": [{"id": SYNC_PROPERTY, "value": value}]}


def marker(record):
    properties = record.get("singleValueExtendedProperties") or []
    for entry in properties:
        if isinstance(entry, dict) and entry.get("id") == SYNC_PROPERTY and isinstance(entry.get("value"), str):
            return entry["value"]
    return ""


def item_id(item):
    value = item.get("id")
    if not isinstance(value, str) or not 1 <= len(value) <= 1024 or value in {".", ".."}:
        raise GraphError()
    return value


def item_url(collection, identifier):
    return f"{collection}/{quote(identifier, safe='')}"


class MicrosoftGraph:
    def __init__(self, http, settings):
        self.http, self.settings = http, settings

    def authorization_url(self, state, challenge):
        params = {
            "client_id": self.settings.todo_client_id, "redirect_uri": self.settings.todo_redirect_uri,
            "response_type": "code", "response_mode": "query", "scope": SCOPES,
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account",
        }
        return f"{AUTHORITY}/authorize?{urlencode(params)}"

    async def redeem(self, code, verifier):
        return await self._token(grant_type="authorization_code", code=code, code_verifier=verifier,
                                 redirect_uri=self.settings.todo_redirect_uri)

    async def refresh(self, value):
        return await self._token(grant_type="refresh_token", refresh_token=value)

    async def _token(self, **grant):
        data = await self._request("POST", f"{AUTHORITY}/token", oauth=True, data={
            "client_id": self.settings.todo_client_id, "client_secret": self.settings.todo_client_secret,
            "scope": SCOPES, **grant,
        })
        access, refresh, seconds = data.get("access_token"), data.get("refresh_token"), data.get("expires_in")
        if (not isinstance(access, str) or not 1 <= len(access) <= 16384
            or not isinstance(refresh, str) or not 1 <= len(refresh) <= 16384
            or type(seconds) is not int or not 1 <= seconds <= 86_400):
            raise GraphError()
        return Tokens(access, refresh, seconds)

    async def _request(self, method, url, *, access=None, missing=False, oauth=False, **options):
        headers = {"Accept": "application/json"}
        if access:
            headers["Authorization"] = f"Bearer {access}"
        try:
            response = await self.http.request(method, url, headers=headers, **options)
            if response.status_code == 204:
                return {}
            if response.status_code == 404 and missing:
                return None
            if response.status_code == 429:
                delay = response.headers.get("retry-after", "")
                raise GraphError("microsoft_throttled", max(1, int(delay)) if delay.isdigit() and len(delay) < 7 else 60)
            if response.status_code in {401, 403}:
                raise GraphError("access_expired" if response.status_code == 401 and not oauth else "reauthorize")
            if oauth and response.status_code == 400:
                if response.json().get("error") in {"invalid_grant", "interaction_required", "consent_required"}:
                    raise GraphError("reauthorize")
            if not 200 <= response.status_code < 300:
                raise GraphError()
            result = response.json()
            if not isinstance(result, dict):
                raise GraphError()
            return result
        except (httpx2.HTTPError, ValueError, AttributeError):
            raise GraphError() from None

    async def identity(self, access):
        return await self._request("GET", GRAPH + "/me", access=access, params={"$select": "id,displayName"})

    async def _collection(self, url, access):
        target = httpx2.URL(url)
        for _ in range(100):
            page = await self._request("GET", target, access=access)
            values = page.get("value")
            if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
                raise GraphError()
            for value in values:
                yield value
            next_link = page.get("@odata.nextLink")
            if not next_link:
                return
            if not isinstance(next_link, str):
                raise GraphError()
            try:
                target = httpx2.URL(next_link)
            except (httpx2.InvalidURL, ValueError):
                raise GraphError() from None
            if ((target.scheme, target.host) != ("https", "graph.microsoft.com") or target.port not in {None, 443}
                or target.userinfo or target.fragment or not target.path.startswith("/v1.0/")):
                raise GraphError()
        raise GraphError()

    def lists(self, access):
        return self._collection(str(httpx2.URL(LISTS).copy_merge_params(SYNC_FIELDS)), access)

    async def get_list(self, access, identifier):
        return await self._request("GET", item_url(LISTS, identifier), access=access, params=SYNC_FIELDS, missing=True)

    async def create_list(self, access, value):
        return await self._request("POST", LISTS, access=access, json=with_marker({"displayName": "中传校园日程"}, value))

    def tasks(self, access, list_id):
        url = item_url(LISTS, list_id) + "/tasks"
        return self._collection(str(httpx2.URL(url).copy_merge_params(SYNC_FIELDS)), access)

    async def apply_task(self, access, list_id, method, identifier, payload):
        url = item_url(LISTS, list_id) + "/tasks"
        if identifier:
            url = item_url(url, identifier)
        body = {"json": payload} if payload is not None else {}
        result = await self._request(method, url, access=access, missing=method in {"DELETE", "PATCH"}, **body)
        return result is not None
