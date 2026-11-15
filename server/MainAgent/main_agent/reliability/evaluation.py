"""Evaluate delivered answers with a scorer independent of the runtime gate.

No model, credentials or network are selected implicitly. The caller supplies
both the runner and the scorer, and controls their budgets and reference labels.
"""

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from copy import deepcopy

from ..core import Message
from .models import Evidence, ReliabilityResult, RunStats


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    messages: tuple[Message, ...]
    references: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class EvaluationScore:
    task_success: bool
    factual: bool
    follows_instructions: bool

    def __post_init__(self):
        if any(type(value) is not bool for value in (
            self.task_success, self.factual, self.follows_instructions
        )):
            raise ValueError("评测标签必须是布尔值")

    @property
    def passed(self) -> bool:
        return self.task_success and self.factual and self.follows_instructions


@dataclass(frozen=True)
class EvaluationRecord:
    case_id: str
    delivered: bool
    score: EvaluationScore | None
    error_type: str | None
    elapsed_seconds: float
    error_stage: str | None = None
    stats: RunStats | None = None


@dataclass(frozen=True)
class EvaluationReport:
    records: tuple[EvaluationRecord, ...]

    @property
    def delivery_rate(self) -> float:
        return sum(record.delivered for record in self.records) / len(self.records) if self.records else 0.0

    @property
    def pass_rate(self) -> float:
        # Rejected and failed cases stay in the denominator.
        return sum(
            record.score is not None and record.score.passed for record in self.records
        ) / len(self.records) if self.records else 0.0

    def _rate(self, field: str) -> float:
        return sum(
            record.score is not None and getattr(record.score, field)
            for record in self.records
        ) / len(self.records) if self.records else 0.0

    @property
    def factual_rate(self) -> float:
        return self._rate("factual")

    @property
    def task_success_rate(self) -> float:
        return self._rate("task_success")

    @property
    def instruction_rate(self) -> float:
        return self._rate("follows_instructions")


async def evaluate(
    cases: Sequence[EvaluationCase],
    runner: Callable[[EvaluationCase], Awaitable[str | ReliabilityResult]],
    scorer: Callable[[EvaluationCase, str], Awaitable[EvaluationScore]],
    *,
    run_timeout: float = 240.0,
    score_timeout: float = 60.0,
    max_concurrency: int = 1,
) -> EvaluationReport:
    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
        raise ValueError("max_concurrency 必须是正整数")
    for value in (run_timeout, score_timeout):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("评测超时必须是正的有限秒数")
    cases = tuple(deepcopy(cases))
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("评测用例 ID 不能重复")
    semaphore = asyncio.Semaphore(max_concurrency)

    async def run_case(case):
        async with semaphore:
            return await score_case(case)

    async def score_case(case):
        started = time.monotonic()
        delivered = False
        score = None
        error_type = None
        stage = "runner"
        stats = None
        try:
            async with asyncio.timeout(run_timeout):
                result = await runner(deepcopy(case))
            answer = result.answer if isinstance(result, ReliabilityResult) else result
            stats = result.stats if isinstance(result, ReliabilityResult) else None
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("评测运行器未交付有效文本")
            delivered = True
            stage = "scorer"
            async with asyncio.timeout(score_timeout):
                score = await scorer(deepcopy(case), answer)
            if not isinstance(score, EvaluationScore):
                raise TypeError("评测器必须返回 EvaluationScore")
        except Exception as exc:
            score = None
            error_type = type(exc).__name__
            stats = getattr(getattr(exc, "snapshot", None), "stats", stats)
        return EvaluationRecord(
            case.id, delivered, score, error_type, time.monotonic() - started,
            stage if error_type else None, stats,
        )

    tasks = [asyncio.create_task(run_case(case)) for case in cases]
    try:
        return EvaluationReport(tuple(await asyncio.gather(*tasks)))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
