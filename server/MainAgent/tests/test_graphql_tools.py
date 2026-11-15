import json
import unittest

import httpx2

from main_agent.graphql_tools import (
    GraphQLProtocolError,
    GraphQLRequestError,
    GraphQLToolClient,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeHTTPClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class GraphQLToolClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_tools_uses_graphql_and_parses_json_schemas(self):
        http = FakeHTTPClient(
            [
                FakeResponse(
                    {
                        "data": {
                            "agentTools": [
                                {
                                    "name": "club_list_activities",
                                    "description": "查询活动",
                                    "parameters": '{"type":"object"}',
                                }
                            ]
                        }
                    }
                )
            ]
        )
        client = GraphQLToolClient("http://127.0.0.1:8102/graphql", client=http)

        tools = await client.list_tools()

        self.assertEqual(tools[0].name, "club_list_activities")
        self.assertEqual(tools[0].parameters, {"type": "object"})
        self.assertIn("query AgentTools", http.requests[0][1]["json"]["query"])

    async def test_call_tool_uses_mutation_and_serializes_arguments(self):
        http = FakeHTTPClient(
            [
                FakeResponse(
                    {"data": {"callTool": {"content": "报名成功", "isError": False}}}
                )
            ]
        )
        client = GraphQLToolClient("http://127.0.0.1:8102/graphql", client=http)

        result = await client.call_tool(
            "club_register_activity",
            {"activity_id": "activity_1", "participant_name": "小明"},
        )

        self.assertEqual(result, "报名成功")
        request = http.requests[0][1]["json"]
        self.assertIn("mutation CallTool", request["query"])
        self.assertEqual(request["variables"]["name"], "club_register_activity")
        self.assertEqual(
            json.loads(request["variables"]["argumentsJson"]),
            {"activity_id": "activity_1", "participant_name": "小明"},
        )

    async def test_tool_error_is_returned_for_model_recovery(self):
        http = FakeHTTPClient(
            [
                FakeResponse(
                    {
                        "data": {
                            "callTool": {
                                "content": "CONFLICT: 已报名",
                                "isError": True,
                            }
                        }
                    }
                )
            ]
        )
        client = GraphQLToolClient("http://127.0.0.1:8102/graphql", client=http)

        result = await client.call_tool("club_register_activity", {})

        self.assertEqual(result, "工具执行失败：\nCONFLICT: 已报名")

    async def test_graphql_errors_are_request_failures_not_tool_results(self):
        http = FakeHTTPClient(
            [FakeResponse({"errors": [{"message": "上游敏感细节"}]})]
        )
        client = GraphQLToolClient("http://127.0.0.1:8102/graphql", client=http)

        with self.assertRaises(GraphQLRequestError) as caught:
            await client.list_tools()

        self.assertNotIn("敏感细节", str(caught.exception))

    async def test_transport_failures_use_stable_error_boundaries(self):
        cases = [
            (httpx2.ReadTimeout("读取细节"), TimeoutError),
            (httpx2.ConnectError("连接细节"), GraphQLRequestError),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                client = GraphQLToolClient(
                    "http://127.0.0.1:8102/graphql",
                    client=FakeHTTPClient([error]),
                )
                with self.assertRaises(expected) as caught:
                    await client.list_tools()
                self.assertIs(caught.exception.__cause__, error)

    async def test_malformed_schema_and_tool_result_are_rejected(self):
        bad_list_payloads = [
            {"data": {"agentTools": [
                {"name": "x", "description": "x", "parameters": "not-json"}
            ]}},
            {"data": {"agentTools": [
                {"name": "x", "description": "x", "parameters": "[]"}
            ]}},
        ]
        for payload in bad_list_payloads:
            with self.subTest(payload=payload):
                client = GraphQLToolClient(
                    "http://127.0.0.1:8102/graphql",
                    client=FakeHTTPClient([FakeResponse(payload)]),
                )
                with self.assertRaises(GraphQLProtocolError):
                    await client.list_tools()

        client = GraphQLToolClient(
            "http://127.0.0.1:8102/graphql",
            client=FakeHTTPClient(
                [FakeResponse({"data": {"callTool": {"content": 1, "isError": False}}})]
            ),
        )
        with self.assertRaises(GraphQLProtocolError):
            await client.call_tool("club_list_activities", {})


if __name__ == "__main__":
    unittest.main()
