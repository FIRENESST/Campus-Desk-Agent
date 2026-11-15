from collections.abc import Iterable

from .agents import SpecialistAgent
from .contracts import AgentId, AgentProfile, ClusterError


class AgentRegistry:
    def __init__(self, agents: Iterable[SpecialistAgent]):
        self._agents = {}
        for agent in agents:
            if not isinstance(agent, SpecialistAgent) or agent.profile.id in self._agents:
                raise ClusterError("子智能体类型无效或 ID 重复")
            self._agents[agent.profile.id] = agent
        if set(self._agents) != set(AgentId):
            raise ClusterError("集群必须包含声明的四个子智能体")

    @property
    def profiles(self) -> tuple[AgentProfile, ...]:
        return tuple(self._agents[agent_id].profile for agent_id in AgentId)

    def get(self, agent_id: AgentId) -> SpecialistAgent:
        if not isinstance(agent_id, AgentId) or agent_id not in self._agents:
            raise ClusterError("未注册的子智能体")
        return self._agents[agent_id]
