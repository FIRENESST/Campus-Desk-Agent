import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx2
from mcp import MCPError, types

from main_agent.mcp_tools import (
    MCPProtocolError,
    MCPRequestError,
    MCPToolClient,
    connect_mcp_http,
)


class FakeSession:
    def __init__(self, list_results=None, call_results=None):
        self._list_results = list(list_results or [])
        self._call_results = list(call_results or [])
        self.list_params = []
        self.calls = []

    async def list_tools(self, *, params=None):
        self.list_params.append(params)
        if not self._list_results:
            raise AssertionError("工具列表被请求了意料之外的次数")
        result = self._list_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if not self._call_results:
            raise AssertionError("工具被调用了意料之外的次数")
        result = self._call_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def tool(name, description=""):
    return types.Tool(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": {}},
    )


class MCPToolClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_tools_follows_every_cursor_page(self):
        session = FakeSession(
            list_results=[
                types.ListToolsResult(
                    tools=[tool("get_time", "获取时间")],
                    nextCursor="page_2",
                ),
                types.ListToolsResult(
                    tools=[tool("get_weather", "获取天气")],
                    nextCursor=None,
                ),
            ]
        )
        client = MCPToolClient(session=session)

        specs = await client.list_tools()

        self.assertEqual([spec.name for spec in specs], ["get_time", "get_weather"])
        self.assertEqual(specs[0].description, "获取时间")
        self.assertEqual(
            specs[0].parameters,
            {"type": "object", "properties": {}},
        )
        self.assertIsNone(session.list_params[0])
        self.assertEqual(session.list_params[1].cursor, "page_2")

    async def test_call_tool_converts_text_and_structured_results_for_the_model(self):
        session = FakeSession(
            call_results=[
                types.CallToolResult(
                    content=[
                        types.TextContent(type="text", text="第一行"),
                        types.TextContent(type="text", text="第二行"),
                    ],
                    isError=False,
                ),
                types.CallToolResult(
                    content=[],
                    structuredContent={"time": "10:00", "city": "上海"},
                    isError=False,
                ),
            ]
        )
        client = MCPToolClient(session=session)

        text_result = await client.call_tool("get_time", {})
        structured_result = await client.call_tool("get_weather", {"city": "上海"})

        self.assertEqual(text_result, "第一行\n第二行")
        self.assertEqual(
            structured_result,
            '{"time":"10:00","city":"上海"}',
        )
        self.assertEqual(
            session.calls,
            [("get_time", {}), ("get_weather", {"city": "上海"})],
        )

    async def test_tool_execution_error_is_returned_for_model_recovery(self):
        session = FakeSession(
            call_results=[
                types.CallToolResult(
                    content=[
                        types.TextContent(type="text", text="城市不存在"),
                    ],
                    isError=True,
                )
            ]
        )
        client = MCPToolClient(session=session)

        result = await client.call_tool("get_weather", {"city": "不存在"})

        self.assertEqual(result, "工具执行失败：\n城市不存在")

    async def test_non_text_content_is_rejected_instead_of_being_dropped(self):
        session = FakeSession(
            call_results=[
                types.CallToolResult(
                    content=[
                        types.ImageContent(
                            type="image",
                            data="aGVsbG8=",
                            mimeType="image/png",
                        )
                    ],
                    isError=False,
                )
            ]
        )
        client = MCPToolClient(session=session)

        with self.assertRaisesRegex(MCPProtocolError, "image"):
            await client.call_tool("get_image", {})

    async def test_unexpected_result_type_is_rejected(self):
        session = FakeSession(call_results=[types.Result()])
        client = MCPToolClient(session=session)

        with self.assertRaisesRegex(MCPProtocolError, "结果类型"):
            await client.call_tool("get_time", {})

    async def test_mcp_request_error_is_stable_and_does_not_leak_details(self):
        upstream_error = MCPError(-32603, "上游敏感信息")
        session = FakeSession(call_results=[upstream_error])
        client = MCPToolClient(session=session)

        with self.assertRaises(MCPRequestError) as caught:
            await client.call_tool("get_time", {})

        self.assertEqual(str(caught.exception), "MCP 请求失败")
        self.assertIs(caught.exception.__cause__, upstream_error)
        self.assertNotIn("敏感信息", str(caught.exception))

    async def test_http_connection_error_has_a_stable_agent_error(self):
        upstream_error = httpx2.ConnectError("连接细节")
        session = FakeSession(call_results=[upstream_error])
        client = MCPToolClient(session=session)

        with self.assertRaises(MCPRequestError) as caught:
            await client.call_tool("get_time", {})

        self.assertEqual(str(caught.exception), "MCP 请求失败")
        self.assertIs(caught.exception.__cause__, upstream_error)

    async def test_http_timeout_uses_the_core_timeout_contract(self):
        upstream_error = httpx2.ReadTimeout("读取超时")
        session = FakeSession(call_results=[upstream_error])
        client = MCPToolClient(session=session)

        with self.assertRaises(TimeoutError) as caught:
            await client.call_tool("get_time", {})

        self.assertIs(caught.exception.__cause__, upstream_error)

    async def test_list_tools_uses_the_same_request_error_boundary(self):
        upstream_error = MCPError(-32603, "工具清单敏感信息")
        session = FakeSession(list_results=[upstream_error])
        client = MCPToolClient(session=session)

        with self.assertRaises(MCPRequestError) as caught:
            await client.list_tools()

        self.assertEqual(str(caught.exception), "MCP 请求失败")
        self.assertIs(caught.exception.__cause__, upstream_error)

    async def test_repeated_pagination_cursor_is_rejected(self):
        session = FakeSession(
            list_results=[
                types.ListToolsResult(tools=[tool("first")], nextCursor="same"),
                types.ListToolsResult(tools=[tool("second")], nextCursor="same"),
            ]
        )
        client = MCPToolClient(session=session)

        with self.assertRaisesRegex(MCPProtocolError, "分页游标"):
            await client.list_tools()

        self.assertEqual(len(session.list_params), 2)

    async def test_duplicate_tool_names_across_pages_are_rejected(self):
        session = FakeSession(
            list_results=[
                types.ListToolsResult(tools=[tool("get_time")], nextCursor="page_2"),
                types.ListToolsResult(tools=[tool("get_time")], nextCursor=None),
            ]
        )
        client = MCPToolClient(session=session)

        with self.assertRaisesRegex(MCPProtocolError, "重复工具"):
            await client.list_tools()

    async def test_http_connection_initializes_and_closes_all_resources(self):
        events = []
        captured = {}

        class FakeHTTPClient:
            async def __aenter__(self):
                events.append("http_enter")
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                events.append("http_exit")

        class FakeClientSession:
            def __init__(self, read_stream, write_stream):
                captured["streams"] = (read_stream, write_stream)

            async def __aenter__(self):
                events.append("session_enter")
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                events.append("session_exit")

            async def initialize(self):
                events.append("initialize")

        http_client = FakeHTTPClient()

        def fake_http_factory(*, headers, timeout):
            captured["headers"] = headers
            captured["timeout"] = timeout
            return http_client

        @asynccontextmanager
        async def fake_transport(url, *, http_client):
            captured["url"] = url
            captured["http_client"] = http_client
            events.append("transport_enter")
            try:
                yield "read_stream", "write_stream"
            finally:
                events.append("transport_exit")

        headers = {"Authorization": "Bearer test-token"}
        timeout = httpx2.Timeout(10.0)
        with (
            patch(
                "main_agent.mcp_tools.create_mcp_http_client",
                fake_http_factory,
            ),
            patch("main_agent.mcp_tools.streamable_http_client", fake_transport),
            patch("main_agent.mcp_tools.ClientSession", FakeClientSession),
        ):
            async with connect_mcp_http(
                "https://mcp.example.com/mcp",
                headers=headers,
                timeout=timeout,
            ) as client:
                self.assertIsInstance(client, MCPToolClient)
                events.append("use")

        self.assertEqual(
            events,
            [
                "http_enter",
                "transport_enter",
                "session_enter",
                "initialize",
                "use",
                "session_exit",
                "transport_exit",
                "http_exit",
            ],
        )
        self.assertEqual(captured["url"], "https://mcp.example.com/mcp")
        self.assertEqual(captured["headers"], headers)
        self.assertIs(captured["timeout"], timeout)
        self.assertIs(captured["http_client"], http_client)
        self.assertEqual(captured["streams"], ("read_stream", "write_stream"))


if __name__ == "__main__":
    unittest.main()
