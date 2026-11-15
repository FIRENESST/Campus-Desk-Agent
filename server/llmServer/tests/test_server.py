from __future__ import annotations

import asyncio
import gzip
import json
import os
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx
from starlette.requests import ClientDisconnect

from server import ProxyStreamingResponse, Settings, create_app


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk

    async def aclose(self):
        self.closed = True


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests: list[httpx.Request] = []
        self.response_status = 200
        self.response_headers = {"content-type": "application/json", "x-request-id": "test-id"}
        self.response_chunks = [b'{"choices":[{"message":{"content":"hello"}}]}']
        self.upstream_error: type[httpx.RequestError] | None = None
        self.stream: TrackingStream | None = None

        async def upstream_handler(request: httpx.Request):
            self.requests.append(request)
            if self.upstream_error:
                raise self.upstream_error("private upstream diagnostic", request=request)
            self.stream = TrackingStream(self.response_chunks)
            return httpx.Response(
                self.response_status,
                headers=self.response_headers,
                stream=self.stream,
            )

        self.config = Settings(
            api_key="upstream-secret",
            base_url="https://upstream.example/api/v1",
            default_model="default-model",
        )
        self.app = create_app(self.config, transport=httpx.MockTransport(upstream_handler))
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://local.test"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)

    async def test_chat_authentication_default_model_and_parameters(self):
        payload = {
            "messages": [{"role": "user", "content": "你好"}],
            "tools": [{"type": "function", "function": {"name": "weather"}}],
            "temperature": 0.3,
            "provider_option": {"enabled": True},
        }
        response = await self.client.post(
            "/v1/chat/completions?tag=a&tag=b",
            json=payload,
            headers={
                "authorization": "Bearer local-placeholder",
                "cookie": "local-session=private",
                "x-api-key": "other-private-key",
                "openai-beta": "feature=v1",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "hello")
        forwarded = self.requests[0]
        self.assertEqual(str(forwarded.url), "https://upstream.example/api/v1/chat/completions?tag=a&tag=b")
        self.assertEqual(forwarded.headers["authorization"], "Bearer upstream-secret")
        self.assertNotIn("cookie", forwarded.headers)
        self.assertNotIn("x-api-key", forwarded.headers)
        self.assertEqual(forwarded.headers["openai-beta"], "feature=v1")
        self.assertEqual(json.loads(forwarded.content), payload | {"model": "default-model"})
        self.assertEqual(response.headers["x-request-id"], "test-id")
        self.assertTrue(self.stream.closed)

    async def test_explicit_model_wins_and_other_post_routes_work(self):
        for path in ("/v1/chat/completions", "/v1/completions", "/v1/embeddings", "/v1/responses"):
            with self.subTest(path=path):
                response = await self.client.post(path, json={"model": "chosen-model", "input": "test"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(json.loads(self.requests[-1].content)["model"], "chosen-model")
                self.assertEqual(self.requests[-1].url.path, "/api" + path)

    async def test_upstream_cookies_are_not_reused_by_later_requests(self):
        self.response_headers["set-cookie"] = "upstream-session=previous; Path=/"
        for text in ("first", "second"):
            response = await self.client.post(
                "/v1/chat/completions", json={"messages": [{"role": "user", "content": text}]},
            )
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("set-cookie", response.headers)
        self.assertTrue(all("cookie" not in request.headers for request in self.requests))

    async def test_models_and_base_url_without_version_prefix(self):
        self.app.state.settings = replace(self.config, base_url="https://upstream.example")
        self.response_chunks = [b'{"object":"list","data":[{"id":"model-a"}]}']
        response = await self.client.get("/v1/models")
        self.assertEqual(response.json()["data"][0]["id"], "model-a")
        self.assertEqual(str(self.requests[0].url), "https://upstream.example/models")

    async def test_local_authentication_and_health(self):
        self.app.state.settings = replace(self.config, local_api_key="local-secret")
        for headers in ({}, {"authorization": "Bearer wrong"}, {"authorization": "Basic local-secret"}):
            response = await self.client.post("/v1/chat/completions", json={}, headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assertEqual(self.requests, [])
        health = await self.client.get("/health")
        self.assertEqual(health.json(), {"status": "ok"})
        self.assertNotIn("secret", health.text)
        response = await self.client.post(
            "/v1/chat/completions", json={}, headers={"authorization": "bearer local-secret"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.requests[0].headers["authorization"], "Bearer upstream-secret")

    async def test_invalid_json_and_missing_model_do_not_call_upstream(self):
        for body in (b"not-json", b"[]", b"null", b'\xff', b'{"temperature":NaN}'):
            with self.subTest(body=body):
                response = await self.client.post("/v1/chat/completions", content=body)
                self.assertEqual(response.status_code, 400)
        self.app.state.settings = replace(self.config, default_model="")
        response = await self.client.post("/v1/chat/completions", json={"messages": []})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "missing_model")
        self.assertEqual(self.requests, [])

    async def test_sse_body_is_preserved_and_stream_closed(self):
        self.response_headers = {"content-type": "text/event-stream", "connection": "keep-alive"}
        self.response_chunks = [b'data: {"delta":"hello"}\n\n', b"data: [DONE]\n\n"]
        response = await self.client.post("/v1/chat/completions", json={"stream": True})
        self.assertEqual(response.content, b"".join(self.response_chunks))
        self.assertEqual(response.headers["content-type"], "text/event-stream")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertNotIn("connection", response.headers)
        self.assertTrue(self.stream.closed)
        self.assertTrue(json.loads(self.requests[0].content)["stream"])

    async def test_upstream_error_and_response_headers_are_preserved(self):
        self.response_status = 429
        self.response_headers.update({
            "retry-after": "30",
            "connection": "keep-alive, x-private",
            "x-private": "hop-value",
            "set-cookie": "upstream-session=secret",
        })
        self.response_chunks = [b'{"error":{"message":"rate limited"}}']
        response = await self.client.post("/v1/chat/completions", json={})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.content, self.response_chunks[0])
        self.assertEqual(response.headers["retry-after"], "30")
        self.assertNotIn("x-private", response.headers)
        self.assertNotIn("set-cookie", response.headers)
        self.assertTrue(self.stream.closed)

    async def test_gzip_response_is_forwarded_without_double_decoding(self):
        original = b'{"message":"compressed"}'
        self.response_chunks = [gzip.compress(original)]
        self.response_headers["content-encoding"] = "gzip"
        response = await self.client.post("/v1/chat/completions", json={})
        self.assertEqual(response.content, original)
        self.assertEqual(response.headers["content-encoding"], "gzip")

    async def test_network_error_and_timeout_are_sanitized(self):
        for exception, status in ((httpx.ConnectError, 502), (httpx.ReadTimeout, 504)):
            with self.subTest(exception=exception):
                self.upstream_error = exception
                response = await self.client.post("/v1/chat/completions", json={})
                self.assertEqual(response.status_code, status)
                self.assertNotIn("private upstream diagnostic", response.text)
                self.assertNotIn("upstream-secret", response.text)


class StreamLifecycleTests(unittest.IsolatedAsyncioTestCase):
    scope = {"type": "http", "method": "POST", "asgi": {"spec_version": "2.4"}}

    async def receive(self):
        return {"type": "http.disconnect"}

    async def test_first_chunk_is_sent_before_second_chunk_is_available(self):
        next_chunk_ready = asyncio.Event()

        class GatedStream(TrackingStream):
            async def __aiter__(self):
                yield b"first\n"
                await next_chunk_ready.wait()
                yield b"second\n"

        stream = GatedStream([])
        response = ProxyStreamingResponse(httpx.Response(200, stream=stream))
        chunks = []

        async def send(message):
            if message["type"] == "http.response.body" and message["body"]:
                chunks.append(message["body"])
                if message["body"] == b"first\n":
                    next_chunk_ready.set()

        await asyncio.wait_for(response(self.scope, self.receive, send), timeout=2)
        self.assertEqual(chunks, [b"first\n", b"second\n"])
        self.assertTrue(stream.closed)

    async def test_client_disconnect_closes_upstream(self):
        stream = TrackingStream([b"first", b"second"])
        response = ProxyStreamingResponse(httpx.Response(200, stream=stream))

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        with self.assertRaises((ClientDisconnect, OSError)):
            await response(self.scope, self.receive, send)
        self.assertTrue(stream.closed)

    async def test_midstream_upstream_failure_closes_connection(self):
        class FailingStream(TrackingStream):
            async def __aiter__(self):
                yield b"first"
                raise httpx.ReadTimeout("upstream stopped")

        stream = FailingStream([])
        response = ProxyStreamingResponse(httpx.Response(200, stream=stream))

        async def send(message):
            pass

        with self.assertRaises(httpx.ReadTimeout):
            await response(self.scope, self.receive, send)
        self.assertTrue(stream.closed)


class SettingsTests(unittest.TestCase):
    def load(self, **overrides):
        values = {"LLM_API_KEY": "test-secret", "LLM_BASE_URL": "https://example.test/v1/"}
        values.update(overrides)
        with patch("server.load_dotenv"), patch.dict(os.environ, values, clear=True):
            return Settings.from_env()

    def test_defaults_and_secret_repr(self):
        settings = self.load()
        self.assertEqual(settings.port, 8000)
        self.assertEqual(settings.base_url, "https://example.test/v1")
        self.assertNotIn("test-secret", repr(settings))

    def test_invalid_configuration_fails_before_starting(self):
        for values in (
            {"LLM_API_KEY": ""},
            {"LLM_API_KEY": "not a key"},
            {"LLM_BASE_URL": ""},
            {"LLM_BASE_URL": "file:///tmp/model"},
            {"LLM_BASE_URL": "https://user:password@example.test"},
            {"LLM_BASE_URL": "https://example.test/v1?token=private"},
            {"LOCAL_PORT": "0"},
            {"LOCAL_PORT": "65536"},
            {"LOCAL_PORT": "abc"},
            {"LLM_TIMEOUT": "nan"},
            {"LLM_TIMEOUT": "-1"},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.load(**values)


if __name__ == "__main__":
    unittest.main()
