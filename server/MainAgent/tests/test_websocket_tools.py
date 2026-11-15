import json
import unittest

from main_agent.websocket_tools import (
    WebSocketProtocolError,
    WebSocketRequestError,
    WebSocketToolClient,
)


class FakeConnection:
    def __init__(self, responses):
        self._responses = list(responses)
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.closed = True

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, str):
            return result
        if result.get("id") is None:
            result["id"] = self.sent[-1]["id"]
        return json.dumps(result)


class WebSocketToolClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_tools_uses_correlated_websocket_messages(self):
        connection = FakeConnection(
            [
                {
                    "type": "tools",
                    "tools": [
                        {
                            "name": "classroom_list_rooms",
                            "description": "查询教室",
                            "parameters": {"type": "object"},
                        }
                    ],
                }
            ]
        )
        urls = []

        def connect(url):
            urls.append(url)
            return connection

        client = WebSocketToolClient("ws://127.0.0.1:8104/ws", connect=connect)

        tools = await client.list_tools()

        self.assertEqual(tools[0].name, "classroom_list_rooms")
        self.assertEqual(tools[0].parameters, {"type": "object"})
        self.assertEqual(urls, ["ws://127.0.0.1:8104/ws"])
        self.assertEqual(connection.sent[0]["type"], "list_tools")
        self.assertTrue(connection.sent[0]["id"])
        self.assertTrue(connection.closed)

    async def test_call_tool_sends_arguments_and_returns_content(self):
        connection = FakeConnection(
            [{"type": "tool_result", "content": "预约成功", "isError": False}]
        )
        client = WebSocketToolClient(
            "ws://127.0.0.1:8104/ws",
            connect=lambda url: connection,
        )

        result = await client.call_tool(
            "classroom_reserve_room",
            {"room_id": "room_1", "user_name": "小明"},
        )

        self.assertEqual(result, "预约成功")
        self.assertEqual(connection.sent[0]["type"], "call_tool")
        self.assertEqual(connection.sent[0]["name"], "classroom_reserve_room")
        self.assertEqual(
            connection.sent[0]["arguments"],
            {"room_id": "room_1", "user_name": "小明"},
        )

    async def test_tool_error_is_returned_for_model_recovery(self):
        connection = FakeConnection(
            [{"type": "tool_result", "content": "CONFLICT: 教室已占用", "isError": True}]
        )
        client = WebSocketToolClient(
            "ws://127.0.0.1:8104/ws",
            connect=lambda url: connection,
        )

        result = await client.call_tool("classroom_reserve_room", {})

        self.assertEqual(result, "工具执行失败：\nCONFLICT: 教室已占用")

    async def test_mismatched_response_id_is_rejected(self):
        connection = FakeConnection(
            [{"id": "wrong", "type": "tools", "tools": []}]
        )
        client = WebSocketToolClient(
            "ws://127.0.0.1:8104/ws",
            connect=lambda url: connection,
        )

        with self.assertRaisesRegex(WebSocketProtocolError, "关联信息"):
            await client.list_tools()

    async def test_malformed_messages_are_rejected(self):
        responses = [
            "not-json",
            {"type": "tools", "tools": [{"name": "missing_fields"}]},
            {"type": "tool_result", "content": 1, "isError": False},
        ]
        for index, response in enumerate(responses):
            with self.subTest(response=response):
                connection = FakeConnection([response])
                client = WebSocketToolClient(
                    "ws://127.0.0.1:8104/ws",
                    connect=lambda url: connection,
                )
                with self.assertRaises(WebSocketProtocolError):
                    if index < 2:
                        await client.list_tools()
                    else:
                        await client.call_tool("classroom_list_rooms", {})

    async def test_connection_and_timeout_errors_have_distinct_boundaries(self):
        cases = [
            (OSError("连接细节"), WebSocketRequestError),
            (TimeoutError("读取细节"), TimeoutError),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                connection = FakeConnection([error])
                client = WebSocketToolClient(
                    "ws://127.0.0.1:8104/ws",
                    connect=lambda url: connection,
                )
                with self.assertRaises(expected) as caught:
                    await client.list_tools()
                self.assertIs(caught.exception.__cause__, error)
                self.assertNotIn("细节", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
