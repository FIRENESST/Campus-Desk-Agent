"""Assess a campus claim through the local rumor-control server."""

import asyncio
import json
import os
import re
from datetime import date, datetime
from itertools import islice
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx2
from pydantic import BaseModel, Field, StringConstraints, field_validator

from ...reliability.models import ToolResult
from ..action_plan import CAMPUS_TIME_ZONE
from ..skills import Skill, SkillContext, SkillInput


RUMOR_SERVICE_PORT = 8100
_MAX_RESPONSE_BYTES = 256 * 1024
_SECRET = re.compile(r"[A-Za-z0-9_-]{32,128}")
_USERNAME = re.compile(r"[A-Za-z0-9_.@-]{1,64}")
_VERDICT_RELATIONS = {
    "supported": {"supported"}, "refuted": {"refuted"}, "conflicting": {"supported", "refuted", "conflicting"},
}


class CampusRumorInput(SkillInput):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16_000)] = Field(
        description="同一话题中待核的校园说法，保留原文、日期、校区及人群范围，仅传当前说法。"
    )
    published: str | None = Field(
        default=None, max_length=64, description="转发原文的发布日期或发布时间，ISO 8601，未知时省略。",
    )

    @field_validator("published")
    @classmethod
    def validate_published(cls, value):
        if value is not None:
            if len(value) == 10:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError("发布时间须为 ISO 日期或日期时间")
            elif not re.match(r"^\d{4}-\d{2}-\d{2}[T ]", value):
                raise ValueError("发布时间须为 ISO 日期或日期时间")
            else:
                datetime.fromisoformat(value)
        return value


class _Claim(BaseModel):
    key: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    excerpt: Annotated[str, Field(min_length=1, max_length=1000)]
    subject: str
    event: str
    stance: str
    days: list[Annotated[str, Field(max_length=16)]]
    priority: int
    external_school: bool = False
    uncertain_scope: bool = False
    unresolved_time: bool = False


class _Assessment(BaseModel):
    analysis_version: str
    max_analysis_chars: int
    kind: Literal["rumor_candidate", "discussion", "demand"]
    topic: str
    priority: int
    reason: str
    claims: list[_Claim]


async def _request(client, method, path, **kwargs):
    async with client.stream(method, path, **kwargs) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError("谣言判别回执超过大小上限")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("谣言判别回执格式无效")
    return value


def _official_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(char.isspace() for char in value):
        return False
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        domains = tuple(item.strip().lower() for item in os.getenv("CLOUD_RUMOR_OFFICIAL_DOMAINS", "").split(",") if item.strip())
        return (parts.scheme == "https" and parts.username is None and parts.path not in {"", "/"}
                and parts.port in {None, 443}
                and any(host == domain or host.endswith("." + domain) for domain in domains))
    except ValueError:
        return False


def official_evidence(entry):
    if not isinstance(entry, dict):
        return False
    url = entry.get("url")
    origin = entry.get("provenance")
    account = re.fullmatch(r"https://weibo\.com/([0-9]{1,20})/[A-Za-z0-9]{8,16}", url) if isinstance(url, str) else None
    allowed_accounts = {item.strip() for item in os.getenv("CLOUD_RUMOR_OFFICIAL_WEIBO_IDS", "").split(",")}
    if not (origin in ("official_article", "operator_excerpt") and (
        _official_url(url) or origin == "operator_excerpt" and account and account[1] in allowed_accounts
    )):
        return False
    if not isinstance(entry.get("quote"), str) or not 1 <= len(entry["quote"].strip()) <= 12_000:
        return False
    try:
        return date.fromisoformat(entry["published"]).isoformat() == entry["published"]
    except (KeyError, TypeError, ValueError):
        return False


async def _stored_review(client, claim):
    try:
        async with asyncio.timeout(3):
            record = await _request(client, "GET", f"/internal/rumor-review/{claim.key}")
    except (TimeoutError, httpx2.HTTPError, ValueError, RecursionError):
        return None
    verdict = record.get("verdict")
    sources = record.get("sources")
    if record.get("id") != claim.key or verdict not in ("supported", "refuted") or not isinstance(sources, list):
        return None
    evidence = [item for item in sources if official_evidence(item) and item.get("relation") == verdict]
    if evidence:
        return {"claim_key": claim.key, "state": verdict, "official_evidence": evidence[:3], "reviewed_on": record.get("reviewed_on")}
    return None


async def _verify_registered_claim(client, claim):
    try:
        async with asyncio.timeout(13):
            result = await _request(client, "POST", f"/api/rumor_sources/{claim.key}")
    except httpx2.HTTPStatusError as exc:
        state = {404: "not_in_monitoring_queue", 409: "pending_review"}.get(exc.response.status_code, "unavailable")
        return {"claim_key": claim.key, "state": state}
    except (TimeoutError, httpx2.HTTPError, ValueError, RecursionError):
        return {"claim_key": claim.key, "state": "unavailable"}
    state = result.get("state")
    entries = result.get("verification")
    if state not in ("checked", "partial") or not isinstance(entries, list):
        return {"claim_key": claim.key, "state": state if state in ("disabled", "busy", "outside_scope") else "unavailable"}
    match = next((entry for entry in entries if isinstance(entry, dict) and entry.get("key") == claim.key), None)
    verdict = match.get("status") if match else None
    if not isinstance(verdict, str) or verdict not in _VERDICT_RELATIONS or match.get("pending_revisions"):
        return {"claim_key": claim.key, "state": "insufficient_evidence"}
    evidence = match.get("evidence")
    if not isinstance(evidence, list):
        return {"claim_key": claim.key, "state": "insufficient_evidence"}
    valid = [
        {field: entry[field] for field in ("relation", "url", "published", "quote", "provenance")}
        for entry in evidence if official_evidence(entry) and isinstance(entry.get("relation"), str)
        and entry["relation"] in _VERDICT_RELATIONS[verdict]
    ]
    by_relation = {}
    for entry in valid:
        by_relation.setdefault(entry["relation"], entry)
    if not by_relation or (verdict == "conflicting" and not ({"supported", "refuted"} <= by_relation.keys()
                                                             or "conflicting" in by_relation)):
        return {"claim_key": claim.key, "state": "insufficient_evidence"}
    return {"claim_key": claim.key, "state": verdict, "official_evidence": list(by_relation.values())}


def _result(payload, status):
    return ToolResult(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False), status)


async def assess_campus_rumor(_context: SkillContext, args: CampusRumorInput) -> ToolResult:
    username = os.getenv("MAIN_AGENT_RUMOR_USER", "")
    password = os.getenv("MAIN_AGENT_RUMOR_PASSWORD", "")
    if not _USERNAME.fullmatch(username) or not _SECRET.fullmatch(password):
        return _result({"state": "not_configured"}, "error")
    try:
        port = int(os.getenv("CAMPUS_RUMOR_PORT") or RUMOR_SERVICE_PORT)
        if not 1 <= port <= 65_535:
            raise ValueError
        async with httpx2.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", auth=(username, password),
            headers={"X-Campus-Request": "1", "Origin": os.getenv("MAIN_AGENT_RUMOR_ORIGIN") or f"http://127.0.0.1:{port}"},
            trust_env=False, follow_redirects=False,
            timeout=httpx2.Timeout(10, connect=2),
        ) as client:
            published = args.published or datetime.now(CAMPUS_TIME_ZONE).date().isoformat()
            async with asyncio.timeout(8):
                assessment = _Assessment.model_validate(
                    await _request(client, "POST", "/api/assess", json={"text": args.text, "published": published}),
                    strict=True,
                )
            claims = assessment.claims
            verification = []
            candidates = {claim.key: claim for claim in claims if claim.stance == "candidate" and not claim.external_school}
            if candidates:
                try:
                    async with asyncio.timeout(16):
                        for claim in islice(candidates.values(), 2):
                            recorded = await _stored_review(client, claim)
                            verification.append(recorded or await _verify_registered_claim(client, claim))
                except TimeoutError:
                    pass
    except httpx2.HTTPStatusError as exc:
        state = "access_denied" if exc.response.status_code in {401, 403} else "unavailable"
        return _result({"state": state}, "error")
    except (TimeoutError, httpx2.HTTPError, ValueError, RecursionError):
        return _result({"state": "unavailable"}, "error")
    return _result({
        "state": "assessed", "assessment": assessment.model_dump(), "verification": verification,
        "reference_date": published, "source_published": args.published is not None,
    }, "success")


CAMPUS_RUMOR = Skill(
    "assess_campus_rumor",
    "把待核的校园传闻、突发情况或显著变动交给谣言防控服务器，提取主张和复核优先级。"
    "对服务器监测库里已有的同一主张，读取后台的复核结论、官方对照和原文证据。"
    "待复核标签、优先级和帖子摘录只描述线索。没有匹配本次主张和日期的官方依据时，需要补查资料。"
    "转发内容带原发布时间时传入 published，未知时保留原话中的时间表达。",
    CampusRumorInput, assess_campus_rumor, read_only=True,
)
