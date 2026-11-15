import unittest

import httpx2

from main import Settings, create_app


class ToolStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_lists_real_tools_without_granting_mcp_access(self):
        settings = Settings(token="m" * 43, runtime_status_key="s" * 43)
        app = create_app(settings)
        async with app.router.lifespan_context(app), httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, client=("127.0.0.1", 10001)), base_url="http://127.0.0.1:8001",
        ) as client:
            self.assertEqual((await client.get("/internal/status/tools")).status_code, 404)
            headers = {"X-Campus-Runtime-Key": settings.runtime_status_key}
            result = await client.get("/internal/status/tools", headers=headers)
            self.assertEqual(result.status_code, 200, result.text)
            tools = {item["name"]: item for item in result.json()["items"]}
            self.assertIs(tools["search_rag"]["read_only"], True)
            self.assertIn("get_subagent_capabilities", tools)
            self.assertEqual((await client.post("/mcp", json={}, headers=headers)).status_code, 401)
