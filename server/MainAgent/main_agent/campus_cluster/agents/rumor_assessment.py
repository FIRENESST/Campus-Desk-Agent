from ...reliability import Finding, ReviewContext, Rule
from ...reliability.review import structured_receipt
from ..contracts import AgentBackend, AgentId, AgentProfile, AgentTask, ClusterError, RumorVerdict, Status
from ..subskills.campus_digest import CAMPUS_DIGEST
from ..subskills.registry import SUBSKILL_REGISTRY
from ..subskills.rumor_assessment import CAMPUS_RUMOR, official_evidence
from .base import SpecialistAgent


_HEADINGS = {
    "结论为有证据支持": RumorVerdict.SUPPORTED,
    "结论为有证据反驳": RumorVerdict.REFUTED,
    "结论为证据混合": RumorVerdict.MIXED,
    "结论为证据不足": RumorVerdict.INSUFFICIENT_EVIDENCE,
}
RUMOR_VERDICT_RULE = Rule(
    "campus.rumor.verdict",
    "首行从以下措辞中选择一句。"
    + "、".join(_HEADINGS)
    + "。明确待核说法时，先用 assess_campus_rumor 提取主张和核对线索。凭同一会话确认对象、时间、校区、适用人群及否定方向。"
    "原帖有发布日期时将原日期传入 published，并按该日期理解「明天」等相对时间。"
    "候选分类、复核优先级、用户转述、模拟校务数据和官网首页均不能证明消息真伪。"
    "本服务已有复核记录或官方对照时引用对应原文、日期和链接。缺少对应证据时用校园日报找校方通知。"
    "若线索来自公众号，用 campus_wechat_articles 找到并读到正文，再核对发布者是否是负责发布该事项的校方单位。"
    "标题、摘要、账号名称及公众号转载只能辅助定位，不能单独作为正式辟谣的依据。"
    "核实变动时，需要能对应到本次变动及生效时间的证据。只有历史资料或缺少可靠证据时使用「结论为证据不足」。"
    "结论后简短写明这次核实的具体说法和用户补充的范围，再说明已核实的部分及仍缺的关键资料。"
    "用「传闻称」「用户提到」标明未经证实的内容及出处。",
)


def _can_verify(evidence, verdict):
    if evidence.kind != "tool":
        return evidence.status != "error"
    if evidence.status != "success" or evidence.source not in (CAMPUS_RUMOR.name, CAMPUS_DIGEST.name):
        return False
    receipt = structured_receipt(evidence.content)
    if not isinstance(receipt, dict):
        return False
    if evidence.source == CAMPUS_DIGEST.name:
        articles = receipt.get("items")
        if not isinstance(articles, list):
            return False
        return any(
            official_evidence({"provenance": "official_article", "url": article.get("url"),
                               "published": article.get("published_on"), "quote": article.get("excerpt")})
            for article in articles if isinstance(article, dict)
        )
    if receipt.get("state") != "assessed":
        return False
    expected = "conflicting" if verdict == RumorVerdict.MIXED else verdict.value
    verification = receipt.get("verification")
    if not isinstance(verification, list):
        return False
    return any(
        isinstance(entry, dict) and entry.get("state") == expected and isinstance(entry.get("official_evidence"), list)
        and any(official_evidence(quote) for quote in entry["official_evidence"])
        for entry in verification
    )


def parse_rumor_verdict(answer: str) -> RumorVerdict:
    verdict = _HEADINGS.get(answer.strip().partition("\n")[0].strip())
    if verdict is None:
        raise ValueError("谣言判别首行缺少规定的结论")
    return verdict


class RumorVerdictCheck:
    def __init__(self, *, require_assessment=False):
        self.require_assessment = require_assessment

    def check(self, context: ReviewContext) -> tuple[Finding, ...]:
        try:
            verdict = parse_rumor_verdict(context.answer)
        except ValueError:
            return (Finding("output_format", RUMOR_VERDICT_RULE.instruction),)
        if verdict != RumorVerdict.INSUFFICIENT_EVIDENCE:
            if not any(_can_verify(evidence, verdict) for evidence in context.evidence):
                return (Finding("missing_evidence", "缺少对应的可靠证据时首行应写「结论为证据不足」"),)
            if self.require_assessment and not any(
                evidence.kind == "tool" and evidence.source == CAMPUS_RUMOR.name for evidence in context.evidence
            ):
                return (Finding("missing_evidence", "需要先调用校园谣言判别服务；没有回执时首行应写「结论为证据不足」"),)
        return ()


PROFILE = AgentProfile(
    AgentId.RUMOR_ASSESSMENT, "谣言判别",
    "负责核实校园传闻、显著变动、突发或不寻常情况。"
    "用同一会话确定待核实的说法，再用校园谣言防控服务器分析，并查相关官方公告和原文。"
    "核对发布机构、时间及适用对象，区分支持、反驳、混合与证据不足。",
)


class RumorAssessmentAgent(SpecialistAgent):
    skill_registry = SUBSKILL_REGISTRY.for_role(PROFILE.id)

    def __init__(self, backend: AgentBackend | None = None, *, profile: AgentProfile = PROFILE):
        if profile.id != AgentId.RUMOR_ASSESSMENT:
            raise ValueError("谣言判别配置的 ID 不匹配")
        super().__init__(profile, backend)

    async def run(self, task: AgentTask):
        reply = await super().run(task)
        if reply.status == Status.COMPLETED:
            if reply.rumor_verdict is None:
                raise ClusterError("谣言判别回复缺少结构化结论")
            if reply.rumor_verdict != RumorVerdict.INSUFFICIENT_EVIDENCE and not reply.evidence:
                raise ClusterError("判别结论缺少证据")
        return reply
