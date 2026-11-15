"""Contracts shared by the generation, review, and repair stages."""

import math
from dataclasses import dataclass
from typing import Literal, Protocol

from ..core import AgentError, Message


EvidenceStatus = Literal["unknown", "success", "error"]
CheckStatus = Literal["pass", "fail", "unknown"]


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")


@dataclass(frozen=True)
class Rule:
    id: str
    instruction: str

    def __post_init__(self):
        _text(self.id, "规则 ID")
        _text(self.instruction, "规则内容")


BUILTIN_RULES = (
    Rule("builtin.instructions", "遵循原始 system/developer 指令及其优先级。"),
    Rule("builtin.claim_coverage", "列出答案中所有可核查事实及操作完成声明，不遗漏无依据的声明。"),
    Rule("builtin.task_completion", "回应用户的实际任务；缺少资料时明确范围，避免无谓拒答或用拒答掩盖未完成。"),
)


@dataclass(frozen=True)
class ToolResult:
    """An application receipt; success must reflect the business outcome."""

    content: str
    status: EvidenceStatus = "unknown"
    expires_at: float | None = None

    def __post_init__(self):
        if not isinstance(self.content, str):
            raise ValueError("工具内容必须是字符串")
        if self.status not in ("unknown", "success", "error"):
            raise ValueError("工具状态无效")
        _expiry(self.expires_at)


def _expiry(value: float | None) -> None:
    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("expires_at 必须是正的有限 Unix 时间戳")


@dataclass(frozen=True)
class Evidence:
    id: str
    content: str
    source: str
    kind: Literal["reference", "tool"] = "reference"
    status: EvidenceStatus = "unknown"
    call_id: str | None = None
    arguments_json: str | None = None
    expires_at: float | None = None

    def __post_init__(self):
        _text(self.id, "证据 ID")
        _text(self.source, "证据来源")
        if not isinstance(self.content, str):
            raise ValueError("证据内容必须是字符串")
        if self.kind not in ("reference", "tool"):
            raise ValueError("证据类型无效")
        if self.status not in ("unknown", "success", "error"):
            raise ValueError("证据状态无效")
        if self.call_id is not None:
            _text(self.call_id, "调用 ID")
        if self.arguments_json is not None and not isinstance(self.arguments_json, str):
            raise ValueError("调用参数记录必须是 JSON 字符串")
        _expiry(self.expires_at)


@dataclass(frozen=True)
class Citation:
    evidence_id: str
    quote: str


@dataclass(frozen=True)
class Claim:
    text: str
    kind: Literal["fact", "action"]
    status: Literal["supported", "unsupported", "contradicted"]
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True)
class RuleCheck:
    rule_id: str
    status: CheckStatus
    reason: str


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    rule_id: str | None = None


@dataclass(frozen=True)
class Review:
    rule_checks: tuple[RuleCheck, ...]
    claims: tuple[Claim, ...] = ()
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewContext:
    messages: tuple[Message, ...]
    rules: tuple[Rule, ...]
    evidence: tuple[Evidence, ...]
    answer: str


class Reviewer(Protocol):
    async def review(self, context: ReviewContext) -> Review:
        ...


class EvidenceRetriever(Protocol):
    async def collect(
        self, context: ReviewContext, findings: tuple[Finding, ...]
    ) -> tuple[Evidence, ...]:
        ...


class OutputCheck(Protocol):
    def check(self, context: ReviewContext) -> tuple[Finding, ...]:
        ...


@dataclass(frozen=True)
class ReliabilityConfig:
    max_repairs: int = 2
    max_retrieval_rounds: int = 1
    max_tool_rounds: int = 5
    llm_timeout: float = 60.0
    tool_timeout: float = 30.0
    review_timeout: float = 60.0
    retrieval_timeout: float = 60.0
    total_timeout: float = 240.0
    max_context_chars: int = 120_000
    max_model_calls: int = 24
    max_tool_calls: int = 20
    max_tool_calls_per_batch: int = 8
    max_evidence_items: int = 128
    max_evidence_chars: int = 80_000

    def __post_init__(self):
        for name in (
            "max_repairs", "max_retrieval_rounds", "max_tool_rounds",
            "max_context_chars", "max_model_calls", "max_tool_calls",
            "max_tool_calls_per_batch", "max_evidence_items", "max_evidence_chars",
        ):
            value = getattr(self, name)
            minimum = 1 if name in ("max_context_chars", "max_evidence_items", "max_evidence_chars") else 0
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} 必须是大于等于 {minimum} 的整数")
        for name in ("llm_timeout", "tool_timeout", "review_timeout", "retrieval_timeout", "total_timeout"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} 必须是大于 0 的有限秒数")


@dataclass(frozen=True)
class Attempt:
    index: int
    answer: str
    review: Review | None
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class RunStats:
    generation_calls: int
    review_rounds: int
    tool_executions: int
    repairs: int
    retrieval_rounds: int
    elapsed_seconds: float
    model_calls: int = 0
    review_model_calls: int = 0
    retrieval_model_calls: int = 0
    total_tool_executions: int = 0


@dataclass(frozen=True)
class RunEvent:
    sequence: int
    phase: str
    kind: str
    elapsed_seconds: float
    name: str | None = None


@dataclass(frozen=True)
class ToolExecution:
    call_id: str
    name: str
    arguments_json: str
    phase: str
    state: Literal["started", "completed", "uncertain"] = "started"
    outcome: EvidenceStatus = "unknown"
    evidence_id: str | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class RunSnapshot:
    phase: str
    events: tuple[RunEvent, ...]
    executions: tuple[ToolExecution, ...]
    evidence: tuple[Evidence, ...]
    attempts: tuple[Attempt, ...]
    stats: RunStats


@dataclass(frozen=True)
class ReliabilityResult:
    """Approved by configured checks, not a guarantee of objective truth."""

    answer: str
    history: tuple[Message, ...]
    evidence: tuple[Evidence, ...]
    attempts: tuple[Attempt, ...]
    stats: RunStats
    snapshot: RunSnapshot | None = None


class ReliabilityError(AgentError):
    snapshot: RunSnapshot | None = None


class BudgetExceeded(ReliabilityError):
    def __init__(self, resource: str, limit: int):
        super().__init__(f"可信性编排超过 {resource} 预算：{limit}")
        self.resource = resource
        self.limit = limit


class InvalidReviewError(ReliabilityError):
    pass


class InvalidDraftError(ReliabilityError):
    pass


class ContextLimitExceeded(ReliabilityError):
    pass


class ReliabilityTimeoutError(ReliabilityError):
    pass


class VerificationFailed(ReliabilityError):
    def __init__(self, attempts: tuple[Attempt, ...], evidence: tuple[Evidence, ...]):
        super().__init__("回答在修复预算内未通过检查；未交付未经批准的答案")
        self.attempts = attempts
        self.evidence = evidence
