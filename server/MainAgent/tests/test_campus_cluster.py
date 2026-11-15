import asyncio
import unittest
from dataclasses import replace

from main_agent import Message, ToolCall, ToolSpec
from main_agent.campus_cluster import (
    AgentId, AgentReply, ClusterConfig, ClusterError, ClusterRequest,
    ClusterTimeoutError, InterAgentLearningAlgorithm, InvalidRoutePlan,
    ReliableAgentBackend, RoutePlan, RumorVerdict, Status, WorkStep,
    build_campus_cluster,
)
from main_agent.reliability import Evidence, ReliableAgent, Review, RuleCheck


INTRO = AgentId.CAMPUS_INTRODUCTION
SEARCH = AgentId.INFORMATION_SEARCH
SERVICE = AgentId.CAMPUS_SERVICES
RUMOR = AgentId.RUMOR_ASSESSMENT


class Backend:
    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    async def run(self, task):
        self.calls.append(task)
        if self.callback:
            return self.callback(task)
        return AgentReply(Status.COMPLETED, "脚本化框架测试输出")


class FixedRouter:
    def __init__(self, plan):
        self.plan = plan

    async def route(self, request, catalog):
        return self.plan


class CampusClusterTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_registry_has_four_empty_roles(self):
        cluster = build_campus_cluster()
        profiles = cluster.registry.profiles
        self.assertEqual({profile.id for profile in profiles}, set(AgentId))
        self.assertEqual([profile.name for profile in profiles], ["校园介绍", "信息搜寻", "校园服务", "谣言判别"])
        self.assertTrue(all(not profile.knowledge_sources and not profile.allowed_tools for profile in profiles))
        result = await cluster.run(ClusterRequest("chat", "介绍校园", (INTRO,)))
        self.assertEqual(result.status, Status.NEEDS_CONFIGURATION)
        self.assertEqual(result.results[0].reply.content, "")
        self.assertEqual(result.results[0].reply.reason, "backend_not_configured")

    async def test_no_target_requests_clarification_without_running_a_backend(self):
        backend = Backend()
        result = await build_campus_cluster(backends={INTRO: backend}).run(ClusterRequest("chat", "你好"))
        self.assertEqual(result.status, Status.NEEDS_INPUT)
        self.assertTrue(result.clarification)
        self.assertEqual(backend.calls, [])

    async def test_no_peer_request_strategy_is_hardcoded(self):
        search = Backend()
        result = await build_campus_cluster(backends={SEARCH: search}).run(ClusterRequest("chat", "核实说法", (RUMOR,)))
        self.assertEqual([step.agent_id for step in result.plan.steps], [RUMOR])
        self.assertEqual(result.status, Status.NEEDS_CONFIGURATION)
        self.assertEqual(search.calls, [])

    async def test_explicit_routing_only_calls_selected_agent(self):
        introduction, service = Backend(), Backend()
        result = await build_campus_cluster(backends={INTRO: introduction, SERVICE: service}).run(ClusterRequest("chat", "服务咨询", (SERVICE,)))
        self.assertEqual(result.status, Status.COMPLETED)
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(introduction.calls, [])

    async def test_router_can_be_replaced_independently(self):
        router = FixedRouter(RoutePlan((WorkStep("search", SEARCH),)))
        backend = Backend()
        result = await build_campus_cluster(router=router, backends={SEARCH: backend}).run(ClusterRequest("chat", "问题"))
        self.assertEqual(result.results[0].agent_id, SEARCH)
        self.assertEqual(len(backend.calls), 1)

    async def test_external_dependencies_transfer_only_declared_results_and_sources(self):
        source = Evidence("tool:1", "测试证据原文", "Fake 数据源", call_id="call")
        search = Backend(lambda task: AgentReply(Status.COMPLETED, "上游摘要", (source,)))
        rumor = Backend(lambda task: AgentReply(
            Status.COMPLETED, "仅作为框架测试的证据不足结论", task.evidence,
            rumor_verdict=RumorVerdict.INSUFFICIENT_EVIDENCE,
        ))
        router = FixedRouter(RoutePlan((
            WorkStep("search", SEARCH), WorkStep("check", RUMOR, ("search",)),
        )))
        result = await build_campus_cluster(router=router, backends={SEARCH: search, RUMOR: rumor}).run(
            ClusterRequest("chat", "核实", evidence=(Evidence("tool:1", "输入证据", "Fake 输入"),))
        )
        self.assertEqual(result.status, Status.COMPLETED)
        task = rumor.calls[0]
        self.assertEqual([item.id for item in task.evidence], ["input:1", "upstream:search:1"])
        self.assertEqual(task.evidence[1].call_id, "call")
        self.assertEqual(task.upstream[0].content, "上游摘要")
        self.assertEqual(task.evidence[1].content, "测试证据原文")

    async def test_unconfigured_dependency_blocks_downstream(self):
        backend = Backend()
        router = FixedRouter(RoutePlan((WorkStep("search", SEARCH), WorkStep("check", RUMOR, ("search",)))))
        result = await build_campus_cluster(router=router, backends={RUMOR: backend}).run(ClusterRequest("chat", "核实"))
        self.assertEqual(result.status, Status.NEEDS_CONFIGURATION)
        self.assertEqual(result.results[1].reply.status, Status.BLOCKED)
        self.assertEqual(backend.calls, [])

    async def test_invalid_plans_fail_before_any_execution(self):
        plans = (
            RoutePlan((WorkStep("a", INTRO, ("b",)), WorkStep("b", SEARCH, ("a",)))),
            RoutePlan((WorkStep("a", INTRO, ("missing",)),)),
            RoutePlan((WorkStep("a", INTRO), WorkStep("a", SEARCH))),
            RoutePlan((WorkStep("a", INTRO),), clarification="不能同时澄清和执行"),
        )
        for plan in plans:
            backend = Backend()
            cluster = build_campus_cluster(router=FixedRouter(plan), backends={INTRO: backend})
            with self.subTest(plan=plan), self.assertRaises(InvalidRoutePlan):
                await cluster.run(ClusterRequest("chat", "问题"))
            self.assertEqual(backend.calls, [])

    async def test_failed_agent_does_not_hide_independent_completed_results(self):
        def fail(task):
            raise RuntimeError("sensitive backend detail")

        service = Backend(fail)
        result = await build_campus_cluster(backends={INTRO: Backend(), SERVICE: service}).run(ClusterRequest("chat", "处理", (INTRO, SERVICE)))
        self.assertEqual(result.status, Status.PARTIAL)
        self.assertEqual(result.results[1].error_type, "RuntimeError")
        self.assertNotIn("sensitive", str(result))
        self.assertEqual(len(service.calls), 1)

    async def test_partial_reply_remains_partial_at_the_main_agent(self):
        backend = Backend(lambda task: AgentReply(Status.PARTIAL, "脚本化部分结果"))
        result = await build_campus_cluster(backends={INTRO: backend}).run(ClusterRequest("chat", "问题", (INTRO,)))
        self.assertEqual(result.status, Status.PARTIAL)

    async def test_parallel_limit_and_plan_order_are_preserved(self):
        active = maximum = 0

        class WaitingBackend:
            async def run(self, task):
                nonlocal active, maximum
                active += 1
                maximum = max(active, maximum)
                await asyncio.sleep(0.005)
                active -= 1
                return AgentReply(Status.COMPLETED, "脚本化输出")

        result = await build_campus_cluster(
            backends={INTRO: WaitingBackend(), SEARCH: WaitingBackend(), SERVICE: WaitingBackend()},
            config=ClusterConfig(max_parallel=2),
        ).run(ClusterRequest("chat", "并行测试", (SERVICE, INTRO, SEARCH)))
        self.assertEqual(maximum, 2)
        self.assertEqual([item.agent_id for item in result.results], [SERVICE, INTRO, SEARCH])

    async def test_task_history_is_isolated_between_agents_and_from_the_caller(self):
        def mutate(task):
            task.history[0].content = "已修改"
            return AgentReply(Status.COMPLETED, "脚本化输出")

        observer = Backend()
        history = (Message("user", "原始历史"),)
        await build_campus_cluster(backends={INTRO: Backend(mutate), SEARCH: observer}).run(ClusterRequest("chat", "问题", (INTRO, SEARCH), history))
        self.assertEqual(history[0].content, "原始历史")
        self.assertEqual(observer.calls[0].history[0].content, "原始历史")

    async def test_rumor_schema_does_not_allow_unsupported_definitive_verdict(self):
        for reply in (
            AgentReply(Status.COMPLETED, "无结论类型"),
            AgentReply(Status.COMPLETED, "无证据却声称反驳", rumor_verdict=RumorVerdict.REFUTED),
        ):
            backend = Backend(lambda task: reply)
            result = await build_campus_cluster(backends={RUMOR: backend}).run(ClusterRequest("chat", "核实", (RUMOR,)))
            self.assertEqual(result.status, Status.FAILED)

    async def test_individual_timeout_is_reported_as_a_failed_step(self):
        class Slow:
            async def run(self, task):
                await asyncio.sleep(10)

        result = await build_campus_cluster(backends={INTRO: Slow()}, config=ClusterConfig(step_timeout=0.01)).run(ClusterRequest("chat", "问题", (INTRO,)))
        self.assertEqual(result.results[0].reply.reason, "agent_timeout")

    async def test_total_timeout_preserves_visible_cancelled_step(self):
        class Slow:
            async def run(self, task):
                await asyncio.sleep(10)

        cluster = build_campus_cluster(backends={INTRO: Slow()}, config=ClusterConfig(total_timeout=0.01))
        with self.assertRaises(ClusterTimeoutError) as failure:
            await cluster.run(ClusterRequest("chat", "问题", (INTRO,)))
        self.assertEqual(failure.exception.results[0].reply.reason, "agent_cancelled")

    async def test_cancellation_propagates_and_cleans_up_the_backend(self):
        started, stopped = asyncio.Event(), asyncio.Event()

        class Slow:
            async def run(self, task):
                started.set()
                try:
                    await asyncio.sleep(10)
                finally:
                    stopped.set()

        cluster = build_campus_cluster(backends={INTRO: Slow()})
        running = asyncio.create_task(cluster.run(ClusterRequest("chat", "问题", (INTRO,))))
        await started.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError) as failure:
            await running
        self.assertTrue(stopped.is_set())
        self.assertEqual(failure.exception.results[0].error_type, "CancelledError")

    async def test_learning_algorithm_is_an_unimplemented_extension(self):
        algorithm = InterAgentLearningAlgorithm()
        with self.assertRaises(NotImplementedError):
            await algorithm.propose_requests(None, None)
        with self.assertRaises(NotImplementedError):
            await algorithm.observe(None, None)


class ReliableBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_reliable_agent_is_scoped_to_role_tool_permissions(self):
        class Model:
            def __init__(self):
                self.calls = []

            async def complete(self, messages, tools):
                self.calls.append((messages, tools))
                return Message("assistant", "建议先绑定资料源。")

        class ToolClient:
            async def list_tools(self):
                return [ToolSpec(name, name, {"type": "object"}) for name in ("intro_read", "service_write")]

            async def call_tool(self, name, arguments):
                raise AssertionError("本测试不应执行工具")

        class Reviewer:
            async def review(self, context):
                return Review(tuple(RuleCheck(rule.id, "pass", "符合") for rule in context.rules))

        model = Model()
        prototype = ReliableAgent(model, ToolClient(), Reviewer())
        profile = build_campus_cluster().registry.get(INTRO).profile
        cluster = build_campus_cluster(
            backends={INTRO: ReliableAgentBackend(prototype)},
            profiles={INTRO: replace(profile, allowed_tools=frozenset({"intro_read"}))},
        )
        result = await cluster.run(ClusterRequest("chat", "给建议", (INTRO,), evidence=(Evidence("tool:1", "测试资料", "Fake"),)))
        self.assertEqual(result.status, Status.COMPLETED)
        self.assertEqual([tool.name for tool in model.calls[0][1]], ["intro_read"])
        self.assertIsNone(prototype.allowed_tools)
        self.assertEqual(result.results[0].snapshot.phase, "approved")


if __name__ == "__main__":
    unittest.main()
