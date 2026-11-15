import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


class LLMClient(Protocol):
    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> Message:
        ...


class ToolClient(Protocol):
    async def list_tools(self) -> list[ToolSpec]:
        ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        ...


class AgentError(RuntimeError):
    pass


class ToolRoundLimitExceeded(AgentError):
    pass


class AgentTimeoutError(AgentError):
    pass


class UnavailableToolError(AgentError):
    pass


class AgentCore:
    def __init__(
        self,
        llm: LLMClient,
        tools: ToolClient,
        max_tool_rounds: int = 5,
        llm_timeout: float = 60.0,
        tool_timeout: float = 30.0,
        *,
        before_tool_batch: Callable[[list[ToolCall]], Awaitable[None]] | None = None,
    ):
        if (
            isinstance(max_tool_rounds, bool)
            or not isinstance(max_tool_rounds, int)
            or max_tool_rounds < 0
        ):
            raise ValueError("max_tool_rounds 必须是大于等于 0 的整数")
        for name, value in (
            ("llm_timeout", llm_timeout),
            ("tool_timeout", tool_timeout),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} 必须是大于 0 的有限秒数")

        self.llm = llm
        self.tools = tools
        self.max_tool_rounds = max_tool_rounds
        self.llm_timeout = llm_timeout
        self.tool_timeout = tool_timeout
        self.before_tool_batch = before_tool_batch

    @staticmethod
    async def _with_timeout(
        awaitable: Awaitable[T],
        seconds: float,
        operation: str,
    ) -> T:
        try:
            async with asyncio.timeout(seconds):
                return await awaitable
        except TimeoutError as exc:
            raise AgentTimeoutError(f"{operation}超时") from exc

    async def run(self, messages: list[Message], *, final_draft_instruction: str | None = None) -> list[Message]:
        history = list(messages)
        available_tools = await self._with_timeout(
            self.tools.list_tools(),
            self.tool_timeout,
            "MCP 工具清单",
        )
        available_tool_names = {tool.name for tool in available_tools}

        for round_index in range(self.max_tool_rounds + 1):
            if final_draft_instruction and available_tools and round_index == self.max_tool_rounds:
                break
            assistant_message = await self._with_timeout(
                self.llm.complete(history, available_tools),
                self.llm_timeout,
                "LLM 请求",
            )
            if not assistant_message.tool_calls:
                return [*history, assistant_message]

            if round_index == self.max_tool_rounds:
                break

            for call in assistant_message.tool_calls:
                if call.name not in available_tool_names:
                    raise UnavailableToolError(f"模型请求了未注册的工具：{call.name}")

            if self.before_tool_batch is not None:
                try:
                    await self._with_timeout(
                        self.before_tool_batch(assistant_message.tool_calls),
                        self.tool_timeout,
                        "工具批次预检",
                    )
                except ToolRoundLimitExceeded:
                    if not final_draft_instruction:
                        raise
                    break

            history.append(assistant_message)
            for call in assistant_message.tool_calls:
                result = await self._with_timeout(
                    self.tools.call_tool(call.name, call.arguments),
                    self.tool_timeout,
                    f"工具 {call.name}",
                )
                history.append(
                    Message(
                        role="tool",
                        content=result,
                        tool_call_id=call.id,
                    )
                )

        if final_draft_instruction and available_tools:
            draft_messages = list(history)
            prompt_index = next((i for i, item in enumerate(history) if item.role not in {"system", "developer"}), len(history))
            draft_messages.insert(prompt_index, Message("system", final_draft_instruction))
            draft = await self._with_timeout(
                self.llm.complete(draft_messages, []),
                self.llm_timeout,
                "生成答案草稿",
            )
            if not draft.tool_calls:
                return [*history, draft]
        raise ToolRoundLimitExceeded("工具调用超过最大轮数")
