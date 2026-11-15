import asyncio
import unittest

from main_agent import core
from main_agent.core import AgentCore, Message, ToolCall, ToolSpec


class FakeLLM:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append((list(messages), list(tools)))
        if not self._replies:
            raise AssertionError("模型被调用了意料之外的次数")
        return self._replies.pop(0)


class FakeTools:
    def __init__(self, specs=None, results=None):
        self._specs = list(specs or [])
        self._results = dict(results or {})
        self.calls = []

    async def list_tools(self):
        return list(self._specs)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self._results[name]


class AgentCoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_answer_returns_new_history_without_mutating_input(self):
        messages = [
            Message(role="system", content="保持简洁"),
            Message(role="user", content="你好"),
        ]
        original = list(messages)
        llm = FakeLLM([Message(role="assistant", content="你好，有什么可以帮你？")])
        agent = AgentCore(llm=llm, tools=FakeTools())

        result = await agent.run(messages)

        self.assertIsNot(result, messages)
        self.assertEqual(messages, original)
        self.assertEqual(
            [message.role for message in result],
            ["system", "user", "assistant"],
        )
        self.assertEqual(result[-1].content, "你好，有什么可以帮你？")

    async def test_tool_calls_execute_in_order_and_results_are_sent_back_to_llm(self):
        specs = [
            ToolSpec("get_time", "获取时间", {"type": "object"}),
            ToolSpec("get_weather", "获取天气", {"type": "object"}),
        ]
        tools = FakeTools(
            specs=specs,
            results={"get_time": "10:00", "get_weather": "晴"},
        )
        llm = FakeLLM(
            [
                Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall("call_time", "get_time", {}),
                        ToolCall("call_weather", "get_weather", {"city": "上海"}),
                    ],
                ),
                Message(role="assistant", content="现在 10:00，上海天气晴。"),
            ]
        )
        agent = AgentCore(llm=llm, tools=tools)

        result = await agent.run([Message(role="user", content="时间和天气？")])

        self.assertEqual(
            tools.calls,
            [("get_time", {}), ("get_weather", {"city": "上海"})],
        )
        self.assertEqual(
            [message.role for message in result],
            ["user", "assistant", "tool", "tool", "assistant"],
        )
        self.assertEqual(
            [message.tool_call_id for message in result[2:4]],
            ["call_time", "call_weather"],
        )
        self.assertEqual(result[-1].content, "现在 10:00，上海天气晴。")
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(llm.calls[0][1], specs)
        self.assertEqual(llm.calls[1][1], specs)
        self.assertEqual(
            [message.content for message in llm.calls[1][0][-2:]],
            ["10:00", "晴"],
        )

    async def test_tool_round_limit_stops_repeated_tool_requests(self):
        repeated_call = Message(
            role="assistant",
            tool_calls=[ToolCall("call_time", "get_time", {})],
        )
        tools = FakeTools(
            specs=[ToolSpec("get_time", "获取时间", {"type": "object"})],
            results={"get_time": "10:00"},
        )
        llm = FakeLLM([repeated_call, repeated_call])
        agent = AgentCore(llm=llm, tools=tools, max_tool_rounds=1)

        with self.assertRaises(RuntimeError) as context:
            await agent.run([Message(role="user", content="现在几点？")])

        self.assertIsInstance(context.exception, core.ToolRoundLimitExceeded)
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(tools.calls, [("get_time", {})])

    async def test_llm_timeout_has_a_stable_agent_error(self):
        class SlowLLM(FakeLLM):
            async def complete(self, messages, tools):
                await asyncio.sleep(1)
                return Message(role="assistant", content="太晚的回答")

        agent = AgentCore(
            llm=SlowLLM([]),
            tools=FakeTools(),
            llm_timeout=0.01,
        )

        with self.assertRaises(RuntimeError) as context:
            await agent.run([Message(role="user", content="你好")])

        self.assertIsInstance(context.exception, core.AgentTimeoutError)
        self.assertIn("LLM", str(context.exception))

    async def test_tool_timeout_has_a_stable_agent_error(self):
        class SlowTools(FakeTools):
            async def call_tool(self, name, arguments):
                await asyncio.sleep(1)
                return "太晚的结果"

        tools = SlowTools(
            specs=[ToolSpec("get_time", "获取时间", {"type": "object"})]
        )
        llm = FakeLLM(
            [
                Message(
                    role="assistant",
                    tool_calls=[ToolCall("call_time", "get_time", {})],
                )
            ]
        )
        agent = AgentCore(llm=llm, tools=tools, tool_timeout=0.01)

        with self.assertRaises(RuntimeError) as context:
            await agent.run([Message(role="user", content="现在几点？")])

        self.assertIsInstance(context.exception, core.AgentTimeoutError)
        self.assertIn("工具", str(context.exception))

    async def test_unavailable_tool_is_rejected_before_execution(self):
        tools = FakeTools(
            specs=[ToolSpec("get_time", "获取时间", {"type": "object"})],
            results={"get_time": "10:00"},
        )
        llm = FakeLLM(
            [
                Message(
                    role="assistant",
                    tool_calls=[ToolCall("call_bad", "delete_everything", {})],
                )
            ]
        )
        agent = AgentCore(llm=llm, tools=tools)

        with self.assertRaises(core.AgentError) as context:
            await agent.run([Message(role="user", content="删除所有数据")])

        self.assertIsInstance(context.exception, core.UnavailableToolError)
        self.assertEqual(tools.calls, [])

    async def test_unknown_tool_in_a_batch_prevents_all_tool_execution(self):
        tools = FakeTools(
            specs=[ToolSpec("get_time", "获取时间", {"type": "object"})],
            results={"get_time": "10:00"},
        )
        llm = FakeLLM(
            [
                Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall("call_time", "get_time", {}),
                        ToolCall("call_bad", "delete_everything", {}),
                    ],
                )
            ]
        )
        agent = AgentCore(llm=llm, tools=tools)

        with self.assertRaises(core.UnavailableToolError):
            await agent.run([Message(role="user", content="执行两个工具")])

        self.assertEqual(tools.calls, [])

    async def test_constructor_rejects_invalid_limits_and_timeouts(self):
        for value in (-1, 1.5, True):
            with self.subTest(max_tool_rounds=value):
                with self.assertRaises(ValueError):
                    AgentCore(
                        llm=FakeLLM([]),
                        tools=FakeTools(),
                        max_tool_rounds=value,
                    )

        for name in ("llm_timeout", "tool_timeout"):
            for value in (0, -1, float("inf"), float("nan"), "1", True):
                with self.subTest(timeout=name, value=value):
                    with self.assertRaises(ValueError):
                        AgentCore(
                            llm=FakeLLM([]),
                            tools=FakeTools(),
                            **{name: value},
                        )


if __name__ == "__main__":
    unittest.main()
