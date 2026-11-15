"""Per-run tool receipts and optional checks at the execution boundary."""

import json
from collections import deque
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import replace
from typing import Any, Protocol

from ..core import ToolCall, ToolSpec, UnavailableToolError
from .models import Evidence, InvalidDraftError, ReliabilityError, ToolResult
from .runtime import RunRuntime


class EvidenceToolClient(Protocol):
    async def list_tools(self) -> list[ToolSpec]:
        ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str | ToolResult:
        ...


ToolGuard = Callable[[str, dict[str, Any]], Awaitable[None]]
ResultInterpreter = Callable[[str, dict[str, Any], str], ToolResult]


def _check_json_keys(value) -> None:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise InvalidDraftError("工具参数对象的键必须是字符串")
        for item in value.values():
            _check_json_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_json_keys(item)


def tool_arguments_json(arguments: dict[str, Any]) -> str:
    if not isinstance(arguments, dict):
        raise InvalidDraftError("工具参数必须是 JSON 对象")
    try:
        _check_json_keys(arguments)
        return json.dumps(arguments, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidDraftError("工具参数不能序列化为标准 JSON") from exc


class RecordingTools:
    def __init__(
        self,
        client: EvidenceToolClient,
        *,
        allowed_tools: frozenset[str] | None,
        guard: ToolGuard | None,
        interpreter: ResultInterpreter | None,
        runtime: RunRuntime | None = None,
    ):
        self.client = client
        self.allowed_tools = allowed_tools
        self.guard = guard
        self.interpreter = interpreter
        self.runtime = runtime
        self.evidence: list[Evidence] = []
        self._names: set[str] = set()
        self._prepared: deque[tuple[ToolCall, str]] = deque()
        self._call_ids: set[str] = set()

    async def list_tools(self) -> list[ToolSpec]:
        tools = deepcopy(await self.client.list_tools())
        names = [tool.name for tool in tools]
        if len(names) != len(set(names)):
            raise ReliabilityError("工具清单包含重复名称")
        if self.allowed_tools is not None:
            tools = [tool for tool in tools if tool.name in self.allowed_tools]
        self._names = {tool.name for tool in tools}
        return tools

    async def prepare_calls(self, calls: list[ToolCall]) -> None:
        """Check the complete batch before any delegated execution starts."""
        if self._prepared:
            raise ReliabilityError("前一批工具调用尚未执行完成")
        prepared = []
        call_ids = set()
        for call in deepcopy(calls):
            if (
                not isinstance(call, ToolCall) or not isinstance(call.id, str)
                or not call.id.strip() or call.id in self._call_ids or call.id in call_ids
            ):
                raise InvalidDraftError("工具调用 ID 无效或重复")
            if call.name not in self._names:
                raise UnavailableToolError(f"工具未被授权或未注册：{call.name}")
            prepared.append((call, tool_arguments_json(call.arguments)))
            call_ids.add(call.id)
        for call, _ in prepared:
            if self.guard is not None:
                await self.guard(call.name, deepcopy(call.arguments))
        if self.runtime is not None:
            self.runtime.reserve_tools(len(prepared))
        self._prepared.extend(prepared)
        self._call_ids.update(call_ids)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name not in self._names:
            raise UnavailableToolError(f"工具未被授权或未注册：{name}")
        arguments_json = tool_arguments_json(arguments)
        if not self._prepared:
            await self.prepare_calls([ToolCall(f"direct:{len(self._call_ids) + 1}", name, arguments)])
        call, expected_arguments = self._prepared.popleft()
        if call.name != name or arguments_json != expected_arguments:
            raise ReliabilityError("实际工具调用与预检内容不一致")
        original_arguments = deepcopy(arguments)
        index = self.runtime.start_tool(call.id, name, arguments_json) if self.runtime else None
        try:
            raw = await self.client.call_tool(name, deepcopy(original_arguments))
            result = self._interpret(name, original_arguments, raw)
        except BaseException as exc:
            if self.runtime is not None:
                self.runtime.finish_tool(index, error=exc)
            raise
        item = Evidence(
            id=self.runtime.evidence_id(self.runtime.phase) if self.runtime else f"tool:{len(self.evidence) + 1}",
            content=result.content, source=name, kind="tool", status=result.status,
            call_id=call.id, arguments_json=arguments_json, expires_at=result.expires_at,
        )
        if self.runtime is not None:
            try:
                self.runtime.add_evidence(item)
            except Exception:
                self.runtime.finish_tool(index, outcome=result.status)
                raise
            self.runtime.finish_tool(index, outcome=result.status, evidence_id=item.id)
        self.evidence.append(item)
        return result.content

    def _interpret(self, name: str, arguments: dict[str, Any], result) -> ToolResult:
        if isinstance(result, str):
            # Existing adapters expose business errors through this stable prefix.
            failed = result.startswith("工具执行失败：")
            if self.interpreter is not None:
                interpreted = self.interpreter(name, deepcopy(arguments), result)
                if not isinstance(interpreted, ToolResult) or interpreted.content != result:
                    raise ReliabilityError("结果解释器必须保留工具原文并返回 ToolResult")
                result = replace(interpreted, status="error" if failed else interpreted.status)
            else:
                result = ToolResult(result, "error" if failed else "unknown")
        if not isinstance(result, ToolResult):
            raise ReliabilityError("工具必须返回字符串或 ToolResult")
        if result.content.startswith("工具执行失败："):
            result = replace(result, status="error")
        return result
