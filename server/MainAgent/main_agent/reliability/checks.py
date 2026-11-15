"""Deterministic gates complement, rather than replace, semantic review."""

import json
import time
from dataclasses import dataclass

from .models import Finding, InvalidReviewError, Review, ReviewContext
from .review import receipt_has_quote


def review_findings(context: ReviewContext, review: Review) -> tuple[Finding, ...]:
    expected = {rule.id for rule in context.rules}
    actual = [check.rule_id for check in review.rule_checks]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise InvalidReviewError("评审必须恰好覆盖每一条规则，不能遗漏、重复或添加规则")
    findings = []
    for check in review.rule_checks:
        if check.status != "pass":
            findings.append(Finding("rule_not_satisfied", check.reason, check.rule_id))
    evidence = {item.id: item for item in context.evidence}
    seen_claims = set()
    for claim in review.claims:
        if claim.text not in context.answer or claim.text in seen_claims:
            findings.append(Finding("invalid_claim_anchor", "评审声明未能唯一对应答案原文，请核对或简化相关表述。"))
            continue
        seen_claims.add(claim.text)
        if claim.status != "supported":
            findings.append(Finding("ungrounded_claim", f"声明缺少支持或与证据冲突：{claim.text}"))
        if claim.status == "supported" and not claim.citations:
            findings.append(Finding("missing_evidence", f"声明没有引用证据：{claim.text}"))
        valid_sources = []
        for citation in claim.citations:
            source = evidence.get(citation.evidence_id)
            if source is None or not (
                receipt_has_quote(source.content, citation.quote) if source.kind == "tool" else citation.quote in source.content
            ):
                findings.append(Finding("invalid_citation", f"引用的 ID 或原文无法核对：{citation.evidence_id}"))
            elif source.expires_at is not None and source.expires_at <= time.time():
                findings.append(Finding("expired_evidence", f"引用的证据已经过期：{source.id}"))
            else:
                valid_sources.append(source)
        if claim.kind == "action" and not any(
            item.kind == "tool" and item.status == "success" for item in valid_sources
        ):
            findings.append(Finding("unconfirmed_action", f"操作完成声明没有明确的成功回执：{claim.text}"))
    findings.extend(Finding("review_issue", message) for message in review.issues)
    return tuple(findings)


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复 JSON 字段")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("非法 JSON 数值")


@dataclass(frozen=True)
class JsonObjectCheck:
    required_fields: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.required_fields, tuple) or any(
            not isinstance(name, str) or not name for name in self.required_fields
        ):
            raise ValueError("required_fields 必须是字段名元组")

    def check(self, context: ReviewContext) -> tuple[Finding, ...]:
        try:
            payload = json.loads(
                context.answer, object_pairs_hook=_unique_keys, parse_constant=_invalid_constant
            )
            if not isinstance(payload, dict) or set(self.required_fields) - set(payload):
                raise ValueError("缺少字段或不是对象")
        except (ValueError, RecursionError):
            return (Finding("output_format", "答案必须是包含所有必填字段的 JSON 对象，且字段不能重复"),)
        return ()


@dataclass(frozen=True)
class MaxLengthCheck:
    max_chars: int

    def __post_init__(self):
        if isinstance(self.max_chars, bool) or not isinstance(self.max_chars, int) or self.max_chars < 1:
            raise ValueError("max_chars 必须是正整数")

    def check(self, context: ReviewContext) -> tuple[Finding, ...]:
        if len(context.answer) > self.max_chars:
            return (Finding("output_length", f"答案超过 {self.max_chars} 字符限制"),)
        return ()
