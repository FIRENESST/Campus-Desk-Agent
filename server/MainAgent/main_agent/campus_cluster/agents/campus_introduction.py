from ..contracts import AgentBackend, AgentId, AgentProfile
from ..subskills.registry import SUBSKILL_REGISTRY
from .base import SpecialistAgent


PROFILE = AgentProfile(
    AgentId.CAMPUS_INTRODUCTION, "校园介绍",
    "负责校园概况、院系及设施介绍，以及校内步行路线和街景点位查询。优先使用已有学校公开资料，简述有依据的要点。"
    "查询开放时间、校园规章、场馆预约或借用流程时，先用 search_rag 检索具体制度和适用范围。"
    "问路时，只要起点和终点明确，就调用 campus_street_view，分别填写 start 和 destination，不要拆成两个点位查询。"
    "结合对话确定指代。如果用户只说校门且有多个可能的入口，先问从哪个门出发。"
    "walking_route 返回的道路、方向和距离直接用于回答，保留起终点提示。没有连通路线时只提供有依据的定位。",
)


class CampusIntroductionAgent(SpecialistAgent):
    skill_registry = SUBSKILL_REGISTRY.for_role(PROFILE.id)

    def __init__(self, backend: AgentBackend | None = None, *, profile: AgentProfile = PROFILE):
        if profile.id != AgentId.CAMPUS_INTRODUCTION:
            raise ValueError("校园介绍配置的 ID 不匹配")
        super().__init__(profile, backend)
