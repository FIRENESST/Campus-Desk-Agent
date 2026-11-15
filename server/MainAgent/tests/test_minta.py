import unittest

import httpx2

from main_agent.minta import (
    MintaGatewayClient,
    MintaProtocolError,
    MintaRequestError,
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


class MintaGatewayClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_follows_openapi_contract_and_preserves_header_pairs(self):
        http = FakeHTTPClient(
            [
                FakeResponse(
                    {
                        "status": 200,
                        "headers": [
                            ["content-type", "application/json"],
                            ["x-dup", "a"],
                            ["x-dup", "b"],
                        ],
                        "trailers": [["grpc-status", "0"]],
                        "body": '{"ok":true}',
                    }
                )
            ]
        )
        client = MintaGatewayClient("http://127.0.0.1:8080/", client=http)

        reply = await client.call(
            "POST",
            "/echo",
            headers=[("X-Request-Id", "req-1"), ("X-Dup", "a"), ("X-Dup", "b")],
            json={"hello": "世界"},
            timeout_ms=5000,
            upstream="default",
        )

        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, b'{"ok":true}')
        self.assertEqual(reply.json(), {"ok": True})
        self.assertEqual(
            reply.headers,
            [("content-type", "application/json"), ("x-dup", "a"), ("x-dup", "b")],
        )
        self.assertEqual(reply.trailers, [("grpc-status", "0")])
        self.assertEqual(
            http.requests,
            [
                (
                    "http://127.0.0.1:8080/v1/call",
                    {
                        "json": {
                            "method": "POST",
                            "target": "/echo",
                            "headers": [
                                ["X-Request-Id", "req-1"],
                                ["X-Dup", "a"],
                                ["X-Dup", "b"],
                            ],
                            "json": {"hello": "世界"},
                            "timeout_ms": 5000,
                            "upstream": "default",
                        }
                    },
                )
            ],
        )

    async def test_binary_bodies_are_base64_decoded_in_both_directions(self):
        http = FakeHTTPClient(
            [
                FakeResponse(
                    {
                        "status": 200,
                        "headers": [],
                        "trailers": [],
                        "body_base64": "/+8=",
                    }
                )
            ]
        )
        client = MintaGatewayClient("http://127.0.0.1:8080", client=http)

        reply = await client.call("PUT", "/binary", body_base64="AAEC")

        self.assertEqual(reply.body, b"\xff\xef")
        self.assertEqual(http.requests[0][1]["json"]["body_base64"], "AAEC")

    async def test_json_null_is_distinct_from_an_omitted_body(self):
        http = FakeHTTPClient(
            [FakeResponse({"status": 200, "headers": [], "trailers": [], "body": "null"})]
        )
        client = MintaGatewayClient("http://127.0.0.1:8080", client=http)

        await client.call("POST", "/null", headers={"X-Test": "yes"}, json=None)

        request = http.requests[0][1]["json"]
        self.assertIn("json", request)
        self.assertIsNone(request["json"])
        self.assertEqual(request["headers"], {"X-Test": "yes"})

    async def test_backend_http_error_is_a_completed_application_response(self):
        http = FakeHTTPClient(
            [
                FakeResponse(
                    {
                        "status": 503,
                        "headers": [["content-type", "text/plain"]],
                        "trailers": [],
                        "body": "backend unavailable",
                    }
                )
            ]
        )
        client = MintaGatewayClient("http://127.0.0.1:8080", client=http)

        reply = await client.call("GET", "/unavailable")

        self.assertEqual(reply.status, 503)
        self.assertEqual(reply.text(), "backend unavailable")

    async def test_gateway_errors_are_typed_and_deadline_is_a_timeout(self):
        gateway_error = FakeResponse(
            {"error": {"code": "unavailable", "message": "上游敏感细节"}},
            status_code=503,
        )
        client = MintaGatewayClient(
            "http://127.0.0.1:8080",
            client=FakeHTTPClient([gateway_error]),
        )
        with self.assertRaises(MintaRequestError) as caught:
            await client.call("GET", "/echo")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertNotIn("敏感细节", str(caught.exception))

        deadline = FakeResponse(
            {"error": {"code": "deadline_exceeded", "message": "deadline"}},
            status_code=504,
        )
        client = MintaGatewayClient(
            "http://127.0.0.1:8080",
            client=FakeHTTPClient([deadline]),
        )
        with self.assertRaisesRegex(TimeoutError, "超时"):
            await client.call("GET", "/echo")

    async def test_transport_failures_use_stable_error_boundaries(self):
        cases = [
            (httpx2.ReadTimeout("读取细节"), TimeoutError),
            (httpx2.ConnectError("连接细节"), MintaRequestError),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                client = MintaGatewayClient(
                    "http://127.0.0.1:8080",
                    client=FakeHTTPClient([error]),
                )
                with self.assertRaises(expected) as caught:
                    await client.call("GET", "/echo")
                self.assertIs(caught.exception.__cause__, error)
                self.assertNotIn("细节", str(caught.exception))

    async def test_invalid_calls_are_rejected_before_any_http_request(self):
        cases = [
            {"method": "GET", "target": "https://example.com/echo"},
            {"method": "GET", "target": "//example.com/echo"},
            {"method": "GET", "target": "echo"},
            {"method": "GET", "target": "/echo", "body": "x", "json": {}},
            {"method": "GET", "target": "/echo", "body": "x", "body_base64": "eA=="},
            {"method": "GET", "target": "/echo", "body_base64": "eA==", "json": {}},
            {"method": "GET", "target": "/echo", "body_base64": "not-base64"},
            {"method": "GET", "target": "/echo", "timeout_ms": 0},
            {"method": "GET", "target": "/echo", "timeout_ms": True},
        ]
        for case in cases:
            with self.subTest(case=case):
                http = FakeHTTPClient([])
                client = MintaGatewayClient("http://127.0.0.1:8080", client=http)
                with self.assertRaises(ValueError):
                    await client.call(**case)
                self.assertEqual(http.requests, [])

    async def test_malformed_gateway_replies_are_rejected(self):
        payloads = [
            {"status": 200, "headers": [], "trailers": []},
            {"status": 200, "headers": [], "trailers": [], "body": "x", "body_base64": "eA=="},
            {"status": 200, "headers": [], "trailers": [], "body_base64": "bad"},
            {"status": 200, "headers": [["only-one"]], "trailers": [], "body": "x"},
            {"status": 99, "headers": [], "trailers": [], "body": "x"},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                client = MintaGatewayClient(
                    "http://127.0.0.1:8080",
                    client=FakeHTTPClient([FakeResponse(payload)]),
                )
                with self.assertRaises(MintaProtocolError):
                    await client.call("GET", "/echo")

        client = MintaGatewayClient(
            "http://127.0.0.1:8080",
            client=FakeHTTPClient([FakeResponse({"error": {"code": "x"}}, status_code=400)]),
        )
        with self.assertRaises(MintaProtocolError):
            await client.call("GET", "/echo")


if __name__ == "__main__":
    unittest.main()
