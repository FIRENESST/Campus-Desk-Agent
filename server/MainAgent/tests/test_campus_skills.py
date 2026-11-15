import json
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from main_agent import Message, ToolCall, UnavailableToolError
from main_agent.campus_cluster import (
    AgentId, AgentTask, CampusIntroductionAgent, ClusterRequest, InformationSearchAgent, LLMRouter,
    ReliableAgentBackend, Skill, SkillContext,
    SkillInput, SkillRegistry, Status, build_campus_cluster,
)
from main_agent.campus_cluster.action_plan import CampusActionPlan
from main_agent.campus_cluster.capabilities import (
    default_skill_registries, describe_capabilities, publish_default_capabilities, scope_actions,
)
from main_agent.campus_cluster.subskills.minta_campus import campus_intent, classroom_command_needs_model, redact_classroom_contact
from main_agent.cloud.runner import CampusRunner, ExecutionContext
from main_agent.reliability import (
    Citation, Claim, InvalidDraftError, ReliableAgent, Review, RuleCheck,
    ToolEvidenceRetriever, ToolResult,
)


INTRO = AgentId.CAMPUS_INTRODUCTION
SEARCH = AgentId.INFORMATION_SEARCH


class LookupInput(SkillInput):
    query: str
    limit: int = 1


class ScriptModel:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.catalogs = []

    async def complete(self, messages, tools):
        self.catalogs.append([tool.name for tool in tools])
        return next(self.replies)


class PassingReviewer:
    async def review(self, context):
        return Review(tuple(RuleCheck(rule.id, "pass", "符合") for rule in context.rules))


class ForbiddenTransport:
    async def list_tools(self):
        raise AssertionError("技能后端访问了原工具客户端")

    async def call_tool(self, name, arguments):
        raise AssertionError("技能后端调用了原工具客户端")


class SkillTests(unittest.IsolatedAsyncioTestCase):
    async def test_campus_action_skill_receives_the_structured_plan(self):
        agent = build_campus_cluster().registry.get(AgentId.CAMPUS_SERVICES)
        plan = CampusActionPlan(action="clubs.register", target="摄影社", arguments={"category": "文艺"})
        calls = []

        async def action(context, args):
            calls.append((context, args.plan))
            return '{"status":"completed","message":"已提交登记"}'

        reply, pending = await agent.execute_action("chat", plan, action)
        self.assertEqual(calls, [(SkillContext(AgentId.CAMPUS_SERVICES, "chat", "minta-action"), plan)])
        self.assertEqual(reply.status, Status.COMPLETED)
        self.assertIsNone(pending)

        agent.skill_registry = SkillRegistry({AgentId.CAMPUS_SERVICES: ()})
        reply, _ = await agent.execute_action("chat", plan, action)
        self.assertEqual(reply.status, Status.NEEDS_CONFIGURATION)
        self.assertEqual(len(calls), 1)

    async def test_registry_binds_role_and_snapshots_advertised_schema(self):
        calls = []

        async def lookup(context, args):
            calls.append((context, args))
            return context.agent_id.value

        same_name = Skill("lookup", "查询资料", LookupInput, lookup, read_only=True)
        extra = Skill("outside", "额外查询", LookupInput, lookup)
        declarations = [same_name]
        registry = SkillRegistry({INTRO: declarations, SEARCH: (same_name, extra)})
        declarations.append(extra)
        profiles = build_campus_cluster().registry
        intro = registry.bind(AgentTask("chat", "intro", profiles.get(INTRO).profile, "问题"))
        search_task = AgentTask("chat", "search", profiles.get(SEARCH).profile, "问题")
        search = registry.bind(search_task)
        self.assertEqual(intro.names, {"lookup"})
        self.assertEqual(search.names, {"lookup", "outside"})
        self.assertFalse(registry.for_role(INTRO).bind(search_task).names)
        restricted_profile = replace(profiles.get(SEARCH).profile, allowed_tools=frozenset({"lookup"}))
        restricted_task = AgentTask("chat", "restricted", restricted_profile, "问题")
        self.assertEqual(registry.bind(restricted_task, allowed_tools={"lookup", "outside"}).names, {"lookup"})
        self.assertFalse(registry.bind(restricted_task, allowed_tools=set()).names)
        self.assertEqual(
            {item["name"]: item["enabled"] for item in registry.describe(restricted_profile)},
            {"lookup": True, "outside": False},
        )

        advertised = (await intro.list_tools())[0]
        advertised.parameters["properties"]["limit"]["type"] = "string"
        self.assertEqual((await intro.list_tools())[0].parameters["properties"]["limit"]["type"], "integer")
        with self.assertRaises(UnavailableToolError):
            await intro.call_tool("outside", {"query": "资料"})
        with self.assertRaises(InvalidDraftError):
            await intro.call_tool("lookup", {"query": "资料", "agent_id": SEARCH.value})
        self.assertEqual(await intro.call_tool("lookup", {"query": "资料"}), INTRO.value)
        self.assertEqual(await search.call_tool("lookup", {"query": "资料"}), SEARCH.value)
        self.assertEqual([item[0] for item in calls], [
            SkillContext(INTRO, "chat", "intro"), SkillContext(SEARCH, "chat", "search"),
        ])
        self.assertEqual(calls[0][1].limit, 1)

        with self.assertRaisesRegex(ValueError, "重复注册"):
            SkillRegistry({INTRO: (same_name, same_name)})

    async def test_default_cloud_runner_uses_each_roles_registry(self):
        calls = []

        async def lookup(context, args):
            calls.append(context)
            return ToolResult(args.query, status="success")

        registry = SkillRegistry({
            SEARCH: (Skill("campus_lookup", "检索资料", LookupInput, lookup, read_only=True),),
        })
        attempt = Message("assistant", tool_calls=[ToolCall("lookup", "campus_lookup", {"query": "图书馆"})])
        recovery = "暂时无法核对图书馆公告。你想查哪一条通知？"
        model = ScriptModel(
            Message("assistant", '{"campus":true}'),
            Message("assistant", '{"targets":["information_search"]}'),
            attempt, Message("assistant", "建议查阅图书馆公告。"), Message("assistant", "我建议查看图书馆公告。"),
            Message("assistant", '{"campus":true}'),
            Message("assistant", '{"targets":["campus_introduction"]}'), attempt, Message("assistant", recovery),
        )
        user, instance = uuid4(), uuid4()
        contexts = [
            ExecutionContext(user, instance, uuid4(), uuid4(), "查询", (role.value,), ())
            for role in (SEARCH, INTRO)
        ]
        with (
            patch.object(InformationSearchAgent, "skill_registry", registry),
            patch.object(CampusIntroductionAgent, "skill_registry", SkillRegistry({INTRO: ()})),
            patch("main_agent.cloud.runner.LLMReviewer", return_value=PassingReviewer()),
            patch("main_agent.mcp_tools.connect_main_agent_mcp", side_effect=TimeoutError),
        ):
            runner = CampusRunner.with_model(model)
            allowed, denied = [await runner.run(context) for context in contexts]

        self.assertEqual(model.catalogs, [
            [], [], ["campus_lookup"], ["campus_lookup"], ["search_rag"], [], [], [], ["search_rag"],
        ])
        self.assertEqual(allowed.result_status, "completed")
        self.assertEqual(allowed.content, "我建议查看图书馆公告。")
        self.assertEqual(denied.steps[0]["status"], "failed")
        self.assertEqual((denied.result_status, denied.content), ("partial", recovery))
        self.assertEqual(calls, [SkillContext(SEARCH, str(contexts[0].conversation_id), allowed.steps[0]["step_id"])])

    async def test_startup_catalog_reports_skill_assignments_and_discovered_unassigned_skills(self):
        async def preview(context, args):
            raise AssertionError("启动盘点不应执行技能")

        unassigned = Skill("preview_only", "预览资料", SkillInput, preview, read_only=True)
        directory = self.enterContext(TemporaryDirectory())
        path = Path(directory) / "runtime" / "capabilities.json"
        with (
            patch("main_agent.campus_cluster.capabilities.iter_modules", return_value=[SimpleNamespace(name="preview")]),
            patch("main_agent.campus_cluster.capabilities.import_module", return_value=SimpleNamespace(skill=unassigned)),
        ):
            publish_default_capabilities(path)
        catalog = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([agent["id"] for agent in catalog["agents"]], [role.value for role in AgentId])
        skills = {skill["name"]: skill for skill in catalog["skills"]}
        self.assertEqual(skills["preview_only"]["registered_agents"], [])
        self.assertEqual(skills["preview_only"]["description"], "预览资料")
        self.assertEqual(skills["campus_street_view"]["registered_agents"], [
            {"id": INTRO.value, "name": "校园介绍", "enabled": True},
        ])
        for name in ("read_domain_page", "minta_gateway"):
            self.assertEqual([agent["id"] for agent in skills[name]["registered_agents"] if agent["enabled"]],
                             [role.value for role in AgentId])
        self.assertTrue(skills["search_rag"]["read_only"])
        self.assertEqual([agent["id"] for agent in skills["search_rag"]["registered_agents"]], [INTRO.value])
        self.assertEqual(skills["search_campus_digest"]["registered_agents"], [
            {"id": SEARCH.value, "name": "信息搜寻", "enabled": True},
            {"id": AgentId.RUMOR_ASSESSMENT.value, "name": "谣言判别", "enabled": True},
        ])
        self.assertTrue(skills["search_campus_digest"]["read_only"])
        self.assertEqual(skills["campus_wechat_articles"]["registered_agents"], skills["search_campus_digest"]["registered_agents"])
        self.assertTrue(skills["campus_wechat_articles"]["read_only"])
        for name in ("minta_campus_status", "minta_campus_action"):
            self.assertEqual([agent["id"] for agent in skills[name]["registered_agents"]], [AgentId.CAMPUS_SERVICES.value])
        actions = {action["id"]: action for action in skills["minta_campus_action"]["actions"]}
        self.assertEqual(set(actions["classroom.start"]["arguments"]), {"name", "phone", "starts_at", "ends_at"})

    async def test_registered_rag_skill_delivers_scoped_search_evidence(self):
        receipt = {"hits": [{"source": "学生手册", "text": "请假须事先申请。"}]}
        call = AsyncMock(return_value=json.dumps(receipt, ensure_ascii=False))

        @asynccontextmanager
        async def mcp():
            yield SimpleNamespace(call_tool=call)

        registry = default_skill_registries()[INTRO]
        profile = build_campus_cluster().registry.get(INTRO).profile
        task = AgentTask("chat", "search", profile, "学生请假规定")
        model = ScriptModel(Message("assistant", tool_calls=[
            ToolCall("lookup", "search_rag", {"query": " 学生请假规定 "}),
        ]), Message("assistant", "根据《学生手册》，请假须事先申请。"))
        with patch("main_agent.mcp_tools.connect_main_agent_mcp", mcp):
            with self.assertRaises(InvalidDraftError):
                await registry.bind(task, read_only=True).call_tool("search_rag", {"query": "请假", "scope": "other"})
            result = await ReliableAgentBackend.from_skills(model, PassingReviewer(), skills=registry).run(task)

        call.assert_awaited_once_with("search_rag", {"query": "学生请假规定", "top_k": 5})
        self.assertEqual(result.status, Status.COMPLETED)
        evidence, = result.evidence
        self.assertEqual((evidence.source, evidence.status, json.loads(evidence.content)), ("search_rag", "success", receipt))

    async def test_router_guidance_uses_only_live_actions_for_the_current_role(self):
        profiles = build_campus_cluster().registry.profiles
        registered = describe_capabilities(profiles, default_skill_registries())
        live = {"minta_campus_action": {"classroom.start"}}

        async def plan(role, action, *, structured=False):
            async def capabilities(_profiles):
                return scope_actions(registered, role, live)

            model = ScriptModel(Message("assistant", '{"campus":true}'),
                                Message("assistant", json.dumps({"campus_action": action})))
            return await LLMRouter(model, allow_direct=True, capabilities=capabilities, allow_action_plans=structured).route(
                ClusterRequest("chat", "能帮我找个地方借用吗"), profiles,
            )

        student = await plan("student", "classroom.start")
        teacher = await plan("teacher", "classroom.start")
        self.assertIn("老师电话号码", student.clarification)
        self.assertIn("教室编号或名称", teacher.clarification)
        self.assertNotIn("电话号码", teacher.clarification)
        self.assertFalse(student.steps)
        self.assertTrue((await plan("student", "library.start")).direct)
        self.assertTrue((await plan(None, "classroom.start")).direct)
        draft = await plan("student", "classroom.start", structured=True)
        self.assertEqual(draft.campus_action.model_dump(), {"action": "classroom.start", "target": "", "arguments": {}})
        self.assertTrue((await plan(None, "classroom.start", structured=True)).direct)

    def test_minta_commands_use_only_explicit_current_text(self):
        for query in ('能借《馆藏》吗？', '查询馆藏状态', '如果说“借阅《馆藏》”会怎样', '不要归还《馆藏》', '怎么预定教室？'):
            self.assertIsNone(campus_intent(query))
        room = campus_intent('帮我预定一个教室')
        self.assertEqual((room.action, room.service, room.target), ("start", "classroom", ""))
        reservation = campus_intent('帮我预约 A101 教室，老师李老师，电话 13900001234')
        self.assertEqual(reservation.fields, {"name": "李老师", "phone": "13900001234"})
        self.assertFalse(classroom_command_needs_model(reservation))
        self.assertFalse(classroom_command_needs_model(campus_intent("帮我预约体育馆力量训练区")))
        club = campus_intent('登记社团：摄影社，类别：艺术，简介：相机、灯光，外拍')
        self.assertEqual((club.action, club.target, club.fields["description"]), ("register", "摄影社", "相机、灯光,外拍"))

    def test_mixed_classroom_commands_mask_phone_even_with_extra_conditions(self):
        for fields, masked in (
            ("测试教师，13900001234", "测试教师,[电话已隐藏]"),
            ("老师姓名：测试教师，电话：13900001234", "老师姓名:测试教师,电话:[电话已隐藏]"),
        ):
            query = f"帮我预约Ａ１０１，{fields}，明天下午"
            self.assertTrue(classroom_command_needs_model(campus_intent(query)))
            self.assertEqual(redact_classroom_contact(query), f"帮我预约A101,{masked},明天下午")

    async def test_cluster_executes_skills_and_records_receipts(self):
        contexts = []

        async def lookup(context, args):
            contexts.append(context)
            return ToolResult(f"已找到 {args.query}", status="success")

        registry = SkillRegistry({
            INTRO: (Skill("intro_lookup", "查询校园介绍", LookupInput, lookup),),
            SEARCH: (Skill("search_lookup", "检索资料", LookupInput, lookup),),
        })
        intro_model = ScriptModel(
            Message("assistant", tool_calls=[ToolCall("intro", "intro_lookup", {"query": "校园介绍"})]),
            Message("assistant", "建议参阅校园介绍。"),
        )
        search_model = ScriptModel(Message("assistant", "建议按资料来源检索。"))
        intro_agent = ReliableAgent(intro_model, ForbiddenTransport(), PassingReviewer())
        cluster = build_campus_cluster(backends={
            INTRO: ReliableAgentBackend(intro_agent, skills=registry),
            SEARCH: ReliableAgentBackend.from_skills(search_model, PassingReviewer(), skills=registry),
        })
        result = await cluster.run(ClusterRequest("session", "介绍和检索", (INTRO, SEARCH)))
        self.assertEqual(result.status, Status.COMPLETED)
        self.assertEqual(intro_model.catalogs, [["intro_lookup"], ["intro_lookup"]])
        self.assertEqual(search_model.catalogs, [["search_lookup"]])
        intro_reply = result.results[0]
        self.assertEqual(contexts, [SkillContext(INTRO, "session", intro_reply.step_id)])
        self.assertEqual(intro_reply.reply.evidence[0].content, "已找到 校园介绍")
        self.assertEqual(intro_reply.reply.evidence[0].status, "success")
        self.assertIsNone(intro_agent.allowed_tools)
        self.assertIsInstance(intro_agent.tools, ForbiddenTransport)

    async def test_schema_and_role_checks_run_before_any_skill_in_a_batch(self):
        calls = []

        async def record(context, args):
            calls.append(args)
            return "完成"

        registry = SkillRegistry({
            INTRO: (Skill("change", "修改资料", LookupInput, record),),
            SEARCH: (Skill("search_only", "检索资料", LookupInput, record),),
        })
        for name, args, error in (
            ("change", {"query": "资料", "limit": "2"}, "InvalidDraftError"),
            ("search_only", {"query": "资料"}, "UnavailableToolError"),
        ):
            with self.subTest(name=name):
                model = ScriptModel(Message("assistant", tool_calls=[
                    ToolCall("first", "change", {"query": "资料"}), ToolCall("second", name, args),
                ]))
                backend = ReliableAgentBackend.from_skills(model, PassingReviewer(), skills=registry)
                result = await build_campus_cluster(backends={INTRO: backend}).run(
                    ClusterRequest("chat", "修改资料", (INTRO,))
                )
                self.assertEqual(result.results[0].error_type, error)
                self.assertEqual(calls, [])

    async def test_evidence_collection_uses_same_role_and_read_only_skills(self):
        calls = []
        fact = "图书馆开放至 20 点"

        async def read(context, args):
            calls.append(context.agent_id)
            return ToolResult(fact, status="success")

        async def write(context, args):
            raise AssertionError("补证阶段尝试写入")

        class Reviewer:
            async def review(self, context):
                checks = tuple(RuleCheck(rule.id, "pass", "符合") for rule in context.rules)
                if not context.evidence:
                    return Review(checks, (Claim(context.answer, "fact", "unsupported"),))
                citation = Citation(context.evidence[0].id, fact)
                return Review(checks, (Claim(context.answer, "fact", "supported", (citation,)),))

        registry = SkillRegistry({
            INTRO: (
                Skill("hours", "查询开放时间", LookupInput, read, read_only=True),
                Skill("change_hours", "调整开放时间", LookupInput, write),
            ),
            SEARCH: (Skill("other", "另一角色查询", LookupInput, read, read_only=True),),
        })
        lookup_model = ScriptModel(
            Message("assistant", tool_calls=[ToolCall("lookup", "hours", {"query": "图书馆"})]),
            Message("assistant", "已查到开放时间。"),
        )
        transport = ForbiddenTransport()
        retriever = ToolEvidenceRetriever(
            lookup_model, transport, allowed_tools={"hours", "change_hours", "other"},
        )
        backend = ReliableAgentBackend.from_skills(
            ScriptModel(Message("assistant", "图书馆开放至 22 点"), Message("assistant", fact)),
            Reviewer(), skills=registry, retriever=retriever,
        )
        result = await build_campus_cluster(backends={INTRO: backend}).run(
            ClusterRequest("chat", "几点关门", (INTRO,))
        )
        self.assertEqual(result.status, Status.COMPLETED)
        self.assertEqual(lookup_model.catalogs, [["hours"], ["hours"]])
        self.assertEqual(calls, [INTRO])
        self.assertEqual(result.results[0].reply.content, fact)
        self.assertIs(retriever.tools, transport)
