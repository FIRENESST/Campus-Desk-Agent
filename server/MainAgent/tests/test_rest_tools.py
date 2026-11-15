import unittest

import httpx2

from main_agent.rest_tools import (
    RestProtocolError,
    RestRequestError,
    RestToolClient,
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

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class RestToolClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_tools_converts_rest_contract_to_tool_specs(self):
        http = FakeHTTPClient(
            [
                FakeResponse(
                    {
                        "tools": [
                            {
                                "name": "library_list_books",
                                "description": "查询图书",
                                "parameters": {"type": "object"},
                            }
                        ]
                    }
                )
            ]
        )
        client = RestToolClient("http://127.0.0.1:8103/", client=http)

        tools = await client.list_tools()

        self.assertEqual(tools[0].name, "library_list_books")
        self.assertEqual(tools[0].description, "查询图书")
        self.assertEqual(tools[0].parameters, {"type": "object"})
        self.assertEqual(
            http.requests,
            [("GET", "http://127.0.0.1:8103/agent/tools", {})],
        )

    async def test_call_tool_posts_arguments_and_returns_content(self):
        http = FakeHTTPClient([FakeResponse({"content": "借阅成功"})])
        client = RestToolClient("http://127.0.0.1:8103", client=http)

        result = await client.call_tool(
            "library_borrow_book",
            {"book_id": "book_1", "borrower_name": "小明"},
        )

        self.assertEqual(result, "借阅成功")
        self.assertEqual(
            http.requests,
            [
                (
                    "POST",
                    "http://127.0.0.1:8103/agent/call",
                    {
                        "json": {
                            "name": "library_borrow_book",
                            "arguments": {"book_id": "book_1", "borrower_name": "小明"},
                        }
                    },
                )
            ],
        )

    async def test_business_error_is_returned_for_model_recovery(self):
        http = FakeHTTPClient(
            [
                FakeResponse(
                    {
                        "error": {
                            "code": "CONFLICT",
                            "message": "已借阅此书",
                            "retryable": False,
                        }
                    },
                    status_code=409,
                )
            ]
        )
        client = RestToolClient("http://127.0.0.1:8103", client=http)

        result = await client.call_tool("library_borrow_book", {})

        self.assertEqual(result, "工具执行失败：\nCONFLICT: 已借阅此书")

    async def test_transport_failures_use_stable_error_boundaries(self):
        cases = [
            (httpx2.ReadTimeout("上游读取细节"), TimeoutError),
            (httpx2.ConnectError("上游连接细节"), RestRequestError),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                client = RestToolClient(
                    "http://127.0.0.1:8103",
                    client=FakeHTTPClient([error]),
                )
                with self.assertRaises(expected) as caught:
                    await client.list_tools()
                self.assertIs(caught.exception.__cause__, error)
                self.assertNotIn("细节", str(caught.exception))

    async def test_malformed_contracts_are_rejected(self):
        payloads = [
            {"tools": [{"name": "missing_fields"}]},
            {"tools": [{"name": "x", "description": "x", "parameters": []}]},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                client = RestToolClient(
                    "http://127.0.0.1:8103",
                    client=FakeHTTPClient([FakeResponse(payload)]),
                )
                with self.assertRaises(RestProtocolError):
                    await client.list_tools()

        client = RestToolClient(
            "http://127.0.0.1:8103",
            client=FakeHTTPClient([FakeResponse({"unexpected": True})]),
        )
        with self.assertRaises(RestProtocolError):
            await client.call_tool("library_list_books", {})

    async def test_server_error_is_not_returned_as_a_tool_result(self):
        client = RestToolClient(
            "http://127.0.0.1:8103",
            client=FakeHTTPClient([FakeResponse({}, status_code=500)]),
        )
        with self.assertRaisesRegex(RestRequestError, "调用请求失败"):
            await client.call_tool("library_list_books", {})


if __name__ == "__main__":
    unittest.main()
