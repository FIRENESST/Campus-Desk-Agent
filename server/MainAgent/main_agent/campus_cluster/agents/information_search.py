from ..contracts import AgentBackend, AgentId, AgentProfile
from ..subskills.registry import SUBSKILL_REGISTRY
from .base import SpecialistAgent


PROFILE = AgentProfile(
    AgentId.INFORMATION_SEARCH, "信息搜寻",
    "负责查找资料、记录来源和有效期、返回可核对证据。近期校园活动优先查询校园日报，"
    "涉及公众号名录时用 campus_wechat_articles 的 accounts，按文章内容查询时用 search 的正文关键词。"
    "用户要求完整名录时，按 next_offset 查到末页再汇总。"
    "根据正文覆盖范围陈述找到的资料，需要确认细节时继续读取选中文章的正文。"
    "区分报道发布日期、举办时间和报名截止。已取得足以回答问题的资料时，直接整理相关条目和来源。",
)


class InformationSearchAgent(SpecialistAgent):
    skill_registry = SUBSKILL_REGISTRY.for_role(PROFILE.id)

    def __init__(self, backend: AgentBackend | None = None, *, profile: AgentProfile = PROFILE):
        if profile.id != AgentId.INFORMATION_SEARCH:
            raise ValueError("信息搜寻配置的 ID 不匹配")
        super().__init__(profile, backend)
