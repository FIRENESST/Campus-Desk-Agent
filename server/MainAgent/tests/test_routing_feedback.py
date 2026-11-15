import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from main_agent import Message
from main_agent.campus_cluster import AgentId, ClusterRequest, LLMRouter, RoutePlan, WorkStep, build_campus_cluster
from main_agent.campus_cluster.routing_feedback import DATA_DIR_ENV, ROUTING_TIMEZONE, RoutingFeedback
from main_agent.cloud import routing_admin
from main_agent.cloud.runner import CampusRunner, ExecutionContext
from main_agent.reliability.prompts import REVIEW_INSTRUCTIONS


INTRO = AgentId.CAMPUS_INTRODUCTION
SERVICE = AgentId.CAMPUS_SERVICES
SEARCH = AgentId.INFORMATION_SEARCH


class LocalRoutingStateTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.feedback = RoutingFeedback(directory)
        self.feedback.initialize()

    def test_epoch_blocks_disabled_runs_and_runs_from_a_previous_enable(self):
        self.assertFalse(self.feedback.is_enabled())
        self.feedback._record(None, "禁用期间的请求", (SEARCH,), False)
        self.assertEqual(self.feedback.list_samples(), [])
        self.feedback.set_enabled(True)
        old_epoch = self.feedback._read_epoch()
        self.feedback.set_enabled(True)
        self.assertEqual(self.feedback._read_epoch(), old_epoch)
        self.feedback.set_enabled(False)
        self.feedback.set_enabled(True)
        self.feedback._record(old_epoch, "重新开启之前开始的请求", (SEARCH,), False)
        self.assertEqual(self.feedback.list_samples(), [])

    def test_scrubbed_samples_are_local_and_only_reviewed_labels_are_used(self):
        self.feedback.set_enabled(True)
        epoch = self.feedback._read_epoch()
        private_prompt = (
            '请帮我预约图书馆自习座位。姓名：李测试，fixture@example.invalid，13812345678，'
            'https://example.invalid/private，"password": "fixture, credential"'
        )
        self.feedback._record(epoch, private_prompt, (INTRO, SERVICE), True)
        sample, = self.feedback.list_samples()
        self.assertEqual(sample["called_agents"], [INTRO.value, SERVICE.value])
        self.assertTrue(sample["explicit"])
        self.assertIn("请帮我预约图书馆自习座位", sample["prompt"])
        for identifier in ("李测试", "fixture@", "13812345678", "example.invalid", "credential"):
            self.assertNotIn(identifier, sample["prompt"])
        self.assertIsNone(self.feedback._read_advice("我想预约图书馆自习座位").suggested_targets)

        self.assertTrue(self.feedback.grade(sample["id"], 2, (SERVICE,)))
        reopened = RoutingFeedback(self.feedback.path.parent)
        self.assertEqual(reopened._read_advice("我想预约图书馆自习座位").suggested_targets, (SERVICE,))
        reopened.set_enabled(False)
        self.assertIsNone(reopened._read_advice(private_prompt).suggested_targets)
        self.assertEqual(reopened.list_samples()[0]["score"], 2)

    def test_conflicting_labels_are_ignored_until_corrected(self):
        self.feedback.set_enabled(True)
        epoch = self.feedback._read_epoch()
        query = "帮我看看图书馆那边"
        self.feedback._record(epoch, query, (INTRO,), False)
        self.feedback._record(epoch, query, (SEARCH,), False)
        first, second = self.feedback.list_samples()
        self.feedback.grade(first["id"], 1, (INTRO,))
        self.feedback.grade(second["id"], 1, ())
        self.assertIsNone(self.feedback._read_advice(query).suggested_targets)
        self.assertTrue(self.feedback.delete_sample(first["id"]))
        self.assertEqual(self.feedback._read_advice(query).suggested_targets, ())

    def test_semantic_prototypes_use_reviewed_text_and_follow_regrading_and_deletion(self):
        calls = []

        def encode(texts):
            calls.append(texts)
            return "fixture-space", [[1.0, 0.0] for _ in texts]

        feedback = RoutingFeedback(self.feedback.path.parent, embed=encode)
        query = "先找我的课程表，再帮忙借一间空教室，viewer@example.invalid"
        original = "帮我查课表，顺便预约一间教室，fixture@example.invalid"
        self.assertIsNone(feedback._read_advice(query).suggested_targets)
        feedback.set_enabled(True)
        feedback._record(feedback._read_epoch(), original, (SEARCH,), False)
        sample, = feedback.list_samples()
        self.assertIsNone(feedback._read_advice(query).suggested_targets)
        self.assertEqual(calls, [])

        feedback.grade(sample["id"], 2, (SEARCH, SERVICE))
        self.assertEqual(feedback._read_advice(original).suggested_targets, (SERVICE, SEARCH))
        self.assertEqual(calls, [])
        self.assertEqual(feedback._read_advice(query).suggested_targets, (SERVICE, SEARCH))
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("@" not in text for batch in calls for text in batch))
        feedback.grade(sample["id"], 1, (SERVICE,))
        self.assertEqual(feedback._read_advice(query).suggested_targets, (SERVICE,))
        self.assertEqual(len(calls), 3)
        self.assertTrue(feedback.delete_sample(sample["id"]))
        self.assertIsNone(feedback._read_advice(query).suggested_targets)
        self.assertEqual(len(calls), 3)

    def test_semantic_threshold_rejects_unfamiliar_text_and_close_competing_labels(self):
        def encode(texts):
            return "fixture-space", [[0.0, 1.0] if "天气" in text else [1.0, 0.0] for text in texts]

        feedback = RoutingFeedback(self.feedback.path.parent, embed=encode)
        feedback.set_enabled(True)
        epoch = feedback._read_epoch()
        feedback._record(epoch, "申请多媒体教室开班会", (SERVICE,), False)
        feedback.grade(feedback.list_samples()[0]["id"], 5, (SERVICE,))
        self.assertIsNone(feedback._read_advice("今天天气会有变化吗").suggested_targets)

        feedback._record(epoch, "查课表并预约一间教室", (SEARCH, SERVICE), False)
        feedback.grade(feedback.list_samples()[0]["id"], 5, (SEARCH, SERVICE))
        self.assertIsNone(feedback._read_advice("班会要借个房间").suggested_targets)

    def test_encoder_failure_and_epoch_change_preserve_introductions_without_a_hint(self):
        self.feedback.set_enabled(True)
        self.feedback.set_introduction(SERVICE, "我负责校园场地预约。")
        self.feedback._record(self.feedback._read_epoch(), "申请多媒体教室开班会", (SERVICE,), False)
        self.feedback.grade(self.feedback.list_samples()[0]["id"], 5, (SERVICE,))

        def unavailable(texts):
            raise OSError("fixture: local RAG unavailable")

        def switch_epoch(texts):
            self.feedback.set_enabled(False)
            self.feedback.set_enabled(True)
            return "fixture-space", [[1.0, 0.0] for _ in texts]

        for encode in (unavailable, switch_epoch):
            with self.subTest(encoder=encode.__name__):
                feedback = RoutingFeedback(self.feedback.path.parent, embed=encode)
                advice = feedback._read_advice("班会要借个房间")
                self.assertEqual(advice.introductions, {SERVICE: "我负责校园场地预约。"})
                self.assertIsNone(advice.suggested_targets)

    def test_admin_cli_requires_local_elevation_and_can_edit_an_introduction(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {DATA_DIR_ENV: str(self.feedback.path.parent)}):
            with patch.object(routing_admin, "_is_local_admin", return_value=False), redirect_stderr(stderr):
                with self.assertRaises(SystemExit):
                    routing_admin.main(["enable"])
            self.assertIn("管理员", stderr.getvalue())
            self.assertFalse(self.feedback.is_enabled())

            intro_file = Path(self.feedback.path.parent) / "intro.txt"
            intro_file.write_text("我帮助查询校园服务并说明办理流程。", encoding="utf-8")
            with patch.object(routing_admin, "_is_local_admin", return_value=True), redirect_stdout(stdout):
                routing_admin.main(["set-introduction", SERVICE.value, str(intro_file)])
                routing_admin.main(["enable"])
            self.assertTrue(self.feedback.is_enabled())
            self.assertEqual(self.feedback.introductions(), {SERVICE: intro_file.read_text(encoding="utf-8")})

    def test_dashboard_uses_real_dates_and_counts_role_sets_with_legacy_samples(self):
        self.feedback.set_enabled(True)
        epoch = self.feedback._read_epoch()
        self.feedback._record(epoch, "历史校园通行问题", (), False)
        with self.feedback._connect(write=True) as db:
            for column in ("created_at", "answered_direct", "expected_direct"):
                db.execute(f"ALTER TABLE routing_samples DROP COLUMN {column}")
        self.feedback.initialize()

        today = datetime.now(ROUTING_TIMEZONE).date()
        timestamp = datetime.combine(today, time(), tzinfo=ROUTING_TIMEZONE).timestamp()
        with patch("main_agent.campus_cluster.routing_feedback.time", return_value=timestamp):
            self.feedback._record(epoch, "图书馆开放时间", (SERVICE, SEARCH, SERVICE), True)
            self.feedback._record(epoch, "在哪里查看校历", (SEARCH,), False)
            self.feedback._record(epoch, "校内场地如何预约", (), False)
        samples = {sample["prompt"]: sample for sample in self.feedback.list_samples()}
        legacy = samples["历史校园通行问题"]
        self.assertIsNone(legacy["created_at"])
        self.assertFalse(legacy["answered_direct"])
        self.assertFalse(legacy["expected_direct"])
        self.assertTrue(self.feedback.grade(samples["图书馆开放时间"]["id"], 5, (SEARCH, SERVICE)))
        self.assertTrue(self.feedback.grade(samples["在哪里查看校历"]["id"], 2, (INTRO,)))

        overview = self.feedback.overview(7)
        self.assertEqual(overview["totals"], {
            "total": 4, "reviewed": 2, "pending": 2, "agreed": 1, "undated": 1, "active_examples": 2,
        })
        self.assertEqual(overview["timeline"][-1], {"day": today.isoformat(), "total": 3, "reviewed": 2})
        distribution = {row["agent"]: (row["actual"], row["expected"]) for row in overview["distribution"]}
        self.assertEqual((distribution[SEARCH], distribution[SERVICE], distribution[INTRO], distribution[None]),
                         ((2, 1), (1, 1), (0, 1), (2, 0)))

        page = self.feedback.samples_page(limit=1, day=today.isoformat())
        self.assertEqual((page["total"], page["next_offset"]), (3, 1))
        self.assertEqual(page["samples"][0]["prompt"], "校内场地如何预约")
        different = self.feedback.samples_page(status="different", agent=SEARCH.value, origin="automatic")
        self.assertEqual([row["prompt"] for row in different["samples"]], ["在哪里查看校历"])
        match = self.feedback.samples_page(q=samples["图书馆开放时间"]["id"].upper())
        self.assertEqual([row["prompt"] for row in match["samples"]], ["图书馆开放时间"])
        self.assertEqual(self.feedback.samples_page(agent="none")["total"], 2)

    def test_direct_answers_and_clarification_have_distinct_feedback(self):
        self.feedback.set_enabled(True)
        epoch = self.feedback._read_epoch()
        query = "我的舍友一直孤立我怎么办"
        self.feedback._record(epoch, query, (), False, answered_direct=True)
        sample, = self.feedback.list_samples()
        self.assertTrue(sample["answered_direct"])
        self.assertTrue(self.feedback.grade(sample["id"], 3, ()))
        self.assertEqual(self.feedback.samples_page(status="different")["total"], 1)
        self.assertEqual(self.feedback.preview(query)["suggested_targets"], ())
        self.assertFalse(self.feedback.preview(query)["suggest_direct"])
        with self.assertRaises(ValueError):
            self.feedback.grade(sample["id"], 5, (SERVICE,), direct=True)
        self.assertTrue(self.feedback.grade(sample["id"], 5, (), direct=True))
        self.assertEqual(self.feedback.samples_page(status="different")["total"], 0)
        self.assertEqual(self.feedback.samples_page(agent="undispatched")["total"], 0)
        direct, = self.feedback.samples_page(agent="direct")["samples"]
        self.assertTrue(direct["expected_direct"])
        self.assertEqual(self.feedback.overview()["totals"]["agreed"], 1)
        self.assertEqual(self.feedback.preview(query), {
            "enabled": True, "suggested_targets": None, "suggest_direct": True, "method": "exact",
        })
        self.feedback.clear_grade(sample["id"])
        self.assertFalse(self.feedback.preview(query)["suggest_direct"])
        self.assertIsNone(self.feedback.list_samples()[0]["expected_targets"])

    def test_preview_uses_current_labels_and_clear_or_delete_removes_their_effect(self):
        self.feedback.set_enabled(True)
        epoch = self.feedback._read_epoch()
        query = "中传场馆预约入口"
        self.feedback._record(epoch, query, (INTRO,), False)
        sample, = self.feedback.list_samples()
        self.feedback.grade(sample["id"], 2, (SERVICE,))
        self.assertEqual(self.feedback.preview(query), {
            "enabled": True, "suggested_targets": (SERVICE,), "suggest_direct": False, "method": "exact",
        })
        self.feedback.clear_grade(sample["id"])
        self.assertIsNone(self.feedback.preview(query)["suggested_targets"])
        self.assertEqual(self.feedback.samples_page(status="pending")["total"], 1)
        self.feedback.grade(sample["id"], 3, ())
        self.assertEqual(self.feedback.preview(query)["suggested_targets"], ())
        self.assertEqual(self.feedback.delete_samples((sample["id"], "missing")), 1)
        self.assertIsNone(self.feedback.preview(query)["suggested_targets"])
        self.assertEqual(self.feedback.list_samples(), [])
        self.feedback.set_introduction(SERVICE, "负责校园办理")
        self.assertTrue(self.feedback.reset_introduction(SERVICE))
        self.assertEqual(self.feedback.introductions(), {})
        self.feedback.set_enabled(False)
        self.assertEqual(self.feedback.preview(query), {
            "enabled": False, "suggested_targets": None, "suggest_direct": False, "method": None,
        })


class RoutingFeedbackIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.feedback = RoutingFeedback(self.enterContext(TemporaryDirectory()))
        self.feedback.initialize()

    @staticmethod
    def context(prompt, targets=(), history=()):
        return ExecutionContext(uuid4(), uuid4(), uuid4(), uuid4(), prompt, targets, history)

    async def test_direct_feedback_can_correct_the_initial_scope_decision(self):
        self.feedback.set_enabled(True)
        query = "室友不理我，我该怎么和他们沟通？"
        self.feedback._record(self.feedback._read_epoch(), query, (SERVICE,), False)
        self.feedback.grade(self.feedback.list_samples()[0]["id"], 2, (), direct=True)
        advice = []
        answer = "被室友冷落会很不好受。可以先找一个你比较信任的室友，私下问问最近发生了什么。"

        class Model:
            async def complete(self, messages, tools):
                instructions = messages[0].content
                if instructions.startswith("你负责校园智能体路由的范围判断"):
                    return Message("assistant", '{"campus":true}')
                if instructions.startswith("你负责校园智能体路由"):
                    advice.append(json.loads(instructions.splitlines()[-1]))
                    return Message("assistant", '{"general":true}')
                return Message("assistant", answer)

        runner = CampusRunner.with_model(Model(), routing_feedback=self.feedback)
        with patch("main_agent.mcp_tools.connect_main_agent_mcp", side_effect=TimeoutError), patch.object(
            runner.answerer, "run", side_effect=AssertionError("人际建议进入了校园事实核验"),
        ):
            output = await runner.run(self.context(query))
        self.assertEqual(advice, [{"general": True}])
        self.assertEqual(output.steps, ())
        self.assertEqual(output.content, answer)
        self.assertTrue(output.artifacts[0]["payload"]["general"])
        self.assertTrue(self.feedback.list_samples()[0]["answered_direct"])

    async def test_reviewed_multi_intent_hint_reaches_the_main_router_as_two_roles(self):
        def encode(texts):
            return "fixture-space", [[1.0, 0.0] for _ in texts]

        feedback = RoutingFeedback(self.feedback.path.parent, embed=encode)
        feedback.set_enabled(True)
        original = "帮我查课表，顺便预约一间教室"
        feedback._record(feedback._read_epoch(), original, (SEARCH,), False)
        feedback.grade(feedback.list_samples()[0]["id"], 2, (SEARCH, SERVICE))
        prompts = []

        class Model:
            async def complete(self, messages, tools):
                system = messages[0].content
                prompts.append(system)
                return Message("assistant", system.splitlines()[-1])

        router = LLMRouter(Model(), feedback=feedback)
        plan = await router.route(
            ClusterRequest("chat", "先找我的课程表，再帮忙借一间空教室"),
            build_campus_cluster().registry.profiles,
        )
        self.assertCountEqual([step.agent_id for step in plan.steps], [SEARCH, SERVICE])
        self.assertIsNone(plan.clarification)
        self.assertNotIn(original, prompts[0])

    async def test_main_router_uses_introductions_and_learns_from_a_correction(self):
        intro = "我负责图书馆预约和其他校园服务流程。"
        self.feedback.set_introduction(SERVICE, intro)
        route_messages = []

        class Model:
            async def complete(self, messages, tools):
                system = messages[0].content
                if system.startswith("你负责校园智能体路由的范围判断"):
                    return Message("assistant", '{"campus":true}')
                if system.startswith("你负责校园智能体路由"):
                    route_messages.append(messages)
                    preferred = json.loads(messages[-1].content).get("suggested_targets", [])
                    role = AgentId(preferred[0]) if preferred else (
                        SERVICE if system.endswith('{"targets": ["campus_services"]}') else INTRO
                    )
                    return Message("assistant", json.dumps({"targets": [role.value]}))
                if system.startswith(REVIEW_INSTRUCTIONS):
                    task = json.loads(messages[-1].content)
                    return Message("assistant", json.dumps({
                        "rule_checks": [{"rule_id": rule["id"], "status": "pass", "reason": "符合"}
                                        for rule in task["rules"]],
                        "claims": [], "issues": [],
                    }))
                return Message("assistant", "请查看学校公布的流程。")

        self.enterContext(patch("main_agent.mcp_tools.connect_main_agent_mcp", side_effect=TimeoutError))
        runner = CampusRunner.with_model(Model(), routing_feedback=self.feedback)
        prompt = "请帮我预约图书馆自习座位"
        await runner.run(self.context(prompt))
        self.assertEqual(self.feedback.list_samples(), [])
        self.assertIn(intro, route_messages[0][0].content)
        self.assertNotIn(intro, route_messages[0][1].content)

        self.feedback.set_enabled(True)
        first = await runner.run(self.context(prompt + "，fixture@example.invalid", history=(
            {"role": "user", "content": "该会话的历史不应进入样本"},
        )))
        self.assertEqual(first.steps[0]["agent_id"], INTRO.value)
        sample, = self.feedback.list_samples()
        self.assertEqual(sample["called_agents"], [INTRO.value])
        self.assertIsNone(sample["score"])
        self.assertNotIn("历史", sample["prompt"])
        self.feedback.grade(sample["id"], 2, (SERVICE,))

        second = await runner.run(self.context("我想预约图书馆自习座位"))
        self.assertEqual(second.steps[0]["agent_id"], SERVICE.value)
        self.assertIn("本机管理员", route_messages[-1][0].content)
        self.assertNotIn(sample["prompt"], route_messages[-1][0].content)
        self.assertNotIn("fixture@example.invalid", route_messages[-1][1].content)

        forced = await runner.run(self.context("我想预约图书馆自习座位", (SEARCH.value,)))
        self.assertEqual(forced.steps[0]["agent_id"], SEARCH.value)
        self.assertEqual(len(route_messages), 4)
        self.assertEqual(self.feedback.list_samples()[0]["called_agents"], [SEARCH.value])

    async def test_records_dispatch_even_when_the_agent_fails_but_skips_blocked_steps(self):
        class Router:
            async def route(self, request, catalog):
                return RoutePlan((WorkStep("search", SEARCH), WorkStep("intro", INTRO, ("search",))))

        class FailingBackend:
            async def run(self, task):
                raise RuntimeError("fixture")

        self.feedback.set_enabled(True)
        runner = CampusRunner(
            lambda context: build_campus_cluster(router=Router(), backends={SEARCH: FailingBackend()}),
            routing_feedback=self.feedback,
        )
        output = await runner.run(self.context("请查证学校公告"))
        self.assertEqual([step["status"] for step in output.steps], ["failed", "blocked"])
        sample, = self.feedback.list_samples()
        self.assertEqual(sample["called_agents"], [SEARCH.value])
