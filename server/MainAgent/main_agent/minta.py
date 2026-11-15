import base64
import binascii
import json as json_library
from dataclasses import dataclass
from typing import Any

import httpx2

from .core import AgentError


_UNSET = object()


class MintaError(AgentError):
    pass


class MintaProtocolError(MintaError):
    pass


class MintaRequestError(MintaError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class MintaResponse:
    status: int
    headers: list[tuple[str, str]]
    trailers: list[tuple[str, str]]
    body: bytes

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding)

    def json(self) -> Any:
        return json_library.loads(self.body)


class MintaGatewayClient:
    def __init__(self, base_url: str, client: httpx2.AsyncClient):
        self._endpoint = f"{base_url.rstrip('/')}/v1/call"
        self._client = client

    @staticmethod
    def _json_response(response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise MintaProtocolError("MINTA 网关响应不是合法 JSON") from exc

    @staticmethod
    def _header_pairs(value: Any) -> list[tuple[str, str]]:
        if not isinstance(value, list):
            raise MintaProtocolError("MINTA 响应头格式无效")
        pairs = []
        for item in value:
            if (
                not isinstance(item, (list, tuple))
                or len(item) != 2
                or not all(isinstance(part, str) for part in item)
            ):
                raise MintaProtocolError("MINTA 响应头格式无效")
            pairs.append((item[0], item[1]))
        return pairs

    async def call(
        self,
        method: str,
        target: str,
        *,
        headers: dict[str, str] | list[tuple[str, str]] | None = None,
        body: str | None = None,
        body_base64: str | None = None,
        json: Any = _UNSET,
        timeout_ms: int | None = None,
        upstream: str | None = None,
    ) -> MintaResponse:
        if not isinstance(method, str) or not method or any(
            char in method for char in " \t\r\n"
        ):
            raise ValueError("method 必须是无空白的 HTTP 方法")
        if not isinstance(target, str) or not target.startswith("/") or target.startswith("//"):
            raise ValueError("target 必须是 origin-form 路径")
        if sum((body is not None, body_base64 is not None, json is not _UNSET)) > 1:
            raise ValueError("body、body_base64 和 json 只能指定一种")
        if body is not None and not isinstance(body, str):
            raise ValueError("body 必须是 UTF-8 字符串")
        if body_base64 is not None:
            if not isinstance(body_base64, str):
                raise ValueError("body_base64 必须是字符串")
            try:
                base64.b64decode(body_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("body_base64 不是合法 base64") from exc
        if (
            isinstance(timeout_ms, bool)
            or timeout_ms is not None
            and (not isinstance(timeout_ms, int) or timeout_ms <= 0)
        ):
            raise ValueError("timeout_ms 必须是正整数")

        request: dict[str, Any] = {"method": method, "target": target}
        if isinstance(headers, dict):
            request["headers"] = headers
        elif headers is not None:
            request["headers"] = [[name, value] for name, value in headers]
        if body is not None:
            request["body"] = body
        if body_base64 is not None:
            request["body_base64"] = body_base64
        if json is not _UNSET:
            request["json"] = json
        if timeout_ms is not None:
            request["timeout_ms"] = timeout_ms
        if upstream is not None:
            request["upstream"] = upstream

        try:
            response = await self._client.post(self._endpoint, json=request)
        except httpx2.TimeoutException as exc:
            raise TimeoutError("MINTA 网关请求超时") from exc
        except httpx2.HTTPError as exc:
            raise MintaRequestError("MINTA 网关请求失败") from exc

        payload = self._json_response(response)
        if not isinstance(payload, dict):
            raise MintaProtocolError("MINTA 网关响应格式无效")
        if response.status_code != 200:
            error = payload.get("error")
            if (
                not isinstance(error, dict)
                or not isinstance(error.get("code"), str)
                or not isinstance(error.get("message"), str)
            ):
                raise MintaProtocolError("MINTA 网关错误响应格式无效")
            if response.status_code == 504 or error["code"] == "deadline_exceeded":
                raise TimeoutError("MINTA 网关调用超时")
            raise MintaRequestError(
                f"MINTA 网关请求失败：{error['code']}",
                status_code=response.status_code,
                code=error["code"],
            )

        status = payload.get("status")
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            raise MintaProtocolError("MINTA 应用响应状态无效")
        headers_value = self._header_pairs(payload.get("headers"))
        trailers = self._header_pairs(payload.get("trailers"))
        has_text = "body" in payload
        has_binary = "body_base64" in payload
        if has_text == has_binary:
            raise MintaProtocolError("MINTA 应用响应必须且只能包含一种 body")
        if has_text:
            if not isinstance(payload["body"], str):
                raise MintaProtocolError("MINTA 文本响应格式无效")
            body_bytes = payload["body"].encode()
        else:
            if not isinstance(payload["body_base64"], str):
                raise MintaProtocolError("MINTA 二进制响应格式无效")
            try:
                body_bytes = base64.b64decode(payload["body_base64"], validate=True)
            except (binascii.Error, ValueError) as exc:
                raise MintaProtocolError("MINTA 二进制响应格式无效") from exc

        return MintaResponse(status, headers_value, trailers, body_bytes)
