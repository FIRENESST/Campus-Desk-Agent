import json
import os
import unittest
from unittest.mock import AsyncMock, call, patch

from main_agent.campus_cluster.agents import AGENT_TYPES
from main_agent.campus_cluster.contracts import AgentId, AgentTask
from main_agent.campus_cluster.subskills import minta_gateway
from main_agent.minta import MintaResponse
from main_agent.reliability import InvalidDraftError


def skills(role):
    agent = AGENT_TYPES[role]()
    task = AgentTask("chat", "step", agent.profile, "查询")
    return agent.skill_registry.bind(task, read_only=True)


def response(status, body):
    return MintaResponse(status, [], [], json.dumps(body).encode("utf-8"))


class MintaSkillTests(unittest.IsolatedAsyncioTestCase):
    def test_explicit_club_audience_is_parsed_as_two_fields(self):
        from main_agent.campus_cluster.subskills.minta_campus import campus_intent

        intent = campus_intent("修改社团摄影社，开放年级：大一、大二，开放学院：新闻学院")
        self.assertEqual((intent.action, intent.target), ("update", "摄影社"))
        self.assertEqual(intent.fields, {"allow_grades": "大一、大二", "allow_colleges": "新闻学院"})

    async def test_registered_as_read_only_for_each_default_role(self):
        for role in AgentId:
            with self.subTest(role=role):
                self.assertIn("minta_gateway", skills(role).names)
                self.assertEqual("campus_street_view" in skills(role).names, role == AgentId.CAMPUS_INTRODUCTION)

    async def test_discovers_and_calls_the_service_query_tool(self):
        parameters = {"type": "object", "required": ["student_id"]}
        tool = {"name": "schedule_query", "description": "查询课表", "parameters": parameters}
        gateway = AsyncMock()
        gateway.call.side_effect = [
            response(200, {"tools": [tool, {"name": "change_schedule"}]}),
            response(200, {"content": '{"student_id":"2025001","courses":[]}'}),
        ]
        client = skills(AgentId.INFORMATION_SEARCH)
        arguments = {"student_id": "2025001", "week": 10}
        with (
            patch.dict(os.environ, MAIN_AGENT_MINTA_URL="", MAIN_AGENT_MINTA_TOKEN=""),
            patch.object(minta_gateway, "MintaGatewayClient", return_value=gateway) as make_gateway,
        ):
            listing = await client.call_tool("minta_gateway", {"service": "schedule", "action": "list_tools"})
            result = await client.call_tool("minta_gateway", {
                "service": "schedule", "action": "call_tool", "tool": "schedule_query", "arguments": arguments,
            })
        self.assertEqual(listing.status, "success")
        self.assertEqual(json.loads(listing.content)["tools"], [tool])
        self.assertEqual(result.status, "success")
        self.assertEqual(json.loads(result.content), {
            "source": "minta_simulation", "service": "schedule", "tool": "schedule_query",
            "data": {"student_id": "2025001", "courses": []},
        })
        self.assertEqual(make_gateway.call_args.args, ("http://127.0.0.1:19000",))
        self.assertEqual(gateway.call.await_args_list, [
            call("GET", "/schedule/agent/tools", timeout_ms=5_000),
            call("POST", "/schedule/agent/call", timeout_ms=5_000,
                 json={"name": "schedule_query", "arguments": arguments}),
        ])

    async def test_rejects_unlisted_tools_before_connecting(self):
        client = skills(AgentId.CAMPUS_SERVICES)
        for tool in ("gym_reserve_venue", "schedule_query"):
            with self.subTest(tool=tool), patch.object(minta_gateway, "MintaGatewayClient") as gateway:
                with self.assertRaises(InvalidDraftError):
                    await client.call_tool("minta_gateway", {"service": "gym", "action": "call_tool", "tool": tool})
                gateway.assert_not_called()

    async def test_backend_failure_remains_an_error_receipt(self):
        gateway = AsyncMock()
        gateway.call.return_value = response(404, {"error": {"code": "NOT_FOUND"}})
        with (
            patch.dict(os.environ, MAIN_AGENT_MINTA_URL="", MAIN_AGENT_MINTA_TOKEN=""),
            patch.object(minta_gateway, "MintaGatewayClient", return_value=gateway),
        ):
            result = await skills(AgentId.CAMPUS_SERVICES).call_tool(
                "minta_gateway", {"service": "library", "action": "call_tool", "tool": "library_list_books"},
            )
        self.assertEqual(result.status, "error")
        self.assertEqual(json.loads(result.content)["error"]["http_status"], 404)

    async def test_library_search_uses_a_bounded_anonymous_catalog(self):
        books = [
            {"id": f"b-{index}", "title": f"Book {index}", "author": "Demo Author",
             "category_id": "computing", "category": "计算机与技术", "total_copies": 2, "available_copies": 2,
             "borrower_name": "private"}
            for index in range(1, 9)
        ]
        gateway = AsyncMock()
        gateway.call.return_value = response(200, {"content": json.dumps({"books": books})})
        with (
            patch.dict(os.environ, MAIN_AGENT_MINTA_URL="", MAIN_AGENT_MINTA_TOKEN=""),
            patch.object(minta_gateway, "MintaGatewayClient", return_value=gateway),
        ):
            result = await skills(AgentId.CAMPUS_SERVICES).call_tool("minta_gateway", {
                "service": "library", "action": "call_tool", "tool": "library_list_books",
                "arguments": {"q": "Ｄｅｍｏ", "category": "计算机与技术", "page": 2},
            })
        self.assertEqual(result.status, "success")
        self.assertNotIn("borrower_name", result.content)
        data = json.loads(result.content)["data"]
        self.assertEqual((data["total"], data["page"]), (8, 2))
        self.assertEqual([item["id"] for item in data["books"]], ["b-7", "b-8"])
        self.assertEqual(gateway.call.await_args.kwargs["json"]["arguments"], {})
