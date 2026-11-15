"""Shared request bounds and errors for both transports."""

import json

from pydantic import ValidationError
from starlette.requests import ClientDisconnect


MAX_BODY = 8 * 1024 * 1024
MAX_CAMPUS_PHOTO_UPLOAD = 20 * 1024 * 1024
MAX_RAG_UPLOAD = 32 * 1024 * 1024


class InterfaceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code

    def payload(self) -> dict:
        return {"error": {"code": self.code, "message": str(self)}}


async def read_body(request, maximum: int) -> bytes:
    raw = bytearray()
    try:
        async for part in request.stream():
            if len(raw) + len(part) > maximum:
                raise InterfaceError(413, "request_too_large", "请求体超过该接口大小限制")
            raw.extend(part)
    except ClientDisconnect:
        raise InterfaceError(400, "client_disconnected", "请求传输中断") from None
    return bytes(raw)


def parse_json(raw: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("重复的 JSON 字段")
            result[key] = value
        return result

    def invalid_number(_):
        raise ValueError("非有限 JSON 数字")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_number)


def validate(model, value):
    try:
        return model.model_validate(value)
    except ValidationError:
        raise InterfaceError(422, "invalid_request", "请求字段或取值无效") from None


def json_input(request, raw: bytes, model):
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        raise InterfaceError(415, "unsupported_media_type", "请使用 application/json")
    try:
        value = parse_json(raw)
    except (ValueError, RecursionError):
        raise InterfaceError(400, "invalid_json", "请提供有效的 UTF-8 JSON") from None
    return validate(model, value)
