from ..contracts import AgentBackend, AgentId, AgentProfile
from ..subskills.registry import SUBSKILL_REGISTRY
from .base import SpecialistAgent


PROFILE = AgentProfile(
    AgentId.INFORMATION_SEARCH, "信息搜寻",
    "负责查找资料、记录来源和有效期、返回可核对证据。查校方最新或指定届次的通知、报名与活动，先用 search_campus_digest。"
    "用户指定官网时限定官网，指定公众号、公众号名录、正文或提供公众号链接时用 campus_wechat_articles。"
    "保留本轮和会话已确定的主体、届次和来源。日期筛选表示最近发布的天数；查指定历史年份时先放宽时间窗，再核对实际发布日期。"
    "已知标题、账号或链接时填写对应字段，其余只传必要的短检索词。无结果时保留核心对象，去掉次要词或改用校方用语。"
    "同一事项、同一来源在一轮只查询一次。读过回执再补查具体缺口，避免一开始并发多条同义查询。"
    "先看回执能否支持所问细节，摘要不足时读取同一篇文章。用户要求完整名录时，按 next_offset 查到末页再汇总。"
    "根据正文覆盖范围陈述能确认的资料。"
    "找校方的说明时，核对发布单位、发布日期及适用对象，保留可访问的原文链接。"
    "只找到首页或不适用的往年报道时，说明能确认的入口和仍缺的具体文件。"
    "区分报道发布日期、举办时间和报名截止。已取得足以回答问题的资料时，直接整理相关条目和来源。",
)


class InformationSearchAgent(SpecialistAgent):
    skill_registry = SUBSKILL_REGISTRY.for_role(PROFILE.id)

    def __init__(self, backend: AgentBackend | None = None, *, profile: AgentProfile = PROFILE):
        if profile.id != AgentId.INFORMATION_SEARCH:
            raise ValueError("信息搜寻配置的 ID 不匹配")
        super().__init__(profile, backend)
