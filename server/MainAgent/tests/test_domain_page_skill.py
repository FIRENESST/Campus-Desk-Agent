import asyncio
import json
import socket
import unittest
from functools import partial
from unittest.mock import AsyncMock, patch

import httpx2
from pydantic import ValidationError

from main_agent.campus_cluster import AgentId, AgentTask, SkillContext, build_campus_cluster
from main_agent.campus_cluster.subskills import domain_page, domain_page_reader


def _dns_record(address):
    if ":" in address:
        return (socket.AF_INET6, socket.SOCK_STREAM, 0, "", (address, 443, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 443))


def _mock_http(handler):
    client_factory = partial(httpx2.AsyncClient, transport=httpx2.MockTransport(handler))
    return patch.object(domain_page_reader.httpx2, "AsyncClient", side_effect=client_factory)


class DomainPageSkillTests(unittest.IsolatedAsyncioTestCase):
    def test_input_requires_a_bare_domain(self):
        for value in ("https://example.org/", "example.org:443", "example.org/path", "127.0.0.1", "localhost"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                domain_page.DomainPageInput(domain=value)

    async def test_all_roles_register_the_read_only_skill(self):
        roles = build_campus_cluster().registry
        for role in AgentId:
            agent = roles.get(role)
            task = AgentTask("chat", "page", agent.profile, "读取域名")
            self.assertIn("read_domain_page", agent.skill_registry.bind(task, read_only=True).names)

    async def test_returns_static_text_and_the_final_public_url(self):
        dns = AsyncMock(side_effect=[[_dns_record("1.1.1.1")], [_dns_record("2606:4700:4700::1111")]])
        requests = []

        async def respond(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx2.Response(302, headers={
                    "Location": "https://www.example.org/welcome#top", "Set-Cookie": "temporary=1",
                })
            html = (
                '<head><meta charset="gb2312"><title>校园 &amp; 服务</title></head>'
                '<h1>图书馆</h1><p>开放 <b>时间</b> 08:00</p>'
                '<script>hiddenScript()</script><style>hiddenStyle</style>'
                '<template><p>隐藏模板</p></template>'
            )
            return httpx2.Response(200, headers={"Content-Type": "text/html"},
                                   stream=httpx2.ByteStream(html.encode("gb18030")))

        role = build_campus_cluster().registry.get(AgentId.INFORMATION_SEARCH)
        task = AgentTask("chat", "page", role.profile, "读取域名")
        client = role.skill_registry.bind(task)
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", dns), _mock_http(respond):
            result = await client.call_tool("read_domain_page", {"domain": "ExAmPlE.org."})

        self.assertEqual(result.status, "success")
        self.assertEqual(json.loads(result.content), {
            "domain": "example.org", "source_url": "https://www.example.org/welcome",
            "title": "校园 & 服务", "text": "图书馆\n开放 时间 08:00", "truncated": False,
        })
        self.assertEqual([call.args[0] for call in dns.await_args_list], ["example.org.", "www.example.org."])
        self.assertEqual([request.url.host for request in requests], ["1.1.1.1", "2606:4700:4700::1111"])
        self.assertEqual([request.headers["host"] for request in requests], ["example.org", "www.example.org"])
        self.assertEqual([request.extensions["sni_hostname"] for request in requests],
                         ["example.org", "www.example.org"])
        self.assertNotIn("cookie", requests[1].headers)

    async def test_rejects_a_redirect_with_a_private_dns_answer(self):
        dns = AsyncMock(side_effect=[
            [_dns_record("1.1.1.1")], [_dns_record("8.8.8.8"), _dns_record("::ffff:127.0.0.1")],
        ])
        respond = AsyncMock(return_value=httpx2.Response(
            302, headers={"Location": "https://private.example.org/"},
        ))
        context = SkillContext(AgentId.RUMOR_ASSESSMENT, "chat", "page")
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", dns), _mock_http(respond):
            result = await domain_page.read_domain_page(context, domain_page.DomainPageInput(domain="example.org"))

        self.assertEqual(result.status, "error")
        self.assertEqual(json.loads(result.content)["error"]["code"], "non_public_address")
        respond.assert_awaited_once()

    async def test_caps_response_bytes_and_extracted_text(self):
        dns = AsyncMock(return_value=[_dns_record("1.1.1.1")])

        async def respond(request):
            return httpx2.Response(200, headers={"Content-Type": "text/plain"},
                                   stream=httpx2.ByteStream("校园服务".encode("utf-8-sig")))

        context = SkillContext(AgentId.CAMPUS_SERVICES, "chat", "page")
        args = domain_page.DomainPageInput(domain="example.org")
        for byte_limit, char_limit in ((10, 10), (30, 2)):
            with (
                self.subTest(byte_limit=byte_limit, char_limit=char_limit),
                patch.object(asyncio.get_running_loop(), "getaddrinfo", dns),
                _mock_http(respond),
                patch.object(domain_page_reader, "_MAX_BYTES", byte_limit),
                patch.object(domain_page_reader, "_MAX_TEXT_CHARS", char_limit),
            ):
                result = await domain_page.read_domain_page(context, args)
                self.assertEqual(result.status, "success")
                payload = json.loads(result.content)
                self.assertEqual(payload["text"], "校园")
                self.assertTrue(payload["truncated"])
