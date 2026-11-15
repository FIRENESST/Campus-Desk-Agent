import json
import math
import time
from dataclasses import asdict
from typing import Any

from ..core import LLMClient, Message
from .models import (
    Citation,
    Claim,
    ContextLimitExceeded,
    InvalidReviewError,
    Review,
    ReviewContext,
    RuleCheck,
)
from .prompts import REVIEW_INSTRUCTIONS


def context_payload(context: ReviewContext) -> dict[str, Any]:
    receipts = {(item.call_id, item.content) for item in context.evidence if item.kind == "tool" and item.call_id}
    return {
        "evaluated_at": time.time(),
        "messages": [asdict(message) for message in context.messages
                     if message.role != "tool" or (message.tool_call_id, message.content) not in receipts],
        "rules": [asdict(rule) for rule in context.rules],
        "evidence": [
            {**asdict(item), "content": structured_receipt(item.content)} if item.kind == "tool" else asdict(item)
            for item in context.evidence
        ],
        "answer": context.answer,
    }


async def complete_review_json(llm: LLMClient, messages: list[Message]) -> Message:
    complete_json = getattr(llm, "complete_json", None)
    if callable(complete_json):
        return await complete_json(messages)
    return await llm.complete(messages, [])


def bounded_json(payload: Any, limit: int) -> str:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(text) > limit:
        raise ContextLimitExceeded("核验上下文超过字符预算；未静默截断规则或证据")
    return text


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidReviewError("评审 JSON 包含重复字段")
        result[key] = value
    return result


def _invalid_constant(value):
    raise InvalidReviewError("评审 JSON 包含非法数值")


def _finite_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("非有限数值")
    return number


def structured_receipt(content: str) -> str | dict | list:
    if not content.lstrip().startswith(("{", "[")):
        return content
    try:
        return json.loads(content, object_pairs_hook=_unique_object, parse_constant=_invalid_constant,
                          parse_float=_finite_number)
    except (ValueError, InvalidReviewError, RecursionError):
        return content


def receipt_has_quote(content: str, quote: str) -> bool:
    if quote in content:
        return True
    receipt = structured_receipt(content)
    if not isinstance(receipt, (dict, list)):
        return False
    # Review input serializes structured receipts without whitespace between JSON fields.
    if quote in json.dumps(receipt, ensure_ascii=False, separators=(",", ":")):
        return True
    values = [receipt]
    while values:
        value = values.pop()
        if isinstance(value, str) and quote in value:
            return True
        if isinstance(value, dict):
            values.extend(value.values())
        elif isinstance(value, list):
            values.extend(value)
    return False


def _object(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise InvalidReviewError("评审 JSON 字段不符合契约")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise InvalidReviewError("评审 JSON 文本字段无效")
    return value


def _array(value):
    if not isinstance(value, list) or len(value) > 256:
        raise InvalidReviewError("评审 JSON 数组无效或超过上限")
    return value


def parse_review(text: str) -> Review:
    if not isinstance(text, str) or len(text) > 100_000:
        raise InvalidReviewError("评审响应无效或超过长度上限")
    try:
        payload = json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
    except (ValueError, RecursionError) as exc:
        raise InvalidReviewError("评审响应不是合法 JSON") from exc
    payload = _object(payload, ("rule_checks", "claims", "issues"))
    checks = []
    for item in _array(payload["rule_checks"]):
        item = _object(item, ("rule_id", "status", "reason"))
        if item["status"] not in ("pass", "fail", "unknown"):
            raise InvalidReviewError("评审规则状态无效")
        checks.append(RuleCheck(_text(item["rule_id"]), item["status"], _text(item["reason"])))
    claims = []
    for item in _array(payload["claims"]):
        item = _object(item, ("text", "kind", "status", "citations"))
        if item["kind"] not in ("fact", "action") or item["status"] not in (
            "supported", "unsupported", "contradicted"
        ):
            raise InvalidReviewError("评审声明类型或状态无效")
        citations = []
        for citation in _array(item["citations"]):
            citation = _object(citation, ("evidence_id", "quote"))
            citations.append(Citation(_text(citation["evidence_id"]), _text(citation["quote"])))
        claims.append(Claim(_text(item["text"]), item["kind"], item["status"], tuple(citations)))
    return Review(tuple(checks), tuple(claims), tuple(_text(x) for x in _array(payload["issues"])))


class LLMReviewer:
    """Tool-free review with a bounded retry for malformed review contracts."""

    def __init__(
        self, llm: LLMClient, *, max_context_chars: int = 120_000,
        max_format_retries: int = 1,
    ):
        if isinstance(max_context_chars, bool) or not isinstance(max_context_chars, int) or max_context_chars < 1:
            raise ValueError("max_context_chars 必须是正整数")
        self.llm = llm
        self.max_context_chars = max_context_chars
        if isinstance(max_format_retries, bool) or not isinstance(max_format_retries, int) or max_format_retries < 0:
            raise ValueError("max_format_retries 必须是非负整数")
        self.max_format_retries = max_format_retries

    def bind_runtime(self, runtime):
        return LLMReviewer(
            runtime.model(self.llm, "review"),
            max_context_chars=min(self.max_context_chars, runtime.config.max_context_chars),
            max_format_retries=self.max_format_retries,
        )

    async def review(self, context: ReviewContext) -> Review:
        from .checks import review_findings

        payload = context_payload(context)
        for attempt in range(self.max_format_retries + 1):
            response = await complete_review_json(self.llm, [
                Message("system", REVIEW_INSTRUCTIONS),
                Message("user", bounded_json(payload, self.max_context_chars)),
            ])
            if not isinstance(response, Message) or response.role != "assistant" or response.tool_calls:
                raise InvalidReviewError("评审器必须返回无工具调用的 assistant 消息")
            try:
                review = parse_review(response.content)
                findings = review_findings(context, review)
            except InvalidReviewError as exc:
                if attempt == self.max_format_retries:
                    raise
                payload["review_format_feedback"] = f"前次评审未通过格式校验。{exc}。请重新完整评审，保持严格 JSON。"
                continue
            if attempt == self.max_format_retries or not any(item.code == "invalid_claim_anchor" for item in findings):
                return review
            payload["review_format_feedback"] = (
                "前次有声明改写、拼接或重复。逐字复制 answer 中的连续原文片段，保留原标点和 Markdown。"
                "将重复项合并，每段声明都要核对证据，缺乏支持的部分标为 unsupported。"
            )
        raise AssertionError("unreachable")
