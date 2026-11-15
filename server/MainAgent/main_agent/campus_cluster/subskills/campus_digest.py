"""Read published campus digest entries through the local indexed endpoint."""

import json
import os
from typing import Annotated, Literal

import httpx2
from pydantic import Field, StringConstraints

from ...reliability.models import ToolResult
from ..skills import Skill, SkillContext, SkillInput


class CampusDigestInput(SkillInput):
    query: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""
    source: Literal["all", "web", "wechat"] = "all"
    days: int = Field(default=30, ge=0, le=36_500, description="近几天，0 表示不限时间")
    limit: int = Field(default=6, ge=1, le=8)


async def digest_request(path, *, params=None, runtime_key=None, payload=None):
    port = int(os.getenv("CAMPUS_DIGEST_PORT") or 8184)
    if not 1 <= port <= 65_535:
        raise ValueError("校园日报端口无效")
    headers = {"X-Campus-Runtime-Key": runtime_key} if runtime_key else None
    timeout = httpx2.Timeout(21 if runtime_key else 8, connect=2)
    async with httpx2.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
        method = "GET" if payload is None else "POST"
        async with client.stream(method, f"http://127.0.0.1:{port}{path}", params=params, headers=headers, json=payload) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 64 * 1024:
                    raise ValueError("校园日报回复超过大小上限")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError("校园日报回复无效")
    return result


async def search_digest(**params):
    result = await digest_request("/api/campus-desk/search", params=params)
    if not isinstance(result.get("revision"), dict) or not isinstance(result.get("items"), list):
        raise ValueError("校园日报检索回复无效")
    return result


async def search_campus_digest(_context: SkillContext, args: CampusDigestInput) -> ToolResult:
    try:
        result = await search_digest(q=args.query, source=args.source, days=args.days, limit=args.limit)
    except (httpx2.HTTPError, httpx2.InvalidURL, ValueError, RecursionError):
        return ToolResult("校园日报暂时无法检索。", "error")
    return ToolResult(json.dumps(result, ensure_ascii=False, separators=(",", ":")), "success")


CAMPUS_DIGEST = Skill(
    "search_campus_digest",
    "查询中国传媒大学官网和微信公众号收录的通知、加分活动、讲座和报名消息。"
    "用简短关键词搜索，或留空查看近期条目。返回发布日、来源及可用原文链接。"
    "发布日期指文章发布，举办时间和报名截止以原文为准。stale 为 true 时说明索引可能尚未更新。",
    CampusDigestInput, search_campus_digest, read_only=True,
)
