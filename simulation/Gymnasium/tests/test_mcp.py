import json
import unittest

from fastapi.testclient import TestClient
from mcp.types import CallToolResult

from app import create_app
from mcp_service import create_mcp_server
from store import GymStore
from fixtures import gym_store


class MCPToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = gym_store()
        self.addCleanup(self.store.close)
        self.mcp = create_mcp_server(self.store)

    async def call(self, name, arguments=None):
        result = await self.mcp.call_tool(name, arguments or {})
        self.assertIsInstance(result, CallToolResult)
        self.assertEqual(
            result.structured_content, json.loads(result.content[0].text)
        )
        return result

    async def test_exact_tool_names_and_argument_schemas(self):
        tools = await self.mcp.list_tools()
        by_name = {tool.name: tool for tool in tools}

        self.assertEqual(
            {"gym_list_venues", "gym_reserve_venue", "gym_cancel_reservation"},
            set(by_name),
        )
        self.assertEqual({}, by_name["gym_list_venues"].input_schema["properties"])
        reserve_schema = by_name["gym_reserve_venue"].input_schema
        self.assertEqual({"venue_id", "user_name", "slot_id"}, set(reserve_schema["properties"]))
        self.assertEqual({"venue_id", "user_name"}, set(reserve_schema["required"]))
        cancel_schema = by_name["gym_cancel_reservation"].input_schema
        self.assertEqual({"reservation_id"}, set(cancel_schema["properties"]))
        self.assertEqual({"reservation_id"}, set(cancel_schema["required"]))

    async def test_list_reserve_and_cancel_share_store(self):
        listed = await self.call("gym_list_venues")
        self.assertFalse(listed.is_error)
        self.assertEqual(7, len(listed.structured_content["venues"]))

        reserved = await self.call(
            "gym_reserve_venue", {"venue_id": "strength", "user_name": "张敏"}
        )
        self.assertFalse(reserved.is_error)
        reservation = reserved.structured_content
        self.assertEqual("张敏", reservation["user_name"])
        self.assertEqual([reservation], self.store.list_reservations())

        cancelled = await self.call(
            "gym_cancel_reservation",
            {"reservation_id": reservation["reservation_id"]},
        )
        self.assertFalse(cancelled.is_error)
        self.assertTrue(cancelled.structured_content["cancelled"])
        self.assertEqual(reservation, cancelled.structured_content["reservation"])
        self.assertEqual([], self.store.list_reservations())

    async def test_business_errors_are_is_error_results_with_stable_payload(self):
        arguments = {"venue_id": "strength", "user_name": "用户甲"}
        await self.call("gym_reserve_venue", arguments)

        duplicate = await self.call("gym_reserve_venue", arguments)
        self.assertTrue(duplicate.is_error)
        self.assertEqual("CONFLICT", duplicate.structured_content["code"])
        self.assertFalse(duplicate.structured_content["retryable"])

        await self.call(
            "gym_reserve_venue", {"venue_id": "strength", "user_name": "用户乙"}
        )
        full = await self.call(
            "gym_reserve_venue", {"venue_id": "strength", "user_name": "用户丙"}
        )
        self.assertTrue(full.is_error)
        self.assertEqual("CAPACITY_FULL", full.structured_content["code"])
        self.assertTrue(full.structured_content["retryable"])

        unknown = await self.call(
            "gym_reserve_venue", {"venue_id": "missing", "user_name": "张敏"}
        )
        self.assertTrue(unknown.is_error)
        self.assertEqual("NOT_FOUND", unknown.structured_content["code"])

        invalid = await self.call(
            "gym_reserve_venue", {"venue_id": "strength", "user_name": " "}
        )
        self.assertTrue(invalid.is_error)
        self.assertEqual("INVALID_ARGUMENT", invalid.structured_content["code"])

        unknown_cancel = await self.call(
            "gym_cancel_reservation", {"reservation_id": "missing"}
        )
        self.assertTrue(unknown_cancel.is_error)
        self.assertEqual("NOT_FOUND", unknown_cancel.structured_content["code"])
        self.assertEqual(2, len(self.store.list_reservations()))


class MCPTransportTests(unittest.TestCase):
    def test_fastapi_exposes_streamable_http_at_exact_mcp_path(self):
        with TestClient(create_app(gym_store()), base_url="http://127.0.0.1:8101") as client:
            response = client.post(
                "/mcp",
                headers={
                    "accept": "application/json, text/event-stream",
                    "content-type": "application/json",
                    "mcp-protocol-version": "2025-03-26",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "unittest", "version": "1.0"},
                    },
                },
            )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, response.json()["id"])
        self.assertEqual("gymnasium", response.json()["result"]["serverInfo"]["name"])


if __name__ == "__main__":
    unittest.main()
