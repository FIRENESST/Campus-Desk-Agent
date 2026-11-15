"""Domain-neutral contracts for the four campus specialists."""

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

from ..core import AgentError, Message
from ..reliability.models import Evidence, ReliabilityResult, RunSnapshot
from .action_plan import CampusActionPlan

if TYPE_CHECKING:
    from .subskills.campus_timetable import CampusTimetableInput


class AgentId(str, Enum):
    CAMPUS_INTRODUCTION = "campus_introduction"
    INFORMATION_SEARCH = "information_search"
    CAMPUS_SERVICES = "campus_services"
    RUMOR_ASSESSMENT = "rumor_assessment"


class Status(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    NEEDS_CONFIGURATION = "needs_configuration"
    NEEDS_INPUT = "needs_input"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    BLOCKED = "blocked"
    FAILED = "failed"


def aggregate_status(states: set[Status]) -> Status:
    if states == {Status.COMPLETED}:
        return Status.COMPLETED
    if Status.COMPLETED in states or Status.PARTIAL in states:
        return Status.PARTIAL
    for status in (Status.NEEDS_CONFIGURATION, Status.NEEDS_INPUT, Status.INSUFFICIENT_EVIDENCE):
        if status in states:
            return status
    return Status.FAILED


class RumorVerdict(str, Enum):
    SUPPORTED = "supported"
    REFUTED = "refuted"
    MIXED = "mixed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True)
class AgentProfile:
    id: AgentId
    name: str
    responsibility: str
    knowledge_sources: tuple[str, ...] = ()
    allowed_tools: frozenset[str] = frozenset()

    def __post_init__(self):
        if not isinstance(self.id, AgentId) or not self.name or not self.responsibility:
            raise ValueError("子智能体配置无效")
        if not isinstance(self.knowledge_sources, tuple) or any(
            not isinstance(item, str) or not item for item in self.knowledge_sources
        ):
            raise ValueError("knowledge_sources 必须是来源名称元组")
        if not isinstance(self.allowed_tools, frozenset) or any(
            not isinstance(item, str) or not item for item in self.allowed_tools
        ):
            raise ValueError("allowed_tools 必须是工具名 frozenset")


@dataclass(frozen=True)
class ClusterRequest:
    conversation_id: str
    query: str
    targets: tuple[AgentId, ...] = ()
    history: tuple[Message, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    campus_required: bool = False

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() for value in (self.conversation_id, self.query)):
            raise ValueError("conversation_id 和 query 必须是非空字符串")
        if not isinstance(self.targets, tuple) or any(not isinstance(item, AgentId) for item in self.targets):
            raise ValueError("targets 必须是 AgentId 元组")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("targets 不能重复")
        if not isinstance(self.history, tuple) or any(not isinstance(item, Message) for item in self.history):
            raise ValueError("history 必须是 Message 元组")
        if not isinstance(self.evidence, tuple) or any(not isinstance(item, Evidence) for item in self.evidence):
            raise ValueError("evidence 必须是 Evidence 元组")
        if not isinstance(self.campus_required, bool):
            raise ValueError("campus_required 必须为布尔值")


@dataclass(frozen=True)
class WorkStep:
    id: str
    agent_id: AgentId
    depends_on: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.id):
            raise ValueError("步骤 ID 必须是小写字母开头的标识符")
        if not isinstance(self.agent_id, AgentId):
            raise ValueError("步骤必须选择一个已声明的 AgentId")
        if not isinstance(self.depends_on, tuple) or any(not isinstance(item, str) for item in self.depends_on):
            raise ValueError("depends_on 必须是步骤 ID 元组")


@dataclass(frozen=True)
class QuestionPart:
    query: str
    plan: "RoutePlan"
    depends_on: tuple[int, ...] = ()


@dataclass(frozen=True)
class RoutePlan:
    steps: tuple[WorkStep, ...] = ()
    clarification: str | None = None
    direct: bool = False
    general: bool = False
    campus_action: CampusActionPlan | None = None
    support: bool = False
    service_guide: Evidence | None = None
    resolved_query: str | None = None
    campus_threat: bool = False
    timetable_query: "CampusTimetableInput | None" = None
    parts: tuple[QuestionPart, ...] = ()


@dataclass(frozen=True)
class UpstreamOutput:
    step_id: str
    agent_id: AgentId
    content: str


@dataclass(frozen=True)
class AgentTask:
    conversation_id: str
    step_id: str
    profile: AgentProfile
    query: str
    history: tuple[Message, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    upstream: tuple[UpstreamOutput, ...] = ()
    resolved_query: str | None = None


@dataclass(frozen=True)
class AgentReply:
    status: Status
    content: str = ""
    evidence: tuple[Evidence, ...] = ()
    reason: str | None = None
    rumor_verdict: RumorVerdict | None = None
    reliability: ReliabilityResult | None = None
    snapshot: RunSnapshot | None = None

    def __post_init__(self):
        if not isinstance(self.status, Status) or not isinstance(self.content, str):
            raise ValueError("子智能体回复类型无效")
        if not isinstance(self.evidence, tuple) or any(not isinstance(item, Evidence) for item in self.evidence):
            raise ValueError("回复证据必须是 Evidence 元组")
        if self.status == Status.COMPLETED and not self.content.strip():
            raise ValueError("已完成的子任务必须提供内容")
        if self.rumor_verdict is not None and not isinstance(self.rumor_verdict, RumorVerdict):
            raise ValueError("rumor_verdict 必须是 RumorVerdict")


class AgentBackend(Protocol):
    async def run(self, task: AgentTask) -> AgentReply:
        ...


@dataclass(frozen=True)
class StepResult:
    step_id: str
    agent_id: AgentId
    reply: AgentReply
    error_type: str | None = None
    snapshot: RunSnapshot | None = None


@dataclass(frozen=True)
class ClusterResult:
    conversation_id: str
    status: Status
    plan: RoutePlan
    results: tuple[StepResult, ...] = ()
    clarification: str | None = None


@dataclass(frozen=True)
class ClusterConfig:
    max_steps: int = 8
    max_parallel: int = 4
    step_timeout: float = 120.0
    total_timeout: float = 240.0
    max_context_chars: int = 120_000

    def __post_init__(self):
        for name in ("max_steps", "max_parallel", "max_context_chars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须是正整数")
        for name in ("step_timeout", "total_timeout"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须是正的有限秒数")


class ClusterError(AgentError):
    pass


class InvalidRoutePlan(ClusterError):
    pass


class ClusterTimeoutError(ClusterError):
    def __init__(self, results: tuple[StepResult, ...]):
        super().__init__("校园智能体编排超过总时间预算")
        self.results = results
