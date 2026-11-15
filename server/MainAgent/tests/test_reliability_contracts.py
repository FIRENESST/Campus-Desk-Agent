import asyncio
import json
import unittest
from dataclasses import asdict

from main_agent.core import Message
from main_agent.reliability import (
    BUILTIN_RULES, Citation, Claim, Evidence, InvalidReviewError, JsonObjectCheck,
    LLMReviewer, ReliabilityConfig, Review, ReviewContext, ReviewPanel, Rule,
    RuleCheck, ToolResult,
)
from main_agent.reliability.evaluation import EvaluationCase, EvaluationScore, evaluate
from main_agent.reliability.checks import review_findings
from main_agent.reliability.review import parse_review


def passing(context):
    return Review(tuple(RuleCheck(rule.id, "pass", "符合规则") for rule in context.rules))


class ContractTests(unittest.TestCase):
    def test_review_rejects_invalid_json_schema_and_duplicate_keys(self):
        cases = (
            "not JSON", "```json\n{}\n```", '{"approved":true}',
            '{"rule_checks":[],"claims":[],"issues":[],"issues":[]}',
            '{"rule_checks":[],"claims":[],"issues":[NaN]}',
            '{"rule_checks":{},"claims":[],"issues":[]}',
            '{"rule_checks":[],"claims":[],"issues":[true]}',
            '{"rule_checks":[{"rule_id":"r","status":true,"reason":"ok"}],"claims":[],"issues":[]}',
        )
        for text in cases:
            with self.subTest(text=text), self.assertRaises(InvalidReviewError):
                parse_review(text)

    def test_review_accepts_exact_contract(self):
        review = Review((RuleCheck("r", "unknown", "缺少依据"),), (
            Claim("声明", "unsupported", "unsupported"),
        ))
        # An invalid kind is rejected even when constructed by a custom reviewer.
        with self.assertRaises(InvalidReviewError):
            parse_review(json.dumps(asdict(review)))
        valid = Review((RuleCheck("r", "pass", "依据明确"),), (
            Claim("声明", "fact", "supported", (Citation("doc", "原文"),)),
        ))
        self.assertEqual(parse_review(json.dumps(asdict(valid))), valid)

    def test_review_checks_compact_json_citations_against_the_cited_receipt(self):
        first = Evidence("tool:1", '{"items": [], "registration_deadline": null}', "search_campus_digest", "tool", "success")
        second = Evidence("tool:2", '{"items": [{"published_at": "2026-11-11T10:01:00"}]}',
                          "search_campus_digest", "tool", "success")
        answer = "报名截止未注明。"
        context = ReviewContext((), (), (first, second), answer)
        valid = Review((), (Claim(answer, "fact", "supported", (Citation(first.id, '"registration_deadline":null'),)),))
        self.assertEqual(review_findings(context, valid), ())
        wrong = Review((), (Claim(answer, "fact", "supported", (Citation(second.id, '"registration_deadline":null'),)),))
        self.assertEqual([finding.code for finding in review_findings(context, wrong)], ["invalid_citation"])

    def test_limits_and_evidence_metadata_are_validated(self):
        for name in (
            "max_repairs", "max_tool_rounds", "max_context_chars", "max_model_calls",
            "max_tool_calls", "max_tool_calls_per_batch", "max_evidence_items", "max_evidence_chars",
        ):
            for value in (-1, True, 1.5):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    ReliabilityConfig(**{name: value})
        for name in ("llm_timeout", "tool_timeout", "review_timeout", "total_timeout"):
            for value in (0, -1, True, float("nan"), float("inf")):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    ReliabilityConfig(**{name: value})
        for build in (
            lambda: Rule("", "规则"),
            lambda: Evidence("doc", "内容", ""),
            lambda: Evidence("doc", "内容", "来源", status="accepted"),
            lambda: ToolResult("内容", "accepted"),
            lambda: ToolResult("内容", expires_at=float("nan")),
            lambda: Evidence("doc", "内容", "来源", expires_at=True),
        ):
            with self.assertRaises(ValueError):
                build()

    def test_json_output_gate_rejects_duplicate_keys_nonfinite_values_and_missing_fields(self):
        for answer in ('{"answer":1,"answer":2}', '{"answer":NaN}', '[]', '{}'):
            context = ReviewContext((), (), (), answer)
            with self.subTest(answer=answer):
                self.assertTrue(JsonObjectCheck(("answer",)).check(context))


class ReviewerTests(unittest.IsolatedAsyncioTestCase):
    async def test_reviewer_must_not_return_tool_calls_or_invalid_role(self):
        class FakeLLM:
            async def complete(self, messages, tools):
                return Message("tool", "{}")

        with self.assertRaises(InvalidReviewError):
            await LLMReviewer(FakeLLM()).review(ReviewContext((), BUILTIN_RULES, (), "建议"))

    async def test_panel_keeps_any_rule_failure_and_claim_disagreement(self):
        context = ReviewContext((), BUILTIN_RULES, (Evidence("doc", "42", "记录"),), "42")

        class First:
            async def review(self, ctx):
                return Review(passing(ctx).rule_checks, (
                    Claim("42", "fact", "supported", (Citation("doc", "42"),)),
                ))

        class Second:
            async def review(self, ctx):
                checks = list(passing(ctx).rule_checks)
                checks[0] = RuleCheck(checks[0].rule_id, "unknown", "规则含义不明确")
                return Review(tuple(checks), (Claim("42", "fact", "unsupported"),))

        result = await ReviewPanel([First(), Second()]).review(context)
        self.assertEqual(result.rule_checks[0].status, "unknown")
        self.assertEqual(result.claims[0].status, "unsupported")

    async def test_panel_cancels_siblings_when_a_member_fails(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class Waiting:
            async def review(self, ctx):
                started.set()
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.set()

        class Failing:
            async def review(self, ctx):
                await started.wait()
                raise RuntimeError("评审失败")

        with self.assertRaises(ExceptionGroup):
            await ReviewPanel([Waiting(), Failing()]).review(ReviewContext((), BUILTIN_RULES, (), "建议"))
        self.assertTrue(cancelled.is_set())

    async def test_evaluation_counts_rejections_and_wrong_answers_in_denominator(self):
        cases = tuple(EvaluationCase(str(i), (Message("user", "问题"),)) for i in range(3))

        async def runner(case):
            if case.id == "1":
                raise RuntimeError("未交付")
            return "正确" if case.id == "0" else "错误"

        async def scorer(case, answer):
            return EvaluationScore(answer == "正确", answer == "正确", True)

        report = await evaluate(cases, runner, scorer)
        self.assertAlmostEqual(report.delivery_rate, 2 / 3)
        self.assertAlmostEqual(report.pass_rate, 1 / 3)
        self.assertIsNone(report.records[1].score)
        self.assertEqual(report.records[1].error_type, "RuntimeError")

    async def test_evaluation_does_not_treat_scorer_failure_as_success(self):
        async def runner(case):
            return "回答"

        async def scorer(case, answer):
            raise RuntimeError("无法判定")

        report = await evaluate([EvaluationCase("a", (Message("user", "问题"),))], runner, scorer)
        self.assertEqual(report.delivery_rate, 1)
        self.assertEqual(report.pass_rate, 0)


if __name__ == "__main__":
    unittest.main()
