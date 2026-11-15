import unittest

from main_agent.composite import CompositeToolClient, DuplicateToolError
from main_agent.core import ToolSpec, UnavailableToolError


class FakeToolClient:
    def __init__(self, specs, results=None):
        self._specs = list(specs)
        self._results = dict(results or {})
        self.list_calls = 0
        self.calls = []

    async def list_tools(self):
        self.list_calls += 1
        return list(self._specs)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self._results[name]


def spec(name):
    return ToolSpec(name=name, description=name, parameters={"type": "object"})


class CompositeToolClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_tools_merges_clients_in_order(self):
        clients = [
            FakeToolClient([spec("gym_list_venues")]),
            FakeToolClient([spec("club_list_activities"), spec("library_list_books")]),
        ]
        client = CompositeToolClient(clients)

        tools = await client.list_tools()

        self.assertEqual(
            [tool.name for tool in tools],
            ["gym_list_venues", "club_list_activities", "library_list_books"],
        )
        self.assertEqual([item.list_calls for item in clients], [1, 1])

    async def test_call_tool_routes_to_the_client_that_advertised_the_name(self):
        gym = FakeToolClient(
            [spec("gym_list_venues")],
            {"gym_list_venues": "场地"},
        )
        club = FakeToolClient(
            [spec("club_list_activities")],
            {"club_list_activities": "活动"},
        )
        client = CompositeToolClient([gym, club])
        await client.list_tools()

        result = await client.call_tool("club_list_activities", {})

        self.assertEqual(result, "活动")
        self.assertEqual(gym.calls, [])
        self.assertEqual(club.calls, [("club_list_activities", {})])

    async def test_duplicate_names_are_rejected_across_the_full_list(self):
        cases = [
            [FakeToolClient([spec("same")]), FakeToolClient([spec("same")])],
            [FakeToolClient([spec("same"), spec("same")])],
        ]
        for clients in cases:
            with self.subTest(client_count=len(clients)):
                client = CompositeToolClient(clients)
                with self.assertRaisesRegex(DuplicateToolError, "same"):
                    await client.list_tools()

    async def test_unknown_tool_cannot_be_called_without_a_route(self):
        client = CompositeToolClient([FakeToolClient([spec("known")])])
        await client.list_tools()

        with self.assertRaisesRegex(UnavailableToolError, "unknown"):
            await client.call_tool("unknown", {})

    async def test_list_failure_does_not_replace_the_last_complete_routes(self):
        class FailSecondListClient(FakeToolClient):
            async def list_tools(self):
                if self.list_calls > 0:
                    raise RuntimeError("服务不可用")
                return await super().list_tools()

        working = FailSecondListClient([spec("known")], {"known": "结果"})
        client = CompositeToolClient([working])
        await client.list_tools()

        with self.assertRaisesRegex(RuntimeError, "服务不可用"):
            await client.list_tools()

        self.assertEqual(await client.call_tool("known", {}), "结果")


if __name__ == "__main__":
    unittest.main()
