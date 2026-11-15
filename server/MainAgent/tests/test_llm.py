import unittest
from types import SimpleNamespace

import openai

from main_agent import llm as llm_module
from main_agent.core import Message, ToolCall, ToolSpec
from main_agent.llm import OpenAICompatibleLLMClient


class FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeOpenAI:
    def __init__(self, response):
        self.completions = FakeCompletions(response)
        self.chat = SimpleNamespace(completions=self.completions)


def response_with(message):
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class OpenAICompatibleLLMClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_options_apply_only_to_structured_requests(self):
        client = FakeOpenAI(response_with(SimpleNamespace(role="assistant", content="{}", tool_calls=None)))
        llm = OpenAICompatibleLLMClient(client, "fixture-model", json_completion_options={
            "response_format": {"type": "json_object"}, "extra_body": {"thinking": {"type": "disabled"}},
        })
        messages = [Message("user", "核验")]
        await llm.complete_json(messages)
        self.assertEqual(client.completions.kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(client.completions.kwargs["extra_body"], {"thinking": {"type": "disabled"}})
        await llm.complete(messages, [])
        self.assertNotIn("response_format", client.completions.kwargs)
        self.assertNotIn("extra_body", client.completions.kwargs)

    async def test_serializes_messages_and_tools_and_returns_final_answer(self):
        response = response_with(
            SimpleNamespace(
                role="assistant",
                content="已完成",
                tool_calls=None,
            )
        )
        client = FakeOpenAI(response)
        llm = OpenAICompatibleLLMClient(client=client, model="deepseek-flash")
        messages = [
            Message(role="system", content="保持简洁"),
            Message(role="user", content="查询时间"),
            Message(
                role="assistant",
                tool_calls=[ToolCall("call_001", "get_time", {})],
            ),
            Message(
                role="tool",
                content="10:00",
                tool_call_id="call_001",
            ),
        ]
        tools = [
            ToolSpec(
                "get_time",
                "获取当前时间",
                {"type": "object", "properties": {}},
            )
        ]

        result = await llm.complete(messages, tools)

        self.assertEqual(result, Message(role="assistant", content="已完成"))
        request = client.completions.kwargs
        self.assertEqual(request["model"], "deepseek-flash")
        self.assertFalse(request["stream"])
        self.assertEqual(request["tool_choice"], "auto")
        self.assertEqual(
            request["tools"],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "get_time",
                        "description": "获取当前时间",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        )
        self.assertEqual(request["messages"][0], {"role": "system", "content": "保持简洁"})
        self.assertEqual(
            request["messages"][2]["tool_calls"],
            [
                {
                    "id": "call_001",
                    "type": "function",
                    "function": {"name": "get_time", "arguments": "{}"},
                }
            ],
        )
        self.assertEqual(
            request["messages"][3],
            {"role": "tool", "content": "10:00", "tool_call_id": "call_001"},
        )

    async def test_parses_model_tool_calls_and_json_arguments(self):
        response = response_with(
            SimpleNamespace(
                role="assistant",
                content=None,
                tool_calls=[
                    SimpleNamespace(
                        id="call_001",
                        type="function",
                        function=SimpleNamespace(
                            name="get_weather",
                            arguments='{"city":"上海","days":2}',
                        ),
                    )
                ],
            )
        )
        llm = OpenAICompatibleLLMClient(
            client=FakeOpenAI(response),
            model="deepseek-flash",
        )

        result = await llm.complete([Message(role="user", content="天气？")], [])

        self.assertEqual(result.role, "assistant")
        self.assertEqual(result.content, "")
        self.assertEqual(
            result.tool_calls,
            [ToolCall("call_001", "get_weather", {"city": "上海", "days": 2})],
        )

    async def test_malformed_model_responses_are_rejected_consistently(self):
        def tool_call(arguments, call_type="function"):
            return SimpleNamespace(
                id="call_001",
                type=call_type,
                function=SimpleNamespace(name="get_time", arguments=arguments),
            )

        cases = {
            "empty_choices": SimpleNamespace(choices=[]),
            "invalid_json": response_with(
                SimpleNamespace(
                    role="assistant",
                    content=None,
                    tool_calls=[tool_call("{bad")],
                )
            ),
            "non_object_arguments": response_with(
                SimpleNamespace(
                    role="assistant",
                    content=None,
                    tool_calls=[tool_call("[]")],
                )
            ),
            "unsupported_tool_type": response_with(
                SimpleNamespace(
                    role="assistant",
                    content=None,
                    tool_calls=[tool_call("{}", call_type="custom")],
                )
            ),
        }

        for name, response in cases.items():
            with self.subTest(name=name):
                llm = OpenAICompatibleLLMClient(
                    client=FakeOpenAI(response),
                    model="deepseek-flash",
                )
                with self.assertRaises(RuntimeError) as context:
                    await llm.complete([Message(role="user", content="你好")], [])
                self.assertIsInstance(
                    context.exception,
                    llm_module.InvalidLLMResponse,
                )

    async def test_openai_failures_are_mapped_without_leaking_response_details(self):
        cases = [
            (
                "timeout",
                openai.APITimeoutError(request=object()),
                TimeoutError,
            ),
            (
                "api",
                openai.APIError(
                    "上游敏感响应",
                    request=object(),
                    body=None,
                ),
                llm_module.LLMError,
            ),
        ]

        for name, error, expected_type in cases:
            with self.subTest(name=name):
                llm = OpenAICompatibleLLMClient(
                    client=FakeOpenAI(error),
                    model="deepseek-flash",
                )
                with self.assertRaises(expected_type) as context:
                    await llm.complete([Message(role="user", content="你好")], [])
                self.assertNotIn("上游敏感响应", str(context.exception))
                self.assertIs(context.exception.__cause__, error)


if __name__ == "__main__":
    unittest.main()
