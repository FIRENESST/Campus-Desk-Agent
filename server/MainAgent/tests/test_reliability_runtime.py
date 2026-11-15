import asyncio
import json
import time
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from main_agent.core import AgentError, AgentTimeoutError, Message, ToolCall, ToolSpec
from main_agent.reliability import (
    BUILTIN_RULES, BudgetExceeded, Citation, Claim, ContextLimitExceeded, Evidence,
    InvalidDraftError, InvalidReviewError, LLMReviewer, ReliabilityConfig,
    ReliableAgent, Review, ReviewContext, ReviewPanel, RuleCheck,
    ToolEvidenceRetriever, ToolResult, VerificationFailed,
)
from main_agent.reliability.checks import review_findings
from main_agent.reliability.evaluation import EvaluationCase, EvaluationScore, evaluate
from main_agent.reliability.tools import RecordingTools


class ModelScript:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append((deepcopy(messages), deepcopy(tools)))
        response = self.replies.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class Tools:
    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []
        self.list_calls = 0

    async def list_tools(self):
        self.list_calls += 1
        return [ToolSpec(name, name, {"type": "object"}) for name in self.results]

    async def call_tool(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        result = self.results[name]
        if isinstance(result, Exception):
            raise result
        return result


def approved(context, claims=()):
    return Review(tuple(RuleCheck(rule.id, "pass", "符合") for rule in context.rules), tuple(claims))


class Reviewer:
    async def review(self, context):
        return approved(context)


class ReviewModel:
    def __init__(self, claims=None):
        self.claims = claims
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append((deepcopy(messages), deepcopy(tools)))
        payload = json.loads(messages[-1].content)
        return Message("assistant", json.dumps({
            "rule_checks": [{"rule_id": rule["id"], "status": "pass", "reason": "符合"} for rule in payload["rules"]],
            "claims": self.claims(payload) if self.claims else [],
            "issues": [],
        }, ensure_ascii=False))


def fact_claim(payload):
    evidence = payload["evidence"]
    return [{
        "text": payload["answer"], "kind": "fact",
        "status": "supported" if evidence else "unsupported",
        "citations": [{"evidence_id": evidence[-1]["id"], "quote": evidence[-1]["content"]}] if evidence else [],
    }]


class ArchitectureRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_entire_tool_batch_is_checked_before_first_execution(self):
        tools = Tools({"first": "第一次操作", "denied": "不能执行"})
        model = ModelScript([Message("assistant", tool_calls=[
            ToolCall("a", "first", {}), ToolCall("b", "denied", {}),
        ])])

        async def guard(name, arguments):
            if name == "denied":
                raise AgentError("拒绝第二项")

        with self.assertRaises(AgentError):
            await ReliableAgent(model, tools, Reviewer(), tool_guard=guard).run([Message("user", "处理")])
        self.assertEqual(tools.calls, [])

    async def test_panel_cannot_erase_a_members_missing_evidence(self):
        context = ReviewContext((), BUILTIN_RULES, (Evidence("doc", "42", "记录"),), "42")

        class Missing:
            async def review(self, ctx):
                return approved(ctx, (Claim("42", "fact", "supported"),))

        class Supported:
            async def review(self, ctx):
                return approved(ctx, (Claim("42", "fact", "supported", (Citation("doc", "42"),)),))

        review = await ReviewPanel([Missing(), Supported()]).review(context)
        self.assertTrue(review_findings(context, review))

    async def test_evaluation_cannot_mutate_its_own_reference_label(self):
        case = EvaluationCase("case", (Message("user", "正确标签"),))

        async def runner(received):
            received.messages[0].content = "错误答案"
            return "错误答案"

        async def scorer(received, answer):
            correct = answer == received.messages[0].content
            return EvaluationScore(correct, correct, True)

        report = await evaluate([case], runner, scorer)
        self.assertEqual(report.pass_rate, 0)
        self.assertEqual(case.messages[0].content, "正确标签")


class RetrievalReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_retains_original_citation_and_tool_call_id(self):
        title, quote = "本科新生报到手册", "请携带录取通知书。"
        receipt = json.dumps({"hits": [{"source": title, "text": quote}]}, ensure_ascii=False)
        tools = Tools({"read": ToolResult(receipt, "success", reuse_for=60)})
        checked = []

        async def guard(name, arguments):
            checked.append((name, arguments))

        def cite_real_receipt(payload):
            evidence, = payload["evidence"]
            self.assertEqual(evidence["content"]["hits"][0]["source"], title)
            self.assertEqual(evidence["call_id"], "first")
            return [{"text": quote, "kind": "fact", "status": "supported",
                     "citations": [{"evidence_id": evidence["id"], "quote": quote}]}]

        arguments = {"query": "新生报到材料", "limit": 5}
        model = ModelScript([
            Message("assistant", tool_calls=[
                ToolCall("first", "read", arguments),
                ToolCall("repeat-in-batch", "read", {"limit": 5, "query": arguments["query"]}),
            ]),
            Message("assistant", tool_calls=[ToolCall("repeat-next-batch", "read", arguments)]),
            Message("assistant", quote),
        ])
        result = await ReliableAgent(model, tools, LLMReviewer(ReviewModel(cite_real_receipt)), tool_guard=guard).run([
            Message("user", "报到要带什么？"),
        ])

        self.assertEqual(tools.calls, [("read", arguments)])
        self.assertEqual(checked, [("read", arguments)] * 3)
        self.assertEqual(result.stats.total_tool_executions, 1)
        self.assertEqual(sum(event.kind == "tool.reused" for event in result.snapshot.events), 2)
        replies = [message for message in result.history if message.role == "tool"]
        self.assertEqual([reply.tool_call_id for reply in replies], ["first", "repeat-in-batch", "repeat-next-batch"])
        self.assertEqual(replies[0].content, receipt)
        self.assertEqual([json.loads(reply.content)["reused_tool_call_id"] for reply in replies[1:]], ["first"] * 2)

    async def test_reuses_still_count_toward_logical_call_budget(self):
        tools = Tools({"read": ToolResult("资料", "success", reuse_for=60)})
        model = ModelScript([
            Message("assistant", tool_calls=[ToolCall(call_id, "read", {})])
            for call_id in ("read-1", "read-2", "read-3")
        ])
        with self.assertRaises(BudgetExceeded) as failure:
            await ReliableAgent(model, tools, Reviewer(), config=ReliabilityConfig(max_tool_calls=2)).run([
                Message("user", "查询"),
            ])
        self.assertEqual(failure.exception.resource, "tool_calls")
        self.assertEqual(len(tools.calls), 1)
        self.assertEqual(sum(event.kind == "tool.reused" for event in failure.exception.snapshot.events), 1)

    async def test_failed_unknown_or_expired_receipts_are_retried(self):
        clock = SimpleNamespace(time=lambda: 100, monotonic=lambda: 10)
        adapter = Tools({"read": ToolResult("检索超时", "error", reuse_for=5)})
        recorded = RecordingTools(adapter, allowed_tools=None, guard=None, interpreter=None)
        await recorded.list_tools()
        with patch("main_agent.reliability.tools.time", clock):
            self.assertEqual(await recorded.call_tool("read", {}), "检索超时")
            adapter.results["read"] = ToolResult("状态未知", reuse_for=5)
            self.assertEqual(await recorded.call_tool("read", {}), "状态未知")
            adapter.results["read"] = ToolResult("证据过期", "success", expires_at=99, reuse_for=5)
            self.assertEqual(await recorded.call_tool("read", {}), "证据过期")
            adapter.results["read"] = ToolResult("最新资料", "success", reuse_for=5)
            self.assertEqual(await recorded.call_tool("read", {}), "最新资料")
            clock.monotonic = lambda: 16
            adapter.results["read"] = ToolResult("更新后的资料", "success", reuse_for=5)
            self.assertEqual(await recorded.call_tool("read", {}), "更新后的资料")
        self.assertEqual(len(adapter.calls), 5)

    async def test_mutation_preserves_execution_order_and_invalidates_previous_reads(self):
        class Service(Tools):
            async def call_tool(self, name, arguments):
                if name == "write":
                    self.results["read"] = ToolResult("已占用", "success", reuse_for=60)
                return await super().call_tool(name, arguments)

        tools = Service({"read": ToolResult("空闲", "success", reuse_for=60), "write": ToolResult("已确认", "success")})
        model = ModelScript([
            Message("assistant", tool_calls=[ToolCall(f"call-{index}", name, {})
                                             for index, name in enumerate(("read", "write", "read"))]),
            Message("assistant", "预约已确认。"),
        ])
        result = await ReliableAgent(model, tools, Reviewer()).run([Message("user", "检查后预约")])
        self.assertEqual([name for name, _ in tools.calls], ["read", "write", "read"])
        self.assertEqual([item.content for item in result.evidence], ["空闲", "已确认", "已占用"])

    async def test_concurrent_runs_do_not_share_identical_requests(self):
        class Model:
            async def complete(self, messages, tools):
                if messages[-1].role == "tool":
                    return Message("assistant", "完成")
                return Message("assistant", tool_calls=[ToolCall("a", "read", {}), ToolCall("b", "read", {})])

        class Read(Tools):
            async def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                count = len(self.calls)
                await asyncio.sleep(0)
                return ToolResult(f"receipt-{count}", "success", reuse_for=60)

        tools = Read({"read": None})
        agent = ReliableAgent(Model(), tools, Reviewer())
        results = await asyncio.gather(*(agent.run([Message("user", user)]) for user in ("甲", "乙")))
        self.assertEqual(len(tools.calls), 2)
        self.assertEqual({tuple(item.content for item in result.evidence) for result in results},
                         {("receipt-1",), ("receipt-2",)})


class RuntimeBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_model_budget_never_calls_the_model(self):
        model = ModelScript([])
        with self.assertRaises(BudgetExceeded) as failure:
            await ReliableAgent(model, Tools(), Reviewer(), config=ReliabilityConfig(max_model_calls=0)).run([Message("user", "问题")])
        self.assertEqual(model.calls, [])
        self.assertEqual(failure.exception.snapshot.stats.model_calls, 0)

    async def test_oversized_model_response_is_rejected_before_any_followup(self):
        model = ModelScript([Message("assistant", "A" * 10_000)])
        with self.assertRaises(ContextLimitExceeded) as failure:
            await ReliableAgent(model, Tools(), Reviewer(), config=ReliabilityConfig(max_context_chars=6000)).run([Message("user", "建议")])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(failure.exception.snapshot.stats.model_calls, 1)
        self.assertEqual(failure.exception.snapshot.stats.review_rounds, 0)

    async def test_uninstrumented_custom_components_are_identified_in_audit(self):
        result = await ReliableAgent(ModelScript([Message("assistant", "建议检查输入。")]), Tools(), Reviewer()).run([Message("user", "建议")])
        external = [event for event in result.snapshot.events if event.kind == "component.external"]
        self.assertEqual([event.name for event in external], ["Reviewer"])
        self.assertEqual(result.stats.model_calls, 1)

    async def test_parallel_reviewers_share_one_model_call_budget(self):
        first, second = ReviewModel(), ReviewModel()
        agent = ReliableAgent(
            ModelScript([Message("assistant", "建议检查输入。")]), Tools(),
            ReviewPanel([LLMReviewer(first), LLMReviewer(second)]),
            config=ReliabilityConfig(max_model_calls=2),
        )
        with self.assertRaises(BudgetExceeded) as failure:
            await agent.run([Message("user", "给建议")])
        self.assertEqual(failure.exception.resource, "model_calls")
        self.assertEqual(failure.exception.snapshot.stats.model_calls, 2)
        self.assertEqual(len(first.calls) + len(second.calls), 1)
        self.assertEqual(failure.exception.snapshot.phase, "failed")

    async def test_all_builtin_review_calls_are_counted(self):
        result = await ReliableAgent(
            ModelScript([Message("assistant", "建议核对输入。")]), Tools(),
            ReviewPanel([LLMReviewer(ReviewModel()), LLMReviewer(ReviewModel())]),
        ).run([Message("user", "给建议")])
        self.assertEqual(result.stats.model_calls, 3)
        self.assertEqual(result.stats.review_model_calls, 2)
        self.assertEqual(result.stats.review_rounds, 1)
        self.assertEqual(result.snapshot.phase, "approved")

    async def test_retrieval_cannot_exceed_shared_model_budget(self):
        tools = Tools({"read": "票价 20 元"})
        lookup = ModelScript([
            Message("assistant", tool_calls=[ToolCall("lookup", "read", {})]),
            Message("assistant", "查询结束"),
        ])
        agent = ReliableAgent(
            ModelScript([Message("assistant", "票价 99 元")]), tools,
            LLMReviewer(ReviewModel(fact_claim)),
            retriever=ToolEvidenceRetriever(lookup, tools, allowed_tools={"read"}),
            config=ReliabilityConfig(max_model_calls=3),
        )
        with self.assertRaises(BudgetExceeded) as failure:
            await agent.run([Message("user", "票价？")])
        snapshot = failure.exception.snapshot
        self.assertEqual(snapshot.stats.model_calls, 3)
        self.assertEqual(snapshot.stats.retrieval_model_calls, 1)
        self.assertEqual(snapshot.stats.total_tool_executions, 1)
        self.assertEqual(snapshot.evidence[0].id, "lookup:1:1")
        self.assertEqual(len(lookup.calls), 1)

    async def test_tool_budget_is_reserved_for_the_whole_batch(self):
        for config, resource in (
            (ReliabilityConfig(max_tool_calls=1), "tool_calls"),
            (ReliabilityConfig(max_tool_calls_per_batch=1), "tool_calls_per_batch"),
        ):
            tools = Tools({"first": "1", "second": "2"})
            model = ModelScript([Message("assistant", tool_calls=[
                ToolCall("a", "first", {}), ToolCall("b", "second", {}),
            ])])
            with self.subTest(resource=resource), self.assertRaises(BudgetExceeded) as failure:
                await ReliableAgent(model, tools, Reviewer(), config=config).run([Message("user", "处理")])
            self.assertEqual(tools.calls, [])
            self.assertEqual(failure.exception.resource, resource)
            self.assertEqual(failure.exception.snapshot.executions, ())

    async def test_tool_limits_leave_a_reviewable_draft_from_existing_evidence(self):
        answer = "本科生报到时应凭录取通知书和有效身份证件。"
        first = Message("assistant", tool_calls=[ToolCall("first", "lookup", {"query": "入学与注册"})])
        more = Message("assistant", tool_calls=[ToolCall("more", "lookup", {"query": "补充说明"})])
        for config, further_requests in (
            (ReliabilityConfig(max_tool_rounds=1), []),
            (ReliabilityConfig(max_tool_calls=1), [more]),
        ):
            with self.subTest(config=config):
                model = ModelScript([first, *further_requests, Message("assistant", answer)])
                tools = Tools({"lookup": ToolResult(answer, "success")})
                reviewer = ReviewModel(fact_claim)
                agent = ReliableAgent(model, tools, LLMReviewer(reviewer), config=config, finalize_on_tool_limit=True)
                result = await agent.run([Message("user", "新生要准备什么材料？请给官方依据。")])
                self.assertEqual(tools.calls, [("lookup", {"query": "入学与注册"})])
                final_history, available_tools = model.calls[-1]
                self.assertEqual(available_tools, [])
                self.assertEqual([(m.content, m.tool_call_id) for m in final_history if m.role == "tool"], [(answer, "first")])
                self.assertEqual(result.answer, answer)
                self.assertEqual(result.stats.review_rounds, 1)
                self.assertEqual(result.snapshot.phase, "approved")

    async def test_final_repair_starts_from_sources_after_rejected_drafts(self):
        answer = "本科新生报到须携带有效身份证件。"
        drafts = ("全校新生都需要携带体检表。", "全校新生还需提交疫苗接种记录。")
        model = ModelScript([Message("assistant", content) for content in (*drafts, answer)])

        def claims(payload):
            if payload["answer"] == answer:
                return fact_claim(payload)
            return [{"text": payload["answer"], "kind": "fact", "status": "unsupported", "citations": []}]

        class ReviewerWithMalformedReply(ReviewModel):
            async def complete(self, messages, tools):
                if json.loads(messages[-1].content)["answer"] == drafts[0]:
                    return Message("assistant", "评审未生成 JSON")
                return await super().complete(messages, tools)

        reviewer = LLMReviewer(ReviewerWithMalformedReply(claims), max_format_retries=0)
        agent = ReliableAgent(model, Tools(), reviewer, final_repair_instruction="按来源重新作答。")
        result = await agent.run([Message("user", "报到需要哪些材料？")], references=[Evidence("rules", answer, "学校规定")])
        clean_input = json.loads(model.calls[-1][0][-1].content)
        self.assertNotIn("answer", clean_input)
        self.assertEqual(clean_input["evidence"][0]["content"], answer)
        self.assertEqual(result.answer, answer)
        self.assertEqual(result.stats.review_rounds, 3)
        self.assertEqual(result.attempts[0].findings[0].code, "invalid_review")

    async def test_nonfinite_arguments_stop_the_batch_before_any_tool_executes(self):
        for arguments in ({"amount": float("nan")}, {"nested": {1: "invalid key"}}):
            tools = Tools({"first": "1", "second": "2"})
            model = ModelScript([Message("assistant", tool_calls=[
                ToolCall("a", "first", {}), ToolCall("b", "second", arguments),
            ])])
            with self.subTest(arguments=arguments), self.assertRaises(InvalidDraftError):
                await ReliableAgent(model, tools, Reviewer()).run([Message("user", "处理")])
            self.assertEqual(tools.calls, [])

    async def test_evidence_limit_stops_after_receipt_and_keeps_execution_outcome(self):
        tools = Tools({"write": ToolResult("很长的成功业务回执" * 10, "success")})
        model = ModelScript([Message("assistant", tool_calls=[ToolCall("save", "write", {"id": 1})])])
        agent = ReliableAgent(model, tools, Reviewer(), config=ReliabilityConfig(max_evidence_chars=5))
        with self.assertRaises(BudgetExceeded) as failure:
            await agent.run([Message("user", "保存")])
        snapshot = failure.exception.snapshot
        self.assertEqual(len(tools.calls), 1)
        self.assertEqual(snapshot.executions[0].state, "completed")
        self.assertEqual(snapshot.executions[0].outcome, "success")
        self.assertEqual(snapshot.evidence, ())
        self.assertEqual(len(model.calls), 1)

    async def test_reference_limit_fails_before_tool_discovery(self):
        tools = Tools()
        model = ModelScript([])
        agent = ReliableAgent(model, tools, Reviewer(), config=ReliabilityConfig(max_evidence_items=1))
        with self.assertRaises(BudgetExceeded):
            await agent.run([Message("user", "问题")], references=[
                Evidence("one", "1", "资料"), Evidence("two", "2", "资料"),
            ])
        self.assertEqual(tools.list_calls, 0)
        self.assertEqual(model.calls, [])

    async def test_expired_citations_trigger_fresh_evidence_collection(self):
        tools = Tools({"read": "票价 20 元"})
        lookup = ModelScript([
            Message("assistant", tool_calls=[ToolCall("look", "read", {})]),
            Message("assistant", "查询结束"),
        ])
        result = await ReliableAgent(
            ModelScript([Message("assistant", "票价 10 元"), Message("assistant", "票价 20 元")]),
            tools, LLMReviewer(ReviewModel(fact_claim)),
            retriever=ToolEvidenceRetriever(lookup, tools, allowed_tools={"read"}),
        ).run([Message("user", "当前票价？")], references=[
            Evidence("old", "票价 10 元", "旧缓存", expires_at=time.time() - 1),
        ])
        self.assertEqual(result.answer, "票价 20 元")
        self.assertIn("expired_evidence", [item.code for item in result.attempts[0].findings])
        self.assertEqual(result.stats.model_calls, 6)
        self.assertEqual(result.stats.total_tool_executions, 1)
        self.assertEqual(result.stats.tool_executions, 0)
        phases = [event.phase for event in result.snapshot.events if event.kind == "phase"]
        self.assertEqual(phases, ["generation", "check", "review", "retrieval", "repair", "check", "review", "approved"])

    async def test_tool_timeout_retains_uncertain_operation_without_secret_error_text(self):
        tools = Tools({"write": TimeoutError("sensitive backend detail")})
        model = ModelScript([Message("assistant", tool_calls=[ToolCall("save", "write", {"seat": "A"})])])
        with self.assertRaises(AgentTimeoutError) as failure:
            await ReliableAgent(model, tools, Reviewer()).run([Message("user", "保存")])
        execution = failure.exception.snapshot.executions[0]
        self.assertEqual(execution.state, "uncertain")
        self.assertEqual(execution.outcome, "unknown")
        self.assertEqual(json.loads(execution.arguments_json), {"seat": "A"})
        self.assertEqual(execution.error_type, "TimeoutError")
        self.assertNotIn("sensitive", str(failure.exception))

    async def test_cancellation_retains_inflight_tool_receipt_state(self):
        ready = asyncio.Event()

        class WaitingTools(Tools):
            async def call_tool(self, name, arguments):
                ready.set()
                await asyncio.sleep(10)

        model = ModelScript([Message("assistant", tool_calls=[ToolCall("save", "write", {})])])
        task = asyncio.create_task(ReliableAgent(model, WaitingTools({"write": ""}), Reviewer()).run([Message("user", "保存")]))
        await ready.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as failure:
            await task
        self.assertEqual(failure.exception.snapshot.phase, "cancelled")
        self.assertEqual(failure.exception.snapshot.executions[0].state, "uncertain")

    async def test_concurrent_runs_have_separate_global_budgets(self):
        class Generator:
            async def complete(self, messages, tools):
                await asyncio.sleep(0)
                return Message("assistant", "建议检查输入。")

        agent = ReliableAgent(Generator(), Tools(), LLMReviewer(ReviewModel()), config=ReliabilityConfig(max_model_calls=2))
        results = await asyncio.gather(*(agent.run([Message("user", str(i))]) for i in range(2)))
        self.assertEqual([result.stats.model_calls for result in results], [2, 2])
        self.assertIsNot(results[0].snapshot, results[1].snapshot)

    async def test_reviewer_format_retry_is_bounded_and_counted(self):
        class Recovering(ReviewModel):
            async def complete(self, messages, tools):
                if not self.calls:
                    self.calls.append((deepcopy(messages), deepcopy(tools)))
                    return Message("assistant", "BROKEN_REVIEW_SECRET")
                return await super().complete(messages, tools)

        model = Recovering()
        result = await ReliableAgent(
            ModelScript([Message("assistant", "建议检查输入。")]), Tools(), LLMReviewer(model),
        ).run([Message("user", "建议")])
        self.assertEqual(result.stats.review_model_calls, 2)
        self.assertEqual(result.stats.model_calls, 3)
        self.assertEqual(result.stats.repairs, 0)
        self.assertNotIn("BROKEN_REVIEW_SECRET", model.calls[-1][0][-1].content)
        self.assertIn("review_format_feedback", json.loads(model.calls[-1][0][-1].content))

    async def test_exhausted_format_retry_retains_pending_draft_for_audit_only(self):
        reviewer = ModelScript([Message("assistant", "bad"), Message("assistant", "still bad")])
        with self.assertRaises(InvalidReviewError) as failure:
            await ReliableAgent(ModelScript([Message("assistant", "草稿")]), Tools(), LLMReviewer(reviewer)).run([Message("user", "建议")])
        snapshot = failure.exception.snapshot
        self.assertEqual(snapshot.stats.model_calls, 3)
        self.assertEqual(snapshot.attempts[0].answer, "草稿")
        self.assertIsNone(snapshot.attempts[0].review)
        self.assertEqual(snapshot.phase, "failed")

    async def test_unmatched_claim_retries_review_and_requires_repair_before_approval(self):
        def claims(payload):
            if payload["answer"] == "旧的草稿":
                return [{"text": "不在草稿里的声明", "kind": "fact", "status": "unsupported", "citations": []}]
            return []

        reviewer = ReviewModel(claims)
        model = ModelScript([Message("assistant", "旧的草稿"), Message("assistant", "请先核对资料。")])
        result = await ReliableAgent(model, Tools(), LLMReviewer(reviewer)).run([Message("user", "如何核实？")])
        self.assertEqual(result.answer, "请先核对资料。")
        self.assertEqual(result.stats.review_model_calls, 3)
        self.assertEqual(result.stats.repairs, 1)
        self.assertEqual(result.attempts[0].findings[0].code, "invalid_claim_anchor")

    async def test_receipts_bind_call_id_and_original_arguments(self):
        model = ModelScript([
            Message("assistant", tool_calls=[ToolCall("read-call", "read", {"city": "上海"})]),
            Message("assistant", "建议检查返回值。"),
        ])
        result = await ReliableAgent(model, Tools({"read": "资料"}), Reviewer()).run([Message("user", "查询")])
        self.assertEqual(result.evidence[0].call_id, "read-call")
        self.assertEqual(json.loads(result.evidence[0].arguments_json), {"city": "上海"})
        self.assertEqual(result.snapshot.executions[0].evidence_id, result.evidence[0].id)


class EvaluationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_and_scorer_timeouts_are_distinguished(self):
        async def slow_runner(case):
            await asyncio.sleep(10)

        async def good_runner(case):
            return "答案"

        async def slow_scorer(case, answer):
            await asyncio.sleep(10)

        case = EvaluationCase("case", (Message("user", "问题"),))
        first = await evaluate([case], slow_runner, slow_scorer, run_timeout=0.01)
        second = await evaluate([case], good_runner, slow_scorer, score_timeout=0.01)
        self.assertEqual(first.records[0].error_stage, "runner")
        self.assertEqual(second.records[0].error_stage, "scorer")
        self.assertEqual(second.delivery_rate, 1)
        self.assertEqual(second.pass_rate, 0)

    async def test_bounded_parallel_evaluation_preserves_case_order(self):
        active = maximum = 0

        async def runner(case):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.005 if case.id == "0" else 0)
            active -= 1
            return "答案"

        async def scorer(case, answer):
            return EvaluationScore(True, True, True)

        cases = [EvaluationCase(str(i), (Message("user", "问题"),)) for i in range(4)]
        report = await evaluate(cases, runner, scorer, max_concurrency=2)
        self.assertEqual([record.case_id for record in report.records], ["0", "1", "2", "3"])
        self.assertEqual(maximum, 2)
        self.assertEqual(report.factual_rate, 1)

    async def test_cancelling_evaluation_cancels_its_workers(self):
        ready = asyncio.Event()
        started = 0
        stopped = []

        async def runner(case):
            nonlocal started
            started += 1
            if started == 2:
                ready.set()
            try:
                await asyncio.sleep(10)
            finally:
                stopped.append(case.id)

        async def scorer(case, answer):
            return EvaluationScore(True, True, True)

        cases = [EvaluationCase(str(i), (Message("user", "问题"),)) for i in range(2)]
        task = asyncio.create_task(evaluate(cases, runner, scorer, max_concurrency=2))
        await ready.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(sorted(stopped), ["0", "1"])


if __name__ == "__main__":
    unittest.main()
