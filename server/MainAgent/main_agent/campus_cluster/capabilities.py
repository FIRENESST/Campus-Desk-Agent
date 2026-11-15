"""Publish the default specialist registrations for the local MCP process."""

import json
import logging
from collections.abc import Collection, Iterable, Mapping
from contextlib import suppress
from importlib import import_module
from pathlib import Path
from pkgutil import iter_modules
from tempfile import NamedTemporaryFile

from . import subskills
from .agents import AGENT_TYPES
from .contracts import AgentId, AgentProfile
from .registry import AgentRegistry
from .skills import Skill, SkillRegistry
from .subskills.registry import SUBSKILL_ASSIGNMENTS


CATALOG_PATH = Path(__file__).resolve().parents[2] / ".runtime" / "subagent-capabilities.json"
logger = logging.getLogger(__name__)


def default_skill_registries() -> dict[AgentId, SkillRegistry]:
    return {role: agent_type.skill_registry for role, agent_type in AGENT_TYPES.items()}


def describe_capabilities(
    profiles: Iterable[AgentProfile], registries: Mapping[AgentId, SkillRegistry],
) -> list[dict]:
    return [
        {"id": profile.id.value, "name": profile.name, "responsibility": profile.responsibility,
         "skills": registries[profile.id].describe(profile)}
        for profile in profiles
    ]


def scope_actions(agents: list[dict], role: str | None, bound_actions: Mapping[str, Collection[str]]) -> list[dict]:
    """Project the registration catalog into the current run without publishing user data."""
    def scope(skill):
        declared = skill["actions"]
        visible = []
        if skill["enabled"] and role:
            live = bound_actions.get(skill["name"], ())
            for action in declared:
                if action["id"] not in live:
                    continue
                for access in action["access"]:
                    if role in access["roles"]:
                        visible.append({
                            "id": action["id"], "label": action["label"],
                            "arguments": action.get("arguments", {}),
                            **{key: access[key] for key in ("required_fields", "checks", "example")},
                        })
                        break
        return {**skill, "actions": visible, "enabled": skill["enabled"] and (not declared or bool(visible))}

    return [{**agent, "skills": [scope(skill) for skill in agent["skills"]]} for agent in agents]


def _discover_skills() -> dict[str, Skill]:
    skills = {skill.name: skill for skill, _ in SUBSKILL_ASSIGNMENTS}
    for module in iter_modules(subskills.__path__, prefix=f"{subskills.__name__}."):
        try:
            definition_module = import_module(module.name)
        except Exception as exc:
            logger.warning("无法盘点技能模块 %s (%s)", module.name, type(exc).__name__)
            continue
        for value in vars(definition_module).values():
            if isinstance(value, Skill):
                skills.setdefault(value.name, value)
    return skills


def _describe_skills(agents: list[dict]) -> list[dict]:
    skills = {
        name: {**skill.describe(), "registered_agents": []}
        for name, skill in _discover_skills().items()
    }
    for agent in agents:
        for skill in agent["skills"]:
            name = skill["name"]
            description = skills.setdefault(name, {
                **{key: skill[key] for key in ("name", "description", "read_only", "actions")},
                "registered_agents": [],
            })
            description["registered_agents"].append({
                "id": agent["id"], "name": agent["name"], "enabled": skill["enabled"],
            })
    return [skills[name] for name in sorted(skills)]


def default_capability_catalog() -> dict:
    profiles = AgentRegistry(agent_type() for agent_type in AGENT_TYPES.values()).profiles
    agents = describe_capabilities(profiles, default_skill_registries())
    return {"agents": agents, "skills": _describe_skills(agents)}


def publish_default_capabilities(path: Path = CATALOG_PATH) -> None:
    catalog = default_capability_catalog()
    pending = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            pending = Path(handle.name)
            json.dump(catalog, handle, ensure_ascii=False)
        pending.replace(path)
        logger.info("已发布子智能体能力清单（%d 个角色，%d 项技能）", len(catalog["agents"]), len(catalog["skills"]))
    except OSError as exc:
        logger.warning("无法发布子智能体能力清单 (%s)", type(exc).__name__)
    finally:
        if pending is not None:
            with suppress(OSError):
                pending.unlink(missing_ok=True)
