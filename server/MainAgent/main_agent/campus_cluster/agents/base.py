from copy import deepcopy

from ..contracts import AgentBackend, AgentProfile, AgentReply, AgentTask, ClusterError, Status


class SpecialistAgent:
    """A role and an optional backend, with no embedded campus content."""

    def __init__(self, profile: AgentProfile, backend: AgentBackend | None = None):
        self.profile = profile
        self.backend = backend

    async def run(self, task: AgentTask) -> AgentReply:
        if task.profile != self.profile:
            raise ClusterError("任务与子智能体配置不匹配")
        if self.backend is None:
            return AgentReply(Status.NEEDS_CONFIGURATION, reason="backend_not_configured")
        reply = await self.backend.run(deepcopy(task))
        if not isinstance(reply, AgentReply):
            raise ClusterError("子智能体后端必须返回 AgentReply")
        return reply
