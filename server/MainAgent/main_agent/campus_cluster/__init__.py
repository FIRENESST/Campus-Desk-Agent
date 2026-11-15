from collections.abc import Mapping

from .agents import (
    AGENT_TYPES, CampusIntroductionAgent, CampusServicesAgent, InformationSearchAgent,
    RumorAssessmentAgent, SpecialistAgent,
)
from .backends import ReliableAgentBackend
from .contracts import (
    AgentBackend, AgentId, AgentProfile, AgentReply, AgentTask, ClusterConfig,
    ClusterError, ClusterRequest, ClusterResult, ClusterTimeoutError,
    InvalidRoutePlan, RoutePlan, RumorVerdict, Status, StepResult, UpstreamOutput, WorkStep,
)
from .coordinator import CampusCluster
from .interaction import InterAgentLearningAlgorithm, PeerRequest
from .registry import AgentRegistry
from .routing import ExplicitRouter, LLMRouter, Router
from .routing_feedback import RoutingFeedback
from .skills import Skill, SkillClient, SkillContext, SkillInput, SkillRegistry


def build_campus_cluster(
    *, backends: Mapping[AgentId, AgentBackend] | None = None,
    profiles: Mapping[AgentId, AgentProfile] | None = None,
    router: Router | None = None, config: ClusterConfig | None = None,
) -> CampusCluster:
    backends, profiles = dict(backends or {}), dict(profiles or {})
    if any(not isinstance(key, AgentId) for key in (*backends, *profiles)):
        raise ValueError("后端与配置必须使用 AgentId 作为键")
    agents = [
        agent_type(backends.get(agent_id), **({"profile": profiles[agent_id]} if agent_id in profiles else {}))
        for agent_id, agent_type in AGENT_TYPES.items()
    ]
    return CampusCluster(AgentRegistry(agents), router=router, config=config)


__all__ = [
    "AgentBackend", "AgentId", "AgentProfile", "AgentRegistry", "AgentReply", "AgentTask",
    "CampusCluster", "CampusIntroductionAgent", "CampusServicesAgent", "InformationSearchAgent",
    "RumorAssessmentAgent", "SpecialistAgent", "ClusterConfig", "ClusterError", "ClusterRequest",
    "ClusterResult", "ClusterTimeoutError", "ExplicitRouter", "LLMRouter", "InvalidRoutePlan", "RoutePlan",
    "Router", "RoutingFeedback", "RumorVerdict", "Status", "StepResult", "UpstreamOutput", "WorkStep",
    "ReliableAgentBackend", "InterAgentLearningAlgorithm", "PeerRequest", "build_campus_cluster",
    "Skill", "SkillClient", "SkillContext", "SkillInput", "SkillRegistry",
]
