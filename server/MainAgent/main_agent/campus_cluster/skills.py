"""Register and bind local capabilities for the four campus specialists."""

import re
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from copy import copy, deepcopy
from dataclasses import dataclass, replace
from inspect import iscoroutinefunction
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from ..core import ToolSpec, UnavailableToolError
from ..reliability.models import InvalidDraftError, ReliabilityError, ToolResult
from ..reliability.tools import tool_arguments_json
from .contracts import AgentId, AgentProfile, AgentTask


class SkillInput(BaseModel):
    """Declare a skill's input fields once for schema generation and validation."""

    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)


@dataclass(frozen=True)
class SkillContext:
    agent_id: AgentId
    conversation_id: str
    step_id: str


InputT = TypeVar("InputT", bound=SkillInput)


class ActionAccess(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    roles: tuple[str, ...]
    required_fields: tuple[str, ...]
    checks: tuple[str, ...] = ()
    example: str = ""


class SkillAction(BaseModel):
    """A discoverable business action and the requirements for each eligible role."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    label: str
    access: tuple[ActionAccess, ...]
    arguments: dict[str, str] = Field(default_factory=dict)

    def for_role(self, role: str) -> ActionAccess | None:
        return next((entry for entry in self.access if role in entry.roles), None)


@dataclass(frozen=True)
class Skill(Generic[InputT]):
    name: str
    description: str
    input_model: type[InputT]
    handler: Callable[[SkillContext, InputT], Awaitable[str | ToolResult]]
    read_only: bool = False
    actions: tuple[SkillAction, ...] = ()

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.name):
            raise ValueError("技能名称需为 1 到 64 个英文字母、数字、下划线或连字符")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("技能需要非空描述")
        if not isinstance(self.input_model, type) or not issubclass(self.input_model, SkillInput):
            raise ValueError("技能参数模型必须继承 SkillInput")
        if not (
            iscoroutinefunction(self.handler) or iscoroutinefunction(getattr(self.handler, "__call__", None))
        ):
            raise ValueError("技能处理函数必须是 async")
        if type(self.read_only) is not bool:
            raise ValueError("read_only 必须是布尔值")
        if not isinstance(self.actions, tuple) or any(not isinstance(action, SkillAction) for action in self.actions):
            raise ValueError("技能动作必须是 SkillAction 元组")
        if self.read_only and self.actions:
            raise ValueError("只读技能不能登记办理动作")
        if len({action.id for action in self.actions}) != len(self.actions):
            raise ValueError("同一技能的办理动作 ID 不能重复")

    def describe(self) -> dict:
        return {"name": self.name, "description": self.description, "read_only": self.read_only,
                "actions": [action.model_dump(mode="json") for action in self.actions]}


@dataclass(frozen=True)
class _RegisteredSkill:
    definition: Skill[Any]
    tool: ToolSpec


class SkillClient:
    """The local capability set for one subagent task."""

    def __init__(self, skills: Mapping[str, _RegisteredSkill], context: SkillContext):
        self._skills = dict(skills)
        self._context = context

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._skills)

    async def list_tools(self) -> list[ToolSpec]:
        return [deepcopy(skill.tool) for skill in self._skills.values()]

    def _validate(self, name: str, arguments: dict[str, Any]) -> tuple[Skill[Any], SkillInput]:
        if not isinstance(name, str) or name not in self._skills:
            raise UnavailableToolError(f"当前子智能体未注册技能 {name}")
        skill = self._skills[name].definition
        payload = tool_arguments_json(arguments)
        try:
            parsed = skill.input_model.model_validate_json(payload, strict=True, extra="forbid")
        except ValueError:
            raise InvalidDraftError(f"技能 {name} 的参数不符合定义") from None
        return skill, parsed

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> None:
        self._validate(name, arguments)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str | ToolResult:
        skill, parsed = self._validate(name, arguments)
        result = await skill.handler(self._context, parsed)
        if not isinstance(result, (str, ToolResult)):
            raise ReliabilityError(f"技能 {name} 必须返回字符串或 ToolResult")
        return result


class SkillRegistry:
    """Take a snapshot of trusted, explicitly assigned skill definitions."""

    def __init__(self, by_role: Mapping[AgentId, Iterable[Skill[Any]]]):
        self._by_role: dict[AgentId, dict[str, _RegisteredSkill]] = {}
        for role, definitions in by_role.items():
            if not isinstance(role, AgentId):
                raise ValueError("技能注册必须使用 AgentId 作为角色")
            entries: dict[str, _RegisteredSkill] = {}
            for skill in definitions:
                if not isinstance(skill, Skill):
                    raise ValueError("角色的技能列表必须包含 Skill")
                if skill.name in entries:
                    raise ValueError(f"角色 {role.value} 重复注册了技能 {skill.name}")
                tool = ToolSpec(skill.name, skill.description, skill.input_model.model_json_schema())
                entries[skill.name] = _RegisteredSkill(skill, tool)
            self._by_role[role] = entries

    @classmethod
    def from_assignments(cls, assignments: Iterable[tuple[Skill[Any], Iterable[AgentId]]]) -> "SkillRegistry":
        """Register each skill once and distribute it to its declared roles."""
        by_role: dict[AgentId, list[Skill[Any]]] = {role: [] for role in AgentId}
        names: set[str] = set()
        for skill, roles in assignments:
            if not isinstance(skill, Skill):
                raise ValueError("统一技能注册必须包含 Skill")
            if skill.name in names:
                raise ValueError(f"统一技能表重复注册了技能 {skill.name}")
            names.add(skill.name)
            for role in roles:
                if not isinstance(role, AgentId):
                    raise ValueError("技能注册必须使用 AgentId 作为角色")
                by_role[role].append(skill)
        return cls(by_role)

    def for_role(self, role: AgentId) -> "SkillRegistry":
        """Keep only one role's registered skills without rebuilding their schemas."""
        if not isinstance(role, AgentId):
            raise ValueError("技能注册必须使用 AgentId 作为角色")
        registry = copy(self)
        registry._by_role = {role: self._by_role.get(role, {})}
        return registry

    def with_handlers(self, handlers: Mapping[str, Callable]) -> "SkillRegistry":
        """Bind trusted per-run implementations while keeping the registered schemas and scopes."""
        registry = copy(self)
        registry._by_role = {
            role: {name: replace(entry, definition=replace(entry.definition, handler=handlers[name]))
                   if name in handlers else entry for name, entry in entries.items()}
            for role, entries in self._by_role.items()
        }
        return registry

    @staticmethod
    def _profile_allows(profile: AgentProfile, name: str) -> bool:
        return not profile.allowed_tools or name in profile.allowed_tools

    def describe(self, profile: AgentProfile) -> list[dict]:
        """Return registration metadata and the profile's effective access."""
        return [
            {**skill.definition.describe(), "enabled": self._profile_allows(profile, name)}
            for name, skill in self._by_role.get(profile.id, {}).items()
        ]

    def bind(
        self, task: AgentTask, *, allowed_tools: Collection[str] | None = None, read_only: bool = False,
    ) -> SkillClient:
        if not isinstance(task, AgentTask) or not isinstance(task.profile, AgentProfile):
            raise ValueError("技能必须绑定到一个子智能体任务")
        if isinstance(allowed_tools, str):
            raise ValueError("allowed_tools 必须是技能名称集合")
        role = task.profile.id
        skills = {
            name: skill for name, skill in self._by_role.get(role, {}).items()
            if self._profile_allows(task.profile, name)
            and (allowed_tools is None or name in allowed_tools)
            and (not read_only or skill.definition.read_only)
        }
        context = SkillContext(role, task.conversation_id, task.step_id)
        return SkillClient(skills, context)
