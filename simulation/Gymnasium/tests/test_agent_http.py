import asyncio
import json
import unittest

from fastapi.testclient import TestClient

from app import create_app
from store import GymStore
from fixtures import gym_store


class AgentHttpContractTests(unittest.TestCase):
    def setUp(self):
        self.store = gym_store()
        self.addCleanup(self.store.close)
        self.app = create_app(self.store)
        self.client_context = TestClient(self.app, base_url="http://127.0.0.1:8101")
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)

    def call(self, name, arguments=None):
        response = self.client.post(
            "/agent/call", json={"name": name, "arguments": arguments or {}}
        )
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual({"content"}, set(body))
        self.assertEqual(
            json.dumps(json.loads(body["content"]), ensure_ascii=False, separators=(",", ":")),
            body["content"],
        )
        return json.loads(body["content"])

    def assert_agent_error(self, response, status, code, retryable=None):
        self.assertEqual(status, response.status_code, response.text)
        body = response.json()
        self.assertEqual({"error"}, set(body))
        self.assertEqual({"code", "message", "retryable"}, set(body["error"]))
        self.assertEqual(code, body["error"]["code"])
        self.assertTrue(body["error"]["message"])
        self.assertIsInstance(body["error"]["retryable"], bool)
        if retryable is not None:
            self.assertEqual(retryable, body["error"]["retryable"])

    def test_healthz(self):
        response = self.client.get("/healthz")

        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok", "service": "gymnasium"}, response.json())

    def test_tool_list_reuses_exact_mcp_names_descriptions_and_schemas(self):
        response = self.client.get("/agent/tools")

        self.assertEqual(200, response.status_code)
        tools = response.json()["tools"]
        self.assertEqual(
            {"gym_list_venues", "gym_reserve_venue", "gym_cancel_reservation"},
            {tool["name"] for tool in tools},
        )
        expected = {
            tool.name: tool.input_schema
            for tool in asyncio.run(self.app.state.mcp.list_tools())
        }
        for tool in tools:
            self.assertTrue(tool["description"])
            self.assertIsInstance(tool["parameters"], dict)
            self.assertEqual(expected[tool["name"]], tool["parameters"])

    def test_list_reserve_and_cancel_share_state_with_rest_frontend(self):
        listed = self.call("gym_list_venues")
        self.assertEqual(7, len(listed["venues"]))

        reservation = self.call(
            "gym_reserve_venue", {"venue_id": "strength", "user_name": "张敏"}
        )
        self.assertEqual([reservation], self.client.get("/api/reservations").json()["reservations"])
        venues = self.client.get("/api/venues").json()["venues"]
        self.assertEqual(1, next(item for item in venues if item["id"] == "strength")["remaining"])

        cancelled = self.call(
            "gym_cancel_reservation", {"reservation_id": reservation["reservation_id"]}
        )
        self.assertTrue(cancelled["cancelled"])
        self.assertEqual(reservation, cancelled["reservation"])
        self.assertEqual([], self.store.list_reservations())
        venues = self.client.get("/api/venues").json()["venues"]
        self.assertEqual(2, next(item for item in venues if item["id"] == "strength")["remaining"])

    def test_business_errors_and_unknown_tool_use_http_status_mapping(self):
        arguments = {"venue_id": "strength", "user_name": "用户甲"}
        self.call("gym_reserve_venue", arguments)

        duplicate = self.client.post("/agent/call", json={"name": "gym_reserve_venue", "arguments": arguments})
        self.assert_agent_error(duplicate, 409, "CONFLICT", False)

        self.call("gym_reserve_venue", {"venue_id": "strength", "user_name": "用户乙"})
        full = self.client.post(
            "/agent/call",
            json={"name": "gym_reserve_venue", "arguments": {"venue_id": "strength", "user_name": "用户丙"}},
        )
        self.assert_agent_error(full, 409, "CAPACITY_FULL", True)

        unknown_venue = self.client.post(
            "/agent/call",
            json={"name": "gym_reserve_venue", "arguments": {"venue_id": "missing", "user_name": "张敏"}},
        )
        self.assert_agent_error(unknown_venue, 404, "NOT_FOUND")
        unknown_cancel = self.client.post(
            "/agent/call",
            json={"name": "gym_cancel_reservation", "arguments": {"reservation_id": "missing"}},
        )
        self.assert_agent_error(unknown_cancel, 404, "NOT_FOUND")
        invalid = self.client.post(
            "/agent/call",
            json={"name": "gym_reserve_venue", "arguments": {"venue_id": "strength", "user_name": " "}},
        )
        self.assert_agent_error(invalid, 400, "INVALID_ARGUMENT")
        unknown_tool = self.client.post(
            "/agent/call", json={"name": "gym_unknown", "arguments": {}}
        )
        self.assert_agent_error(unknown_tool, 404, "NOT_FOUND")
        self.assertEqual(2, len(self.store.list_reservations()))

    def test_strict_request_and_tool_argument_validation(self):
        invalid_payloads = [
            {},
            {"name": "gym_list_venues"},
            {"arguments": {}},
            {"name": "gym_list_venues", "arguments": {}, "extra": True},
            {"name": 7, "arguments": {}},
            {"name": "gym_list_venues", "arguments": []},
            {"name": "gym_list_venues", "arguments": None},
            {"name": "gym_list_venues", "arguments": {"extra": True}},
            {"name": "gym_reserve_venue", "arguments": {"venue_id": "strength"}},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.post("/agent/call", json=payload)
                self.assert_agent_error(response, 400, "INVALID_ARGUMENT", False)

        malformed = self.client.post(
            "/agent/call", content="not-json", headers={"content-type": "application/json"}
        )
        self.assert_agent_error(malformed, 400, "INVALID_ARGUMENT", False)
        self.assertEqual([], self.store.list_reservations())


    def test_admin_console_serves_management_page(self):
        response = self.client.get("/admin")

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertIn("健身房 · 运维管理台", response.text)
        self.assertIn("agent/call", response.text)


if __name__ == "__main__":
    unittest.main()
