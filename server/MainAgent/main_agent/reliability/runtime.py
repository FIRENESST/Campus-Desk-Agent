"""Per-run lifecycle, accounting and immutable failure snapshots."""

import time
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, replace

from ..core import LLMClient, Message
from .models import (
    Attempt, BudgetExceeded, Evidence, InvalidDraftError, ReliabilityConfig, ReliabilityError,
    RunEvent, RunSnapshot, RunStats, ToolExecution,
)
from .review import bounded_json, complete_review_json


_TRANSITIONS = {
    "pending": {"generation"},
    "generation": {"check"},
    "check": {"review", "repair", "retrieval"},
    "review": {"retrieval", "repair", "approved"},
    "retrieval": {"repair"},
    "repair": {"check"},
}
_TERMINAL = {"approved", "failed", "cancelled"}


class RunRuntime:
    """Owned by one asyncio run; shared only by that run's bounded stages."""

    def __init__(self, config: ReliabilityConfig):
        self.config = config
        self.phase = "pending"
        self.started = time.monotonic()
        self._events: list[RunEvent] = []
        self._executions: list[ToolExecution] = []
        self._evidence: dict[str, Evidence] = {}
        self._evidence_chars = 0
        self._ids: Counter = Counter()
        self._models: Counter = Counter()
        self._reserved_tools = 0
        self.attempts: list[Attempt] = []
        self.review_rounds = 0
        self.retrieval_rounds = 0
        self.repairs = 0

    def event(self, kind: str, name: str | None = None) -> None:
        self._events.append(RunEvent(
            len(self._events) + 1, self.phase, kind,
            time.monotonic() - self.started, name,
        ))

    def transition(self, phase: str) -> None:
        if self.phase in _TERMINAL:
            raise ReliabilityError("已结束的运行不能继续执行")
        if phase not in ("failed", "cancelled") and phase not in _TRANSITIONS.get(self.phase, set()):
            raise ReliabilityError(f"无效的运行阶段转换：{self.phase} -> {phase}")
        self.phase = phase
        self.event("phase")

    @property
    def evidence(self) -> tuple[Evidence, ...]:
        return tuple(self._evidence.values())

    def add_evidence(self, item: Evidence) -> None:
        if item.id in self._evidence:
            if self._evidence[item.id] != item:
                raise ReliabilityError("同一证据 ID 不能被其他内容覆盖")
            return
        if len(self._evidence) >= self.config.max_evidence_items:
            self.event("budget.exceeded", "evidence_items")
            raise BudgetExceeded("evidence_items", self.config.max_evidence_items)
        if self._evidence_chars + len(item.content) > self.config.max_evidence_chars:
            self.event("budget.exceeded", "evidence_chars")
            raise BudgetExceeded("evidence_chars", self.config.max_evidence_chars)
        self._evidence[item.id] = item
        self._evidence_chars += len(item.content)

    def evidence_id(self, phase: str) -> str:
        prefix = f"lookup:{self.retrieval_rounds}" if phase == "retrieval" else "tool"
        self._ids[prefix] += 1
        return f"{prefix}:{self._ids[prefix]}"

    def reserve_tools(self, count: int) -> None:
        if count > self.config.max_tool_calls_per_batch:
            self.event("budget.exceeded", "tool_calls_per_batch")
            raise BudgetExceeded("tool_calls_per_batch", self.config.max_tool_calls_per_batch)
        if self._reserved_tools + count > self.config.max_tool_calls:
            self.event("budget.exceeded", "tool_calls")
            raise BudgetExceeded("tool_calls", self.config.max_tool_calls)
        self._reserved_tools += count

    def start_tool(self, call_id: str, name: str, arguments_json: str) -> int:
        self._executions.append(ToolExecution(call_id, name, arguments_json, self.phase))
        self.event("tool.start", name)
        return len(self._executions) - 1

    def finish_tool(self, index: int, *, outcome="unknown", evidence_id=None, error=None) -> None:
        self._executions[index] = replace(
            self._executions[index], state="uncertain" if error else "completed",
            outcome=outcome, evidence_id=evidence_id,
            error_type=type(error).__name__ if error else None,
        )
        self.event("tool.uncertain" if error else "tool.complete", self._executions[index].name)

    def model(self, client: LLMClient, phase: str):
        if phase not in ("generation", "review", "retrieval", "repair"):
            raise ValueError("模型阶段无效")
        return _BudgetedModel(client, self, phase)

    def stats(self) -> RunStats:
        return RunStats(
            generation_calls=self._models["generation"] + self._models["repair"],
            review_rounds=self.review_rounds,
            tool_executions=sum(item.phase == "generation" for item in self._executions),
            repairs=self.repairs,
            retrieval_rounds=self.retrieval_rounds,
            elapsed_seconds=time.monotonic() - self.started,
            model_calls=sum(self._models.values()),
            review_model_calls=self._models["review"],
            retrieval_model_calls=self._models["retrieval"],
            total_tool_executions=len(self._executions),
        )

    def snapshot(self) -> RunSnapshot:
        return RunSnapshot(
            self.phase, tuple(self._events), tuple(self._executions), self.evidence,
            tuple(self.attempts), self.stats(),
        )


class _BudgetedModel:
    def __init__(self, client: LLMClient, runtime: RunRuntime, phase: str):
        self.client = client
        self.runtime = runtime
        self.phase = phase

    async def complete(self, messages, tools):
        return await self._complete(messages, tools)

    async def complete_json(self, messages):
        return await self._complete(messages, [], structured=True)

    async def _complete(self, messages, tools, *, structured=False):
        runtime = self.runtime
        if runtime.phase in _TERMINAL:
            raise ReliabilityError("运行已经结束，不能再请求模型")
        if runtime.phase != self.phase:
            raise ReliabilityError("模型调用与当前运行阶段不一致")
        bounded_json({
            "messages": [asdict(message) for message in messages],
            "tools": [asdict(tool) for tool in tools],
        }, runtime.config.max_context_chars)
        if sum(runtime._models.values()) >= runtime.config.max_model_calls:
            runtime.event("budget.exceeded", "model_calls")
            raise BudgetExceeded("model_calls", runtime.config.max_model_calls)
        # No await between the shared budget check and reservation.
        runtime._models[self.phase] += 1
        runtime.event("model.start", self.phase)
        try:
            if structured:
                response = await complete_review_json(self.client, deepcopy(messages))
            else:
                response = await self.client.complete(deepcopy(messages), deepcopy(tools))
            if isinstance(response, Message):
                try:
                    bounded_json(asdict(response), runtime.config.max_context_chars)
                except (TypeError, ValueError, RecursionError) as exc:
                    raise InvalidDraftError("模型响应包含无法序列化的数据") from exc
        except BaseException:
            runtime.event("model.failed", self.phase)
            raise
        runtime.event("model.complete", self.phase)
        return response


def bind_runtime(component, runtime: RunRuntime):
    """Custom components can opt into the same run budget with this hook."""
    bind = getattr(component, "bind_runtime", None)
    if callable(bind):
        return bind(runtime)
    runtime.event("component.external", type(component).__name__)
    return component
