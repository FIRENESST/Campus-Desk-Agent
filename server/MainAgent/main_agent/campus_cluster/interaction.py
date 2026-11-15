"""Reserved for the user's inter-agent self-learning request algorithm.

No default strategy, dependency rule, message loop or training is implemented.
The coordinator does not invoke this extension automatically.
"""

from dataclasses import dataclass

from .contracts import AgentId, AgentReply, AgentTask


@dataclass(frozen=True)
class PeerRequest:
    sender: AgentId
    recipient: AgentId
    query: str


class InterAgentLearningAlgorithm:
    """TODO: implement request generation and feedback learning here."""

    async def propose_requests(self, task: AgentTask, reply: AgentReply) -> tuple[PeerRequest, ...]:
        raise NotImplementedError("智能体间自学习请求算法待接入")

    async def observe(self, request: PeerRequest, reply: AgentReply) -> None:
        raise NotImplementedError("自学习反馈处理待接入")
