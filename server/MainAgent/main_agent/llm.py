import json
from typing import Any

from openai import APIError, APITimeoutError, AsyncOpenAI

from .core import AgentError, Message, ToolCall, ToolSpec


class LLMError(AgentError):
    pass


class InvalidLLMResponse(LLMError):
    pass


class OpenAICompatibleLLMClient:
    def __init__(self, client: AsyncOpenAI, model: str, *, json_completion_options: dict[str, Any] | None = None):
        self._client = client
        self._model = model
        self._json_completion_options = json_completion_options

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> Message:
        return await self._complete(messages, tools)

    async def complete_json(self, messages: list[Message]) -> Message:
        return await self._complete(messages, [], self._json_completion_options)

    async def _complete(self, messages, tools, options=None) -> Message:
        request: dict[str, Any] = {
            "model": self._model,
            "messages": [self._serialize_message(message) for message in messages],
            "stream": False,
        }
        if options:
            request.update(options)
        if tools:
            request["tools"] = [self._serialize_tool(tool) for tool in tools]
            request["tool_choice"] = "auto"

        try:
            response = await self._client.chat.completions.create(**request)
        except APITimeoutError as exc:
            raise TimeoutError("LLM 请求超时") from exc
        except APIError as exc:
            raise LLMError("LLM 请求失败") from exc

        if not response.choices:
            raise InvalidLLMResponse("LLM 响应不包含候选消息")

        assistant_message = response.choices[0].message
        tool_calls = []
        for call in assistant_message.tool_calls or []:
            if call.type != "function":
                raise InvalidLLMResponse(f"不支持的工具调用类型：{call.type}")
            try:
                arguments = json.loads(call.function.arguments)
            except (json.JSONDecodeError, TypeError) as exc:
                raise InvalidLLMResponse("工具调用参数不是合法 JSON") from exc
            if not isinstance(arguments, dict):
                raise InvalidLLMResponse("工具调用参数必须是 JSON 对象")
            tool_calls.append(
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=arguments,
                )
            )

        return Message(
            role="assistant",
            content=assistant_message.content or "",
            tool_calls=tool_calls,
        )

    @staticmethod
    def _serialize_message(message: Message) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "role": message.role,
            "content": message.content,
        }
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
                for call in message.tool_calls
            ]
        if message.tool_call_id is not None:
            payload["tool_call_id"] = message.tool_call_id
        return payload

    @staticmethod
    def _serialize_tool(tool: ToolSpec) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
