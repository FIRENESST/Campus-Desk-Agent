import asyncio
import json
import os
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from main_agent import LLMServerSettings, Message, ToolCall, ToolResult
from main_agent.campus_cluster import (
    AgentId, AgentReply, ClusterConfig, ClusterRequest, LLMRouter, Skill, SkillInput, SkillRegistry, Status, build_campus_cluster,
)
from main_agent.campus_cluster.action_plan import CAMPUS_ACTION_CONFIRMATION
from main_agent.campus_cluster.capabilities import default_skill_registries, describe_capabilities, scope_actions
from main_agent.campus_cluster.continuity import SUPPORT_RESPONSE, resolved_query_message
from main_agent.campus_cluster.routing import priority_campus_role, reports_campus_threat
from main_agent.campus_cluster.subskills.campus_digest import CAMPUS_DIGEST
from main_agent.campus_cluster.subskills.campus_street_view import CAMPUS_STREET_VIEW
from main_agent.campus_cluster.subskills.minta_campus import CAMPUS_ACTION
from main_agent.campus_cluster.subskills.rumor_assessment import CAMPUS_RUMOR
from main_agent.campus_cluster.subskills.wechat_articles import WECHAT_ARTICLES
from main_agent.cloud.persona import MAIN_AGENT_PERSONA
from main_agent.cloud.runner import (
    ASSISTANT_INTRODUCTION, CAMPUS_CONTEXT_REQUIRED, CAMPUS_THREAT_FALLBACK, CAMPUS_THREAT_RESPONSE,
    GENERAL_ANSWER_INSTRUCTIONS, MAIN_ANSWER_INSTRUCTIONS,
    CampusRunner, ExecutionContext,
)
from main_agent.reliability.prompts import REPAIR_INSTRUCTIONS, REVIEW_INSTRUCTIONS


class LLMServerSettingsTests(unittest.TestCase):
    def test_local_connection_uses_proxy_port_and_local_key(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("main_agent.llm_server.LOCAL_ENV") as path,
            patch("main_agent.llm_server.dotenv_values", return_value={
                "LOCAL_PORT": "8080", "LOCAL_API_KEY": "fixture-local",
                "LLM_API_KEY": "fixture-upstream", "LLM_MODEL": "fixture-model",
                "LLM_BASE_URL": "https://api.deepseek.com",
            }),
        ):
            path.is_file.return_value = True
            settings = LLMServerSettings.from_env()
        self.assertEqual(settings.base_url, "http://127.0.0.1:8080/v1")
        self.assertEqual(settings.api_key, "fixture-local")
        self.assertNotIn(settings.api_key, repr(settings))
        self.assertTrue(settings.deepseek_json)

    def test_explicit_proxy_address_uses_its_own_credentials(self):
        with patch.dict(os.environ, {
            "MAIN_AGENT_LLM_SERVER_URL": "https://llm.example.invalid/v1",
            "LOCAL_API_KEY": "another-service-key",
        }, clear=True):
            settings = LLMServerSettings.from_env()
        self.assertEqual(settings.base_url, "https://llm.example.invalid/v1")
        self.assertEqual(settings.api_key, "")
        self.assertFalse(settings.deepseek_json)


class ModelWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_compound_turn_scopes_two_rumors_and_keeps_independent_answers(self):
        self.enterContext(patch.dict(os.environ, {"CLOUD_RUMOR_OFFICIAL_DOMAINS": "campus.example.test"}))
        gate = "核实明天南门是否封闭"
        library = "核实后天图书馆是否临时闭馆"
        general = "解释什么是学分"
        compared = "比较两项核验结果"
        followup = "复述核实后确定关闭的校门"
        query = "听说明天南门封闭、后天图书馆临时闭馆。比较两项核验结果，并告诉我核实后确定关闭的是哪个门。顺便解释学分。"
        segments = [
            {"query": gate, "route": {"verify_change": True}},
            {"query": library, "route": {"verify_change": True}},
            {"query": compared, "depends_on": [1, 2], "route": {"direct": True}},
            {"query": followup, "depends_on": [1], "route": {"direct": True}},
            {"query": general, "route": {"general": True}},
        ]
        called = []
        test = self

        async def assess(_context, args):
            called.append(args.text)
            verified = args.text == gate
            evidence = {"provenance": "official_article", "relation": "supported",
                        "url": "https://campus.example.test/notices/fictional", "published": "2026-11-15",
                        "quote": "南门明日关闭。"}
            return ToolResult(json.dumps({"state": "assessed", "verification": [
                {"state": "supported", "official_evidence": [evidence]},
            ] if verified else []}, ensure_ascii=False), "success")

        class Model:
            async def complete(self, messages, tools):
                instructions = messages[0].content
                if instructions.startswith("你负责校园智能体路由的范围判断"):
                    return Message("assistant", '{"compound":true}')
                if instructions.startswith("你负责校园智能体路由。"):
                    return Message("assistant", json.dumps({"parts": segments}, ensure_ascii=False))
                if instructions.startswith(REVIEW_INSTRUCTIONS):
                    rules = json.loads(messages[-1].content)["rules"]
                    return Message("assistant", json.dumps({
                        "rule_checks": [{"rule_id": rule["id"], "status": "pass", "reason": "符合"} for rule in rules],
                        "claims": [], "issues": [],
                    }))
                if instructions == GENERAL_ANSWER_INSTRUCTIONS:
                    test.assertEqual(messages[-1].content, general)
                    return Message("assistant", "学分用于记录课程学习量。")
                if Message("system", MAIN_ANSWER_INSTRUCTIONS) in messages:
                    if messages[-1].content == followup:
                        test.assertTrue(any("南门明日关闭" in item.content for item in messages))
                        return Message("assistant", "确认关闭的校门是南门。")
                    test.assertIn(messages[-1].content, (gate, library))
                    return Message("assistant", "南门明日关闭。" if messages[-1].content == gate else "图书馆闭馆时间待核实。")
                target = next(item.content for item in reversed(messages) if item.role == "user")
                if messages[-1].role != "tool":
                    return Message("assistant", tool_calls=[ToolCall("assess", CAMPUS_RUMOR.name, {"text": target})])
                return Message("assistant", "结论为有证据支持\n南门明日关闭。" if target == gate
                               else "结论为证据不足\n图书馆闭馆时间待核实。")

        role = AgentId.RUMOR_ASSESSMENT
        skill = replace(CAMPUS_RUMOR, handler=assess)
        runner = CampusRunner.with_model(Model(), skills_factory=lambda _: SkillRegistry({role: (skill,)}))
        with patch("main_agent.cloud.runner.load_subagent_capabilities", side_effect=lambda catalog: catalog):
            output = await runner.run(ExecutionContext(uuid4(), uuid4(), uuid4(), uuid4(), query, (), ()))
        self.assertCountEqual(called, [gate, library])
        self.assertEqual([step["agent_id"] for step in output.steps], [role.value, role.value])
        self.assertEqual(len({step["step_id"] for step in output.steps}), 2)
        self.assertEqual(output.result_status, "partial")
        for expected in ("南门明日关闭", "图书馆闭馆时间待核实", "等第 2 项核实", "确认关闭的校门是南门", "学分用于记录"):
            self.assertIn(expected, output.content)

    async def test_compound_turn_preserves_read_results_and_action_confirmation(self):
        general = "解释什么是预约"
        writes = [
            {"query": "取消体育馆预约 G42", "route": {"campus_action": {"action": "gym.close", "target": "G42"}}},
            {"query": "借阅《样例馆藏》", "route": {"campus_action": {"action": "library.start", "target": "样例馆藏"}}},
        ]
        previews = []

        class CampusChat:
            def read_handlers(self, _context):
                return {}

            async def discover(self, _context, catalog):
                return scope_actions(catalog, "student", {CAMPUS_ACTION.name: {"gym.close", "library.start"}})

            async def run_action(self, context, plan, _agent, *, confirm_first):
                previews.append((context.query, plan.target, confirm_first))
                return AgentReply(Status.NEEDS_INPUT, "请回复「确认办理」。", reason=CAMPUS_ACTION_CONFIRMATION), plan

        class Model:
            async def complete(self, messages, tools):
                instructions = messages[0].content
                if instructions.startswith("你负责校园智能体路由的范围判断"):
                    return Message("assistant", '{"compound":true}')
                if instructions.startswith("你负责校园智能体路由。"):
                    query = json.loads(messages[-1].content)["query"]
                    parts = [{"query": general, "route": {"general": True}}]
                    parts += writes if "借阅" in query else writes[:1]
                    return Message("assistant", json.dumps({"parts": parts}, ensure_ascii=False))
                if instructions.startswith(REVIEW_INSTRUCTIONS):
                    return Message("assistant", json.dumps({
                        "rule_checks": [{"rule_id": rule["id"], "status": "pass", "reason": "符合"}
                                        for rule in json.loads(messages[-1].content)["rules"]],
                        "claims": [], "issues": [],
                    }))
                return Message("assistant", "预约是预先约定时间。")

        runner = CampusRunner.with_model(Model(), campus_chat=CampusChat())
        query = "解释什么是预约，并帮我取消体育馆预约 G42"
        context = ExecutionContext(uuid4(), uuid4(), uuid4(), uuid4(), query, (), ())
        with patch("main_agent.cloud.runner.load_subagent_capabilities", side_effect=lambda catalog: catalog):
            many = await runner.run(replace(context, query=query + "，还请借阅《样例馆藏》"))
            self.assertFalse(previews)
            self.assertIn("预约是预先约定时间", many.content)
            self.assertIn("请先选择一项办理", many.content)
            one = await runner.run(context)
        self.assertEqual(previews, [(query, "G42", True)])
        self.assertIn("预约是预先约定时间", one.content)
        self.assertIn("确认办理", one.content)
        self.assertEqual(one.steps[0]["result"]["compound_reply"], one.content)

    async def test_compound_timeout_keeps_the_completed_answer(self):
        quick = "描述北斗七星的排列"
        slow = "解释潮汐的成因"

        class Model:
            async def complete(self, messages, tools):
                instructions = messages[0].content
                if instructions.startswith("你负责校园智能体路由的范围判断"):
                    return Message("assistant", '{"compound":true}')
                if instructions.startswith("你负责校园智能体路由。"):
                    return Message("assistant", json.dumps({"parts": [
                        {"query": query, "route": {"general": True}} for query in (slow, quick)
                    ]}))
                if messages[-1].content == slow:
                    await asyncio.Event().wait()
                return Message("assistant", "这七颗星的排列像一把斗勺。")

        runner = CampusRunner.with_model(Model())
        real_factory = runner.cluster_factory

        def cluster_factory(context):
            cluster = real_factory(context)
            cluster.config = replace(cluster.config, step_timeout=0.08)
            return cluster

        runner.cluster_factory = cluster_factory
        with patch("main_agent.cloud.runner.load_subagent_capabilities", side_effect=lambda catalog: catalog):
            output = await runner.run(ExecutionContext(
                uuid4(), uuid4(), uuid4(), uuid4(), f"{slow}，再{quick}", (), (),
            ))
        self.assertEqual(output.result_status, "partial")
        self.assertIn("这部分查询超时", output.content)
        self.assertIn("这七颗星的排列像一把斗勺", output.content)

    async def test_search_followup_keeps_the_original_goal_through_routing_and_answering(self):
        query = "对的，国家奖学金"
        resolved = "今年本科生国家奖学金获奖名单"
        history = (
            {"role": "user", "content": "给出今年国家奖学金获得者的名单"},
            {"role": "assistant", "content": "今年的获奖名单暂时无法确认。"},
            {"role": "user", "content": "本科生"},
            {"role": "assistant", "content": "想查公示名单，还是评审办法？"},
        )
        role = AgentId.CAMPUS_INTRODUCTION
        searches = []
        test = self

        class MCP:
            async def call_tool(self, name, args):
                if name == "get_subagent_capabilities":
                    return "{}"
                test.assertEqual(name, "search_rag")
                test.assertEqual(args["query"], resolved)
                searches.append(args["query"])
                return '{"hits":[]}'

        @asynccontextmanager
        async def mcp():
            yield MCP()

        class Model:
            async def complete(self, messages, tools):
                instructions = messages[0].content
                if instructions.startswith("你负责校园智能体路由的范围判断"):
                    payload = json.loads(messages[-1].content)
                    test.assertEqual(payload, {"query": query, "history": list(history)})
                    return Message("assistant", json.dumps({"campus": True, "resolved_query": resolved}))
                if instructions.startswith("你负责校园智能体路由"):
                    payload = json.loads(messages[-1].content)
                    test.assertEqual(payload["resolved_query"], resolved)
                    return Message("assistant", json.dumps(decision))
                if instructions.startswith(REVIEW_INSTRUCTIONS):
                    review = json.loads(messages[-1].content)
                    requests = [item["content"] for item in review["messages"] if item["role"] == "user"]
                    test.assertEqual(requests[-2:], [resolved_query_message(resolved).content, query])
                    return Message("assistant", json.dumps({
                        "rule_checks": [{"rule_id": rule["id"], "status": "pass", "reason": "符合"}
                                        for rule in review["rules"]],
                        "claims": [], "issues": [],
                    }))
                test.assertIn(resolved_query_message(resolved), messages)
                if messages[-1].role == "tool":
                    return Message("assistant", "暂时无法确认今年本科生国家奖学金获奖名单。")
                test.assertEqual(messages[-1], Message("user", query))
                if tools:
                    return Message("assistant", tool_calls=[ToolCall("search", "search_rag", {"query": resolved})])
                raise AssertionError("校园资料查询必须使用已注册的查询工具")

        runner = CampusRunner.with_model(Model())
        context = ExecutionContext(uuid4(), uuid4(), uuid4(), uuid4(), query, (), history)
        for decision, targets in (
            ({"targets": [role.value]}, [role.value]),
            ({"clarification": "想查名单还是申请流程？"}, []),
        ):
            with self.subTest(decision=decision), patch("main_agent.mcp_tools.connect_main_agent_mcp", mcp):
                searches.clear()
                output = await runner.run(context)
                self.assertIn(resolved, output.content)
                self.assertEqual([step["agent_id"] for step in output.steps], targets)
                self.assertTrue(searches)
                self.assertNotEqual(output.result_status, "needs_input")

    async def test_an_unresolved_safety_followup_keeps_same_chat_context_and_skips_campus_routing(self):
        prior = "我不想活了"
        followup = "我期末考试考查了"

        class Model:
            async def complete(self, messages, tools):
                self.calls.append((messages, tools))
                if messages[0].content.startswith("你负责校园智能体路由的范围判断"):
                    return Message("assistant", '{"general":true}')
                if Message("system", SUPPORT_RESPONSE) in messages:
                    return Message("assistant", "考试这件事让你很难受吧。你现在安全吗？")
                return Message("assistant", "你想了解哪门课的考查安排？")

        model = Model()
        model.calls = []
        runner = CampusRunner.with_model(model)
        context = ExecutionContext(uuid4(), uuid4(), uuid4(), uuid4(), followup, (), (
            {"role": "user", "content": prior}, {"role": "assistant", "content": "你现在安全吗？"},
        ))
        result = await runner.run(context)
        self.assertIn("你现在安全吗", result.content)
        self.assertEqual(model.calls[0][0][-3:], [
            Message("user", prior), Message("assistant", "你现在安全吗？"), Message("user", followup),
        ])
        self.assertEqual(model.calls[0][1], [])
        self.assertFalse(result.steps)
        other = await runner.run(replace(context, conversation_id=uuid4(), run_id=uuid4(), history=()))
        self.assertIn("考查安排", other.content)
        self.assertNotIn(Message("system", SUPPORT_RESPONSE), model.calls[-1][0])

    async def test_live_campus_threat_and_followup_preserve_context_before_any_lookup(self):
        initial = "我最近听说有人想来中传把学校炸了"
        followup = "消息是刚才在同学群看到的，我已经在校外"
        first_reply = "你听到的中传安全威胁还未证实。如果你附近可能有危险，先撤到安全处，拨打 110 并通知学校保卫处。你现在安全吗？"
        next_reply = "既然你已在校外，留在安全处，把群里原消息交给警方。"
        self.assertTrue(reports_campus_threat(initial))
        self.assertFalse(reports_campus_threat("帮我翻译「有人想把学校炸了」"))
        self.assertFalse(reports_campus_threat("如果有人要炸学校，演练中怎么处理？"))
        test = self

        class Model:
            fail = False

            async def complete(self, messages, tools):
                test.assertEqual(tools, [])
                if messages[0].content.startswith("你负责校园智能体路由的范围判断"):
                    payload = json.loads(messages[-1].content)
                    test.assertEqual(payload["history"][0]["content"], initial)
                    return Message("assistant", '{"support":true,"campus_threat":true}')
                test.assertIn(Message("system", CAMPUS_THREAT_RESPONSE), messages)
                if self.fail:
                    raise TimeoutError
                if messages[-1].content == followup:
                    test.assertEqual(messages[-3:], [
                        Message("user", initial), Message("assistant", first_reply), Message("user", followup),
                    ])
                    return Message("assistant", next_reply)
                return Message("assistant", first_reply)

        model = Model()
        runner = CampusRunner.with_model(model)
        context = ExecutionContext(uuid4(), uuid4(), uuid4(), uuid4(), initial, (), ())
        with patch("main_agent.mcp_tools.connect_main_agent_mcp", side_effect=AssertionError("即时安全回复不查资料")):
            first = await runner.run(context)
            second = await runner.run(replace(context, run_id=uuid4(), query=followup, history=(
                {"role": "user", "content": initial}, {"role": "assistant", "content": first.content},
            )))
            model.fail = True
            failed_model = await runner.run(context)
        self.assertEqual(first.content, first_reply)
        self.assertEqual(second.content, next_reply)
        self.assertFalse(first.steps or second.steps)
        self.assertEqual(failed_model.content, CAMPUS_THREAT_FALLBACK)

    async def test_clear_campus_rules_and_abnormal_changes_have_dedicated_roles(self):
        class Model:
            async def complete(self, messages, tools):
                if messages[0].content.startswith("你负责校园智能体路由的范围判断"):
                    return Message("assistant", '{"verify_change":true}')
                return Message("assistant", '{"direct":true}')

        router = LLMRouter(Model(), allow_direct=True, capabilities=lambda _: None)
        profiles = build_campus_cluster().registry.profiles
        for query, role in (
            ("我想了解校园体育场馆的开放时间和预约流程。", AgentId.CAMPUS_INTRODUCTION),
            ("校园体育馆开放时间是不是突然调整了？", AgentId.RUMOR_ASSESSMENT),
            ("听说期末考试全部改期了，是真的吗？", AgentId.RUMOR_ASSESSMENT),
            ("图书馆临时闭馆了？", AgentId.RUMOR_ASSESSMENT),
            ("南门 15 点后临时关闭吗？", AgentId.RUMOR_ASSESSMENT),
            ("请帮我核实这条校园消息是否属实：明天学生统一改线上课", AgentId.RUMOR_ASSESSMENT),
            ("这是真的吗？", AgentId.RUMOR_ASSESSMENT),
        ):
            with self.subTest(query=query):
                result = await router.route(ClusterRequest("chat", query), profiles)
                self.assertEqual([step.agent_id for step in result.steps], [role])
        self.assertIsNone(priority_campus_role("我想临时取消我的体育馆预约"))
        self.assertIsNone(priority_campus_role("体育馆突然取消了我的预约，能查原因吗？"))
        self.assertIsNone(priority_campus_role("听说学校哪里可以借书？"))
        self.assertIsNone(priority_campus_role("帮我翻译「学校突然关闭」"))

    async def test_questions_about_verification_channels_go_directly_to_main_agent(self):
        calls = []
        answer = "先在学校官网或发布部门的公众号按标题找原文，再核对日期和发布单位。"

        class Model:
            async def complete(self, messages, tools):
                if messages[0].content.startswith("你负责校园智能体路由的范围判断"):
                    return Message("assistant", '{"verification_guidance":true}')
                calls.append((messages[0].content, tools))
                return Message("assistant", answer)

        class CampusChat:
            def read_handlers(self, context):
                return {}

        runner = CampusRunner.with_model(Model(), campus_chat=CampusChat())
        targets = (AgentId.RUMOR_ASSESSMENT.value,)
        for query in ("收到一条转发的校园通知，我可以通过哪些渠道核实它的来源？",
                      "有人转发说图书馆临时闭馆，我可以通过哪些渠道核实通知是否属实？"):
            with self.subTest(query=query):
                self.assertIsNone(priority_campus_role(query))
                output = await runner.run(ExecutionContext(uuid4(), uuid4(), uuid4(), uuid4(), query, targets, ()))
                self.assertEqual(output.content, answer)
                self.assertFalse(output.steps)
        self.assertEqual(calls, [(GENERAL_ANSWER_INSTRUCTIONS, [])] * 2)
        self.assertEqual(priority_campus_role("图书馆临时闭馆是真的吗？也想知道有哪些渠道确认通知"),
                         AgentId.RUMOR_ASSESSMENT)

    async def test_wechat_question_reaches_information_search_and_main_agent_uses_the_article(self):
        article_id = "e" * 64
        text = "周六 14:00 在综合楼 301 开始。"
        url = "https://mp.weixin.qq.com/s/fixture"
        used = []

        async def article(_context, args):
            used.append(args.action)
            if args.action == "search":
                return ToolResult(json.dumps({"items": [{"id": article_id, "title": "开放日通知"}]}), "success")
            self.assertEqual(args.article_id, article_id)
            return ToolResult(json.dumps({"state": "text", "content": text, "url": url}, ensure_ascii=False), "success")

        class Model:
            async def complete(self, messages, tools):
                if messages[0].content.startswith(REVIEW_INSTRUCTIONS):
                    data = json.loads(messages[-1].content)
                    receipt = next(item for item in data["evidence"] if item["source"] == WECHAT_ARTICLES.name
                                   and item["kind"] == "tool" and item["content"].get("state") == "text")
                    return Message("assistant", json.dumps({
                        "rule_checks": [{"rule_id": rule["id"], "status": "pass", "reason": "符合"} for rule in data["rules"]],
                        "claims": [{"text": text, "kind": "fact", "status": "supported",
                                    "citations": [{"evidence_id": receipt["id"], "quote": text}]}], "issues": [],
                    }))
                if any(message.content == MAIN_ANSWER_INSTRUCTIONS for message in messages):
                    return Message("assistant", text + "\n" + url)
                if not used:
                    return Message("assistant", tool_calls=[ToolCall(
                        "search", WECHAT_ARTICLES.name, {"action": "search", "title": "开放日通知"},
                    )])
                if used == ["search"]:
                    return Message("assistant", tool_calls=[ToolCall(
                        "read", WECHAT_ARTICLES.name, {"action": "read", "article_id": article_id},
                    )])
                return Message("assistant", text)

        role = AgentId.INFORMATION_SEARCH
        skill = replace(WECHAT_ARTICLES, handler=article)
        runner = CampusRunner.with_model(Model(), skills_factory=lambda _: SkillRegistry({role: (skill,)}))
        query = "到公众号总成找开放日通知，打开正文看看活动几点开始"
        output = await runner.run(ExecutionContext(uuid4(), uuid4(), uuid4(), uuid4(), query, (), ()))
        self.assertEqual(used, ["search", "read"])
        self.assertEqual(output.steps[0]["agent_id"], role.value)
        self.assertEqual(output.content, text + "\n" + url)

    async def test_rumor_followup_uses_current_conversations_claim_and_skill(self):
        query = "北京校区本科生"
        claim = "听说明天北京校区本科生停课。"
        history = (
            {"role": "user", "content": "听说明天全校停课，属实吗？"},
            {"role": "assistant", "content": "是哪个校区和学生群体？"},
        )
        test = self
        invocations = []
        final_answer = "明天北京校区本科生是否停课还无法确认。如果方便，可以把最初的通知截图发给我核对。"

        async def assess(context, args):
            invocations.append((context.agent_id, args.text))
            return ToolResult('{"state":"assessed","assessment":{"kind":"rumor_candidate"},"verification":[]}', "success")

        class Model:
            async def complete(self, messages, tools):
                prefix = messages[0].content
                if prefix.startswith("你负责校园智能体路由的范围判断"):
                    test.assertEqual(json.loads(messages[-1].content)["history"], list(history))
                    return Message("assistant", json.dumps({"verify_change": True, "resolved_query": claim}))
                if prefix.startswith(REVIEW_INSTRUCTIONS):
                    review = json.loads(messages[-1].content)
                    return Message("assistant", json.dumps({
                        "rule_checks": [{"rule_id": rule["id"], "status": "pass", "reason": "符合"}
                                        for rule in review["rules"]],
                        "claims": [], "issues": [],
                    }))
                test.assertIn(resolved_query_message(claim), messages)
                if any(message.content == MAIN_ANSWER_INSTRUCTIONS for message in messages):
                    test.assertEqual(messages[-4:], [
                        Message("user", history[0]["content"]), Message("assistant", history[1]["content"]),
                        resolved_query_message(claim), Message("user", query),
                    ])
                    return Message("assistant", final_answer)
                test.assertIn(CAMPUS_RUMOR.name, {tool.name for tool in tools})
                if messages[-1].role == "tool":
                    return Message("assistant", "结论为证据不足\n这条停课消息暂未核实。")
                return Message("assistant", tool_calls=[ToolCall("assess", CAMPUS_RUMOR.name, {"text": claim})])

        skill = replace(CAMPUS_RUMOR, handler=assess)
        runner = CampusRunner.with_model(Model(), skills_factory=lambda _: SkillRegistry({
            AgentId.RUMOR_ASSESSMENT: (skill,),
        }))
        output = await runner.run(ExecutionContext(uuid4(), uuid4(), uuid4(), uuid4(), query, (), history))
        self.assertEqual(invocations, [(AgentId.RUMOR_ASSESSMENT, claim)])
        self.assertEqual(output.content, final_answer)
        self.assertEqual(output.result_status, "insufficient_evidence")
        self.assertEqual(output.steps[0]["agent_id"], AgentId.RUMOR_ASSESSMENT.value)

    async def test_live_service_workflow_guides_use_campus_services_and_offer_help(self):
        class Model:
            async def complete(self, messages, tools):
                instructions = messages[0].content
                if instructions.startswith("你负责校园智能体路由的范围判断"):
                    return Message("assistant", '{"campus":true}')
                if instructions.startswith("你负责校园智能体路由"):
                    self.route_prompt = instructions
                    return Message("assistant", self.decision)
                if instructions.startswith(REVIEW_INSTRUCTIONS):
                    review = json.loads(messages[-1].content)
                    return Message("assistant", json.dumps({
                        "rule_checks": [{"rule_id": rule["id"], "status": "pass", "reason": "符合"}
                                        for rule in review["rules"]],
                        "claims": [], "issues": [],
                    }))
                if instructions == MAIN_ANSWER_INSTRUCTIONS:
                    return Message("assistant", "在「校园应用 → 体育馆预约」选场馆、日期、时段和场地号，再核对预约信息。")
                self.specialist_prompt = "\n".join(message.content for message in messages)
                self.specialist_tools = {tool.name for tool in tools}
                return Message("assistant", "选择场馆、日期、时段和场地号，确认身份和预约明细。")

        class CampusChat:
            live = {"gym.start"}

            def read_handlers(self, _context):
                return {}

            async def discover(self, _context, agents):
                return scope_actions(agents, "student", {CAMPUS_ACTION.name: self.live})

            async def run_action(self, *_args, **_kwargs):
                raise AssertionError("咨询流程不能提交操作")

        model, campus = Model(), CampusChat()
        model.decision = '{"service_guide":["gym.start"]}'
        runner = CampusRunner.with_model(model, campus_chat=campus)
        context = ExecutionContext(
            uuid4(), uuid4(), uuid4(), uuid4(), "我想了解校园体育场馆的开放时间和预约流程。", (), (),
        )
        with patch("main_agent.cloud.runner.load_subagent_capabilities", side_effect=lambda catalog: catalog):
            output = await runner.run(context)
            self.assertEqual(output.steps[0]["agent_id"], AgentId.CAMPUS_SERVICES.value)
            self.assertIn('{"service_guide":["动作 ID"]}', model.route_prompt)
            self.assertIn("使用本人校园身份并确认预约明细", model.specialist_prompt)
            self.assertNotIn(CAMPUS_ACTION.name, model.specialist_tools)
            self.assertTrue(output.content.endswith("我也可以在这里帮你办理。"))

            campus.live = set()
            output = await runner.run(replace(context, run_id=uuid4()))
            self.assertFalse(output.steps)
            self.assertNotIn("帮你办理", output.content)

            model.decision = '{"policy_lookup":true}'
            output = await runner.run(replace(context, run_id=uuid4(), query="学校的学生请假管理规定原文在哪里？"))
            self.assertEqual(output.steps[0]["agent_id"], AgentId.CAMPUS_INTRODUCTION.value)
            self.assertNotIn("帮你办理", output.content)

    async def test_unverified_campus_change_is_reported_as_unverified(self):
        question = "听说学校突然暂停了体育馆预约，是真的吗？"
        answer = "暂时无法确认学校是否暂停了体育馆预约。你有提到暂停预约的原通知吗？"

        class Model:
            async def complete(self, messages, tools):
                if messages[0].content.startswith(REVIEW_INSTRUCTIONS):
                    payload = json.loads(messages[-1].content)
                    return Message("assistant", json.dumps({
                        "rule_checks": [{"rule_id": rule["id"], "status": "pass", "reason": "符合"}
                                        for rule in payload["rules"]],
                        "claims": [], "issues": [],
                    }))
                if messages[0].content.startswith(REPAIR_INSTRUCTIONS):
                    self.repair = json.loads(messages[-1].content)
                    return Message("assistant", answer)
                if any(message.content == MAIN_ANSWER_INSTRUCTIONS for message in messages):
                    return Message("assistant", "这条校园消息暂未核实，请以校方正式通知为准。")
                self.specialist_prompt = messages[1].content
                return Message("assistant", "结论为证据不足\n暂时没有本次变动的可靠通知。")

        model = Model()
        runner = CampusRunner.with_model(model)
        result = await runner.run(ExecutionContext(
            uuid4(), uuid4(), uuid4(), uuid4(), question, (), (),
        ))
        self.assertEqual(result.steps[0]["agent_id"], AgentId.RUMOR_ASSESSMENT.value)
        self.assertIn("谣言判别", model.specialist_prompt)
        self.assertTrue(any(item["code"] == "missing_conversation_context" for item in model.repair["findings"]))
        self.assertEqual(model.repair["messages"][-1]["content"], question)
        self.assertEqual((result.result_status, result.content), ("insufficient_evidence", answer))

    async def test_shared_model_has_independent_history_and_skills_for_concurrent_sessions(self):
        role = AgentId.CAMPUS_INTRODUCTION
        notes = ("session-alpha-note", "session-beta-note")
        routes_started = asyncio.Event()
        routed = set()
        reviews = []
        main_reviews = []

        def skills_factory(context):
            async def note(task, args):
                self.assertEqual(task.conversation_id, str(context.conversation_id))
                return ToolResult(context.history[0]["content"], status="success")

            return SkillRegistry({role: (Skill("read_note", "查看本会话记录", SkillInput, note, read_only=True),)})

        class Model:
            async def complete(self, messages, tools):
                text = "\n".join(message.content for message in messages)
                included = [note for note in notes if note in text]
                if len(included) != 1:
                    raise AssertionError(f"模型请求混合了会话内容 {included}")
                note = included[0]
                instructions = messages[0].content
                if instructions.startswith("你负责校园智能体路由的范围判断"):
                    return Message("assistant", '{"campus":true}')
                if instructions.startswith("你负责校园智能体路由"):
                    self_roles = json.loads(instructions.splitlines()[-1])
                    services = next(item for item in self_roles if item["id"] == role.value)
                    if [item["name"] for item in services["skills"]] != ["read_note"]:
                        raise AssertionError("选角应使用本次运行的实际技能注册表")
                    routed.add(note)
                    if len(routed) == len(notes):
                        routes_started.set()
                    await routes_started.wait()
                    return Message("assistant", json.dumps({"targets": [role.value]}))
                if instructions.startswith(REVIEW_INSTRUCTIONS):
                    task = json.loads(messages[-1].content)
                    is_main = task["messages"][0]["content"] == MAIN_ANSWER_INSTRUCTIONS
                    bucket = main_reviews if is_main else reviews
                    bucket.append([item["content"] for item in task["evidence"] if item["content"] == note])
                    return Message("assistant", json.dumps({
                        "rule_checks": [
                            {"rule_id": rule["id"], "status": "pass", "reason": "符合"}
                            for rule in task["rules"]
                        ],
                        "claims": [],
                        "issues": [],
                    }))
                if any(message.content == MAIN_ANSWER_INSTRUCTIONS for message in messages):
                    return Message("assistant", "主助手整理：" + note)
                if messages[-1].role == "tool":
                    return Message("assistant", messages[-1].content)
                return Message("assistant", tool_calls=[ToolCall("read", "read_note", {})])

        runner = CampusRunner.with_model(Model(), skills_factory=skills_factory)
        user, instance = uuid4(), uuid4()
        @asynccontextmanager
        async def stale_catalog():
            yield StaleMCP()

        class StaleMCP:
            async def call_tool(self, name, arguments):
                return json.dumps({"agents": [{"id": role.value, "skills": []}]})

        with patch("main_agent.mcp_tools.connect_main_agent_mcp", stale_catalog):
            outputs = await asyncio.wait_for(asyncio.gather(*(
                runner.run(ExecutionContext(
                    user, instance, uuid4(), uuid4(), "继续", (), ({"role": "user", "content": note},),
                ))
                for note in notes
            )), timeout=5)
        self.assertEqual([output.result_status for output in outputs], ["completed", "completed"])
        self.assertEqual([output.steps[0]["result"]["content"] for output in outputs], list(notes))
        self.assertEqual([output.content for output in outputs], ["主助手整理：" + note for note in notes])
        self.assertCountEqual(reviews, [[note] for note in notes])
        self.assertCountEqual(main_reviews, [[note] for note in notes])

    async def test_router_generation_review_and_repair_share_the_configured_model(self):
        stages = []
        catalog = describe_capabilities(build_campus_cluster().registry.profiles, default_skill_registries())
        test = self

        class MCP:
            async def call_tool(self, name, arguments):
                test.assertEqual((name, arguments), ("get_subagent_capabilities", {}))
                stages.append("capabilities")
                return json.dumps({"agents": catalog})

        @asynccontextmanager
        async def connect():
            yield MCP()

        class Model:
            async def complete(self, messages, tools):
                instructions = messages[0].content
                if instructions.startswith("你负责校园智能体路由的范围判断"):
                    test.assertNotIn(Message("system", MAIN_AGENT_PERSONA), messages)
                    stages.append("scope")
                    return Message("assistant", '{"campus":true}')
                if instructions.startswith("你负责校园智能体路由"):
                    test.assertNotIn(Message("system", MAIN_AGENT_PERSONA), messages)
                    stages.append("route")
                    roles = json.loads(instructions.splitlines()[-1])
                    test.assertEqual([role["id"] for role in roles], [role.value for role in AgentId])
                    test.assertEqual([role["id"] for role in roles if any(
                        skill["name"] == "campus_street_view" for skill in role["skills"]
                    )], [AgentId.CAMPUS_INTRODUCTION.value])
                    return Message("assistant", json.dumps({"targets": [role.value for role in AgentId]}))
                if instructions.startswith(REVIEW_INSTRUCTIONS):
                    task = json.loads(messages[-1].content)
                    stages.append("main_review" if task["messages"][0]["content"] == MAIN_ANSWER_INSTRUCTIONS else "review")
                    return Message("assistant", json.dumps({
                        "rule_checks": [
                            {"rule_id": rule["id"], "status": "pass", "reason": "符合"}
                            for rule in task["rules"]
                        ],
                        "claims": [],
                        "issues": [],
                    }))
                if instructions.startswith(REPAIR_INSTRUCTIONS):
                    stages.append("repair")
                    return Message("assistant", "结论为证据不足\n请提供待核实说法及其来源。")
                if any(message.content == MAIN_ANSWER_INSTRUCTIONS for message in messages):
                    test.assertIn(Message("system", MAIN_AGENT_PERSONA), messages)
                    stages.append("main_answer")
                    return Message("assistant", "我可以先帮你核对具体的资料来源。")
                test.assertNotIn(Message("system", MAIN_AGENT_PERSONA), messages)
                stages.append("generate")
                if "谣言判别" in messages[1].content:
                    return Message("assistant", "结论为有证据支持")
                return Message("assistant", "建议先核对学校公开资料。")

        runner = CampusRunner.with_model(Model())
        with patch("main_agent.mcp_tools.connect_main_agent_mcp", connect):
            result = await runner.run(ExecutionContext(
                uuid4(), uuid4(), uuid4(), uuid4(), "请协助核对校园资料", (), (),
            ))
        self.assertEqual(result.result_status, "completed")
        self.assertEqual([step["agent_id"] for step in result.steps], [role.value for role in AgentId])
        self.assertEqual(result.steps[-1]["result"]["rumor_verdict"], "insufficient_evidence")
        self.assertEqual(result.content, "我可以先帮你核对具体的资料来源。")
        self.assertEqual(stages[:3], ["scope", "capabilities", "route"])
        self.assertCountEqual(stages[3:], ["repair", "main_answer", "main_review"] + ["generate", "review"] * 4)

    async def test_general_questions_use_the_model_without_campus_tools_or_review(self):
        calls = []
        query = "你知道359*896等于多少吗？"

        class Model:
            async def complete(self, messages, tools):
                calls.append((messages, tools))
                if messages[0].content.startswith("你负责校园智能体路由"):
                    return Message("assistant", '{"general":true}')
                if messages[0].content == GENERAL_ANSWER_INSTRUCTIONS:
                    return Message("assistant", "359 × 896 = 321664。")
                raise AssertionError("通用问题不应进入校园检索或核验")

        runner = CampusRunner.with_model(Model())
        with patch("main_agent.mcp_tools.connect_main_agent_mcp", side_effect=AssertionError("通用问题调用了 MCP")):
            result = await runner.run(ExecutionContext(
                uuid4(), uuid4(), uuid4(), uuid4(), query, (AgentId.CAMPUS_SERVICES.value,),
                ({"role": "user", "content": "请帮我做一道计算题。"},),
            ))
        self.assertEqual(result.content, "359 × 896 = 321664。")
        self.assertEqual(result.result_status, "completed")
        self.assertEqual(result.steps, ())
        self.assertTrue(result.artifacts[0]["payload"]["general"])
        self.assertEqual([tools for _, tools in calls], [[], []])
        self.assertIn("严格、保守", calls[0][0][0].content)
        self.assertIn("包含通用问题或需要查询后再办理", calls[0][0][0].content)
        self.assertEqual(calls[1][0][1], Message("system", MAIN_AGENT_PERSONA))
        self.assertEqual([message.content for message in calls[1][0][2:]], ["请帮我做一道计算题。", query])

    async def test_navigation_keeps_the_location_result_after_the_tool_limit(self):
        from navigation_fixture import navigation_fixture
        self.enterContext(navigation_fixture())
        question = "从校门到图书馆怎么走？"
        answer = "你打算从哪个校门出发？我可以按出发点帮你确认路线。"
        intro = AgentId.CAMPUS_INTRODUCTION
        test = self

        class Model:
            async def complete(self, messages, tools):
                system = messages[0].content
                if "范围判断" in system:
                    return Message("assistant", '{"campus":true}')
                if system.startswith("你负责校园智能体路由"):
                    return Message("assistant", '{"targets":["campus_introduction"]}')
                if system.startswith(REVIEW_INSTRUCTIONS):
                    review = json.loads(messages[-1].content)
                    places = [entry for entry in review["evidence"] if entry["source"] == "campus_street_view"]
                    test.assertEqual(len(places), 1)
                    test.assertEqual(json.loads(places[0]["content"])["point"]["id"], "library")
                    return Message("assistant", json.dumps({
                        "rule_checks": [{"rule_id": rule["id"], "status": "pass", "reason": "符合"}
                                        for rule in review["rules"]],
                        "claims": [], "issues": [],
                    }))
                if any(message.content == MAIN_ANSWER_INSTRUCTIONS for message in messages):
                    return Message("assistant", answer)
                names = {tool.name for tool in tools}
                test.assertIn("campus_street_view", names)
                count = 9 if messages[-1].role == "tool" else 1
                prefix = "repeat" if count > 1 else "first"
                return Message("assistant", tool_calls=[
                    ToolCall(f"{prefix}-{index}", "campus_street_view", {"point": "图书馆"})
                    for index in range(count)
                ])

        runner = CampusRunner.with_model(Model(), skills_factory=lambda _: SkillRegistry({intro: (CAMPUS_STREET_VIEW,)}))
        with patch("main_agent.mcp_tools.connect_main_agent_mcp", side_effect=TimeoutError):
            output = await runner.run(ExecutionContext(uuid4(), uuid4(), uuid4(), uuid4(), question, (intro.value,), ()))
        self.assertEqual(output.content, answer)
        self.assertEqual(output.result_status, "insufficient_evidence")
        self.assertEqual(output.steps[0]["result"]["reason"], "tool_limit_reached")

    async def test_navigation_provides_the_route_to_the_main_answer_and_reviewer(self):
        from navigation_fixture import navigation_fixture
        self.enterContext(navigation_fixture())
        question = "从西门走到阅览室怎么走？"
        answer = "从西门沿测试主路向东，再沿测试图书馆道路向东。"
        intro = AgentId.CAMPUS_INTRODUCTION
        seen = []
        test = self

        class Model:
            async def complete(self, messages, tools):
                system = messages[0].content
                if "范围判断" in system:
                    return Message("assistant", '{"campus":true}')
                if system.startswith("你负责校园智能体路由"):
                    return Message("assistant", '{"targets":["campus_introduction"]}')
                if system.startswith(REVIEW_INSTRUCTIONS):
                    payload = json.loads(messages[-1].content)
                    receipt = next(item for item in payload["evidence"] if item["source"] == "campus_street_view")
                    route = json.loads(receipt["content"]) if receipt["kind"] == "reference" else receipt["content"]
                    test.assertEqual((route["status"], route["destination"]["name"]), ("walking_route", "图书馆"))
                    seen.append(receipt["kind"])
                    return Message("assistant", json.dumps({
                        "rule_checks": [{"rule_id": rule["id"], "status": "pass", "reason": "符合"}
                                        for rule in payload["rules"]],
                        "claims": [{"text": "测试主路", "kind": "fact", "status": "supported",
                                    "citations": [{"evidence_id": receipt["id"], "quote": "测试主路"}]}], "issues": [],
                    }))
                if any(message.content == MAIN_ANSWER_INSTRUCTIONS for message in messages) or messages[-1].role == "tool":
                    return Message("assistant", answer)
                return Message("assistant", tool_calls=[ToolCall(
                    "walk", "campus_street_view", {"start": "西门", "destination": "阅览室"},
                )])

        runner = CampusRunner.with_model(Model(), skills_factory=lambda _: SkillRegistry({intro: (CAMPUS_STREET_VIEW,)}))
        with patch("main_agent.mcp_tools.connect_main_agent_mcp", side_effect=TimeoutError):
            output = await runner.run(ExecutionContext(uuid4(), uuid4(), uuid4(), uuid4(), question, (), ()))
        self.assertEqual((output.result_status, output.content), ("completed", answer), output.steps)
        self.assertEqual(seen, ["tool", "reference"])

    async def test_campus_digest_receipts_reach_mainagent_when_the_specialist_cannot_finish(self):
        question = "你知道最近有什么校友交流会吗"
        title = "测试来源：校友座谈会"
        answer = f"最近有一则已举办活动的报道《{title}》。我暂时无法确认近期可报名的校友交流会。"
        unverified = "未经核验的说法：明天举办校友会。"
        raw = json.dumps({"items": [{"title": title, "published_on": "2026-11-11"}]}, ensure_ascii=False)
        role = AgentId.INFORMATION_SEARCH
        test = self

        async def search(_context, args):
            tool_calls.append(args.query)
            return ToolResult(raw, "success")

        skill = replace(CAMPUS_DIGEST, handler=search)

        class Model:
            async def complete(self, messages, tools):
                system = messages[0].content
                if "范围判断" in system:
                    return Message("assistant", '{"campus":true}')
                if system.startswith("你负责校园智能体路由"):
                    return Message("assistant", '{"targets":["information_search"]}')
                if system.startswith(REVIEW_INSTRUCTIONS):
                    payload = json.loads(messages[-1].content)
                    is_main = payload["messages"][0]["content"] == MAIN_ANSWER_INSTRUCTIONS
                    review_passes.append(is_main)
                    if not is_main or failure == "invalid_main_review":
                        return Message("assistant", '{"rule_checks":[],"claims":[],"issues":[]}')
                    receipt = next(item for item in payload["evidence"] if item["source"] == CAMPUS_DIGEST.name)
                    test.assertEqual(receipt["content"], raw)
                    test.assertEqual(receipt["kind"], "reference")
                    test.assertNotIn(unverified, messages[-1].content)
                    return Message("assistant", json.dumps({
                        "rule_checks": [{"rule_id": rule["id"], "status": "pass", "reason": "符合"}
                                        for rule in payload["rules"]],
                        "claims": [{"text": title, "kind": "fact", "status": "supported",
                                    "citations": [{"evidence_id": receipt["id"], "quote": title}]}],
                        "issues": [],
                    }))
                if any(message.content == MAIN_ANSWER_INSTRUCTIONS for message in messages):
                    return Message("assistant", answer)
                if messages[-1].role == "tool":
                    if failure == "invalid_tool_batch":
                        return Message("assistant", tool_calls=[ToolCall("follow-up", CAMPUS_DIGEST.name, {"days": -1})])
                    if failure == "step_timeout":
                        await asyncio.Future()
                    return Message("assistant", unverified)
                return Message("assistant", tool_calls=[ToolCall("digest", CAMPUS_DIGEST.name, {"query": "校友"})])

        for failure, reason, reviews in (
            ("invalid_specialist_review", "invalid_review", [False, False, True]),
            ("invalid_tool_batch", "agent_error", [True]),
            ("step_timeout", "agent_timeout", [True]),
            ("invalid_main_review", "invalid_review", [False, False, True, True]),
        ):
            with self.subTest(failure=failure):
                tool_calls = []
                review_passes = []
                runner = CampusRunner.with_model(Model(), skills_factory=lambda _: SkillRegistry({role: (skill,)}))
                if failure == "step_timeout":
                    make_cluster = runner.cluster_factory

                    def timed_cluster(context):
                        cluster = make_cluster(context)
                        cluster.config = ClusterConfig(step_timeout=0.05)
                        return cluster

                    runner.cluster_factory = timed_cluster
                with patch("main_agent.mcp_tools.connect_main_agent_mcp", side_effect=TimeoutError):
                    output = await runner.run(ExecutionContext(uuid4(), uuid4(), uuid4(), uuid4(), question, (), ()))
                if failure == "invalid_main_review":
                    self.assertIn(question, output.content)
                    self.assertIn("暂时无法确认", output.content)
                else:
                    self.assertEqual(output.content, answer)
                self.assertEqual(output.result_status, "insufficient_evidence" if failure == "invalid_main_review" else "partial")
                self.assertEqual(output.steps[0]["result"]["reason"], reason)
                self.assertEqual(tool_calls, ["校友"])
                self.assertEqual(review_passes, reviews)

    async def test_opening_questions_use_the_persona_and_keep_a_fixed_fallback(self):
        calls = []

        class Model:
            async def complete(self, messages, tools):
                calls.append((messages, tools))
                if messages[-1].content == "你是谁？":
                    raise TimeoutError
                return Message("assistant", "你好，今天想聊些什么？")

        runner = CampusRunner.with_model(Model())
        with patch.object(runner, "cluster_factory", side_effect=AssertionError("问候和身份问题应直接回答")):
            greeting = await runner.run(ExecutionContext(uuid4(), uuid4(), uuid4(), uuid4(), "你好！", (), ()))
            identity = await runner.run(ExecutionContext(uuid4(), uuid4(), uuid4(), uuid4(), "你是谁？", (), ()))
        self.assertEqual(greeting.content, "你好，今天想聊些什么？")
        self.assertEqual(identity.content, ASSISTANT_INTRODUCTION)
        self.assertEqual([greeting.result_status, identity.result_status], ["completed", "completed"])
        self.assertEqual([tools for _, tools in calls], [[], []])
        for messages, _ in calls:
            self.assertEqual(messages[1], Message("system", MAIN_AGENT_PERSONA))
            self.assertIn(ASSISTANT_INTRODUCTION, messages[2].content)

    async def test_main_agent_searches_rag_and_keeps_search_reports_out_of_the_reply(self):
        calls = []
        evidence_text = "志愿者服务中心\n值班电话 010-00000000"
        search_report = (
            "我按‘志愿者服务中心值班电话’等关键词在校园资料库中做了多次检索，结果均为空。"
            "本次查到的资料只有学校概况，中国传媒大学于1954年创办。还需要补充值班安排与联系电话。"
        )
        unavailable = "志愿者服务中心的值班电话暂时无法确认。"
        unsupported = "志愿者服务中心值班电话是 999。"
        question = "志愿者服务中心值班电话是什么？"

        class MCP:
            async def call_tool(self, name, arguments):
                calls.append((name, arguments))
                return json.dumps({"scope": "campus", "hits": hits}, ensure_ascii=False)

        @asynccontextmanager
        async def connect():
            yield MCP()

        class Model:
            async def complete(self, messages, tools):
                instructions = messages[0].content
                if instructions.startswith("你负责校园智能体路由的范围判断"):
                    return Message("assistant", '{"general":true}' if route == "general" else '{"campus":true}')
                if instructions.startswith("你负责校园智能体路由"):
                    self.request = json.loads(messages[-1].content)
                    decision = {"targets": ["campus_services"]} if route == "delegated" else {"direct": True}
                    return Message("assistant", json.dumps(decision))
                if instructions == GENERAL_ANSWER_INSTRUCTIONS:
                    return Message("assistant", CAMPUS_CONTEXT_REQUIRED)
                if not any(message.content == MAIN_ANSWER_INSTRUCTIONS for message in messages):
                    return Message("assistant", unsupported)
                if instructions.startswith(REPAIR_INSTRUCTIONS):
                    payload = json.loads(messages[-1].content)
                    self.repair_messages = messages
                    self.search_report_rejected = any(item["code"] == "internal_search_report" for item in payload["findings"])
                    return Message("assistant", unavailable if repaired else search_report)
                if messages[-1].role == "tool":
                    return Message("assistant", evidence_text if hits else search_report)
                return Message("assistant", tool_calls=[ToolCall("rag", "search_rag", {"query": "志愿者服务中心值班电话"})])

            async def complete_json(self, messages):
                payload = json.loads(messages[-1].content)
                checks = [{"rule_id": rule["id"], "status": "pass", "reason": "符合"} for rule in payload["rules"]]
                if payload["messages"][0]["content"] != MAIN_ANSWER_INSTRUCTIONS:
                    return Message("assistant", json.dumps({
                        "rule_checks": checks,
                        "claims": [{"text": unsupported, "kind": "fact", "status": "unsupported", "citations": []}],
                        "issues": [],
                    }))
                receipt = next(item for item in payload["evidence"] if item["source"] == "search_rag")
                self.structured_receipt = receipt["content"]
                self.review_messages = payload["messages"]
                return Message("assistant", json.dumps({
                    "rule_checks": checks,
                    "claims": [{"text": payload["answer"], "kind": "fact", "status": "supported",
                                "citations": [{"evidence_id": receipt["id"], "quote": evidence_text}]}] if hits else [],
                    "issues": [],
                }))

        for route, hits, repaired, status, content in (
            ("direct", [{"text": evidence_text}], True, "completed", evidence_text),
            ("delegated", [{"text": evidence_text}], True, "partial", evidence_text),
            ("general", [{"text": evidence_text}], True, "completed", evidence_text),
            ("delegated", [], True, "insufficient_evidence", unavailable),
            ("direct", [], False, "insufficient_evidence", f"关于 ` {question} `，我暂时无法确认。"),
        ):
            with self.subTest(route=route, repaired=repaired), patch("main_agent.mcp_tools.connect_main_agent_mcp", connect):
                calls.clear()
                model = Model()
                runner = CampusRunner.with_model(model, skills_factory=lambda _: SkillRegistry({}))
                result = await runner.run(ExecutionContext(
                    uuid4(), uuid4(), uuid4(), uuid4(), question,
                    (AgentId.CAMPUS_SERVICES.value,), (),
                ))
                self.assertEqual(model.request["suggested_targets"], [AgentId.CAMPUS_SERVICES.value])
                self.assertEqual(result.result_status, status)
                self.assertEqual([step["status"] for step in result.steps], ["insufficient_evidence"] if route == "delegated" else [])
                self.assertEqual(result.content, content)
                self.assertEqual(calls, [
                    ("get_subagent_capabilities", {}), ("search_rag", {"query": "志愿者服务中心值班电话", "top_k": 5}),
                ])
                if repaired:
                    self.assertEqual(model.structured_receipt["hits"], hits)
                    self.assertFalse(any(message["role"] == "tool" for message in model.review_messages))
                if not hits:
                    self.assertTrue(model.search_report_rejected)
                    self.assertIn(Message("system", MAIN_AGENT_PERSONA), model.repair_messages)
                    snapshot = next(item["payload"] for item in result.artifacts if item["kind"] == "main_answer_snapshot")
                    self.assertEqual(snapshot["attempts"][0]["answer"], search_report)

    async def test_explicit_targets_skip_model_routing_and_unknown_roles_cannot_be_dispatched(self):
        calls = []

        class Model:
            async def complete(self, messages, tools):
                calls.append(tools)
                return Message("assistant", '{"targets":["unknown_role"]}')

        profiles = build_campus_cluster().registry.profiles
        router = LLMRouter(Model())
        target = AgentId.INFORMATION_SEARCH
        explicit = await router.route(ClusterRequest("chat", "问题", (target,)), profiles)
        self.assertEqual([step.agent_id for step in explicit.steps], [target])
        self.assertEqual(calls, [])
        invalid = await router.route(ClusterRequest("chat", "问题"), profiles)
        self.assertFalse(invalid.steps)
        self.assertTrue(invalid.clarification)
        self.assertEqual(calls, [[]])
