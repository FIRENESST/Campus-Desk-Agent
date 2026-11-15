import asyncio
import json
import logging
import re
import unicodedata
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from typing import Protocol
from uuid import UUID

from ..campus_cluster import (
    AgentId, AgentProfile, ClusterRequest, LLMRouter, ReliableAgentBackend, Router, RoutingFeedback, RumorVerdict,
    SkillRegistry, Status, build_campus_cluster,
)
from ..campus_cluster.action_plan import CAMPUS_ACTION_CONFIRMATION
from ..campus_cluster.agents.rumor_assessment import (
    PROFILE as RUMOR_ASSESSMENT_PROFILE, RUMOR_VERDICT_RULE, RumorVerdictCheck, parse_rumor_verdict,
)
from ..campus_cluster.agents.campus_services import PROFILE as CAMPUS_SERVICES_PROFILE
from ..campus_cluster.subskills.campus_timetable import CAMPUS_TIMETABLE
from ..campus_cluster.capabilities import default_skill_registries, describe_capabilities, scope_actions
from ..campus_cluster.coordinator import StepActivity
from ..campus_cluster.contracts import aggregate_status
from ..campus_cluster.continuity import (
    CONVERSATION_CONTINUITY, SUPPORT_RESPONSE, recent_explicit_risk, resolved_query_message,
)
from ..campus_cluster.routing import asks_how_to_verify_notice, priority_campus_role, reports_campus_threat
from ..campus_cluster.subskills.campus_digest import CAMPUS_DIGEST
from ..campus_cluster.subskills.minta_campus import (
    CLASSROOM_CONFIRMATION, CLASSROOM_PENDING, campus_intent,
    redact_classroom_contact,
)
from ..campus_cluster.subskills.rumor_assessment import CAMPUS_RUMOR
from ..campus_cluster.subskills.wechat_articles import WECHAT_ARTICLES
from ..core import AgentError, LLMClient, Message
from ..mcp_tools import MainAgentRAGClient, load_subagent_capabilities
from ..reliability import (
    Evidence, Finding, InvalidReviewError, LLMReviewer, ReliabilityConfig, ReliableAgent, ReviewContext, Rule,
    VerificationFailed,
)
from ..reliability.review import structured_receipt
from .campus_planning import (
    last_answered_user, pending_campus_action, pending_confirmation_context, plan_matches_message, review_campus_action,
)
from .classroom_dialog import (
    CLARIFY_CAMPUS_TURN, CONFIRM_PENDING, ClassroomTurn, classroom_draft, classroom_route_turn, plan_campus_turn,
)
from .persona import MAIN_AGENT_PERSONA
from .campus_timetable import format_timetable


CAMPUS_EVIDENCE_RULE = Rule(
    "campus.evidence",
    "依据本次提供的资料和工具回执陈述校园事实。只使用与当前问题直接相关的证据，区分已确认和待确认的内容。",
)
PUBLIC_ANSWER_RULE = Rule(
    "campus.public_answer",
    "用用户能直接使用的结果回答，保留相关事实和可靠来源。不要报告内部检索的关键词、调用次数、命中情况、"
    "工具和服务状态，也不要用无关学校概况填充。即使没有能回答问题的资料，也要结合本轮和前文，"
    "用一两句指出具体事项以及暂时无法确认的部分。把用户转述与已核实的事实分开。"
    "只有明确能帮助用户继续时才给一个已核实的入口或问一个关键问题。不要列待核对事项清单或泛泛的查询建议。",
)
GREETING_QUERIES = frozenset({"你好", "您好", "嗨", "hi", "hello"})
IDENTITY_QUERIES = frozenset({
    "你是谁", "请问你是谁", "你是什么助手", "你能做什么", "请问你能做什么", "介绍一下你自己",
})
ASSISTANT_INTRODUCTION = "我是中传校园助手，可以介绍校园、搜寻校园资料、协助查询和办理校园服务，也可以帮你核实校园传闻。"
OPENING_ANSWER_INSTRUCTIONS = (
    "如果用户在打招呼，用一两句自然回应。"
    "同一会话有正在讨论的事时，可以自然接上当前话题。"
    f"如果用户在问你的身份或能提供什么帮助，依据以下介绍回答。{ASSISTANT_INTRODUCTION}"
)
ABANDON_RESERVATION = frozenset({"取消预约", "取消本次预约", "放弃预约", "不预约了"})
ABANDON_CAMPUS_ACTION = frozenset({"取消办理", "放弃办理"})
MAIN_ANSWER_INSTRUCTIONS = (
    "你是中传校园助手，由你向用户给出最终回答。"
    f"{CONVERSATION_CONTINUITY}"
    "你可以判断本轮是否涉及校园。判断应严格、保守，以完成任务是否依赖校园事实或校内业务为准，"
    "结合上下文判断指代，避免单凭出现学校名称下结论。"
    "协作结果作为参考资料，以自然、简明的方式组织答案。只引用与用户问题直接相关的资料。"
    "涉及多个事项时，按提问顺序给出各项结果或指出具体缺口，保留每项对应的对象和范围。"
    "以本轮提供的可办理事项清单了解当前账号可申请的业务、所需信息和前置校验。"
    "自行判断用户是在询问、准备办理还是补充资料。已经提供的资料不重复索要，意图不清楚时只问最需要确认的一项。"
    "用户询问如何使用已接入的办理功能时，按清单说明可操作的下一步。"
    "路线问题需要明确起点和终点。用户只说校门且可能有多个入口时，先问从哪个门出发。"
    "有校园地图 walking_route 结果时，按道路、方向和距离逐段给出走法，距离写作约数，并保留终点入口提示和来源。"
    "地图只有点位或路段不连通时，答能确认的位置，不补写缺失的路线。"
    "本回答分支提供查询结果和办理说明，实际提交由已授权的校务分支执行。"
    "校园业务的实时状态以服务数据为准，办理成功以真实回执为准。"
    "有 campus_timetable 回执时，按里面的北京时间、场地、起止和余量回答，保留查阅日期。"
    "未核对、暂停预约、已占用或未发布的时段不能视作空闲，历史资料不用于推断实时可约状态。"
    "回答近期活动时，分别核对文章发布日期、活动举办时间和报名截止。明确区分已举办的报道和可参加的预告。"
    "回答公众号文章的问题时，优先依据读取到的正文。引用标题、公众号、发布时间及已有原文链接。"
    "查询公众号名称时按名录的账户名称回答。正文关键词搜索的覆盖范围以 body_coverage 为准。"
    "只取得索引信息或部分摘要时，只使用这些资料能确认的内容，指出正文中仍无法核对的细节。"
    "只问如何核实一条转发通知时，直接说明怎样按标题找原文、核对发布单位和时间、查看后续更正及联系发文部门。"
    "渠道和步骤可以作为核对方法说明。具体联系方式、已发布的通知及消息真伪仍需证据。"
    "当前资料只支持近期报道时，列出一两条相关报道，说明是否能确认有即将举行或开放报名的活动。"
    "涉及校园事实时只陈述有资料支持的要点，"
    "保留必要的来源和不确定性。只问学校概况且所给的官网简介已经足够时，直接用这份简介作答。"
    "遇到不确定的校园事实或现有校园资料不足时，"
    "先调用 search_rag 检索校园资料库，再根据查到的证据作答。"
    "有整理出的查询目标时，用对话核对其中的对象和范围，再用完整目标组织检索参数。"
    "用户要的是名单、公示或结果时，围绕该目标查询。评审办法只能辅助解释流程或定位来源。"
    "对数学、翻译、写作或可从用户给出的内容直接推导的部分，可以直接作答。"
    "资料不足时，先答能确认的部分，其余只说明具体无法确认的事项。"
)
CAMPUS_CONTEXT_REQUIRED = "[CAMPUS_CONTEXT_REQUIRED]"
GENERAL_ANSWER_INSTRUCTIONS = (
    "你是中传校园助手。结合本轮请求和当前会话，直接回答不依赖校园事实或校内业务的问题。"
    f"{CONVERSATION_CONTINUITY}"
    "计算时先核对结果。使用用户要求的语言和格式，直接给出有用的内容。"
    "只问转发通知的来源核对方法时，说明怎样按标题找原文、核对发布单位和时间、查看后续更正及联系发文部门。"
    "只有用户要你核实一条具体消息或查某个部门的联系方式时，才需要按校园事实处理。"
    "遇到室友排斥、人际矛盾或情绪困扰时，先回应对方的感受，再结合已知情况给出可尝试的下一步。"
    "可以建议向信任的人、辅导员或宿舍管理人员求助。只有当问题需要具体的校内事实或要办理校内业务时才转入校园核实。"
    "涉及尚待处置的紧急人身安全风险时，先给出安全行动，不能用需要校园资料的标记代替安全响应。"
    f"严格、保守地判断任务范围。若仍需核实具体校园事实或办理校园业务，只输出内部标记 {CAMPUS_CONTEXT_REQUIRED}。"
    "对于无法确认的实时信息，说明不确定的部分，不声称查过或执行过外部操作。"
)
ANSWER_UNAVAILABLE = "这次未能完成回答，请稍后重试。"
GENERIC_EVIDENCE_ANSWERS = frozenset({
    "这项校园信息暂时无法确认", "这条校园消息暂未核实，请以校方正式通知为准", "现有证据不足，暂时无法给出可靠结论",
})
CHANGE_REVIEW_INSTRUCTIONS = (
    "本轮已委派谣言判别。依据核验结论和可追溯的最新证据回答。"
    "只给出能对齐日期、校区和适用人群的事实及官方来源链接。"
    "历史规定只用于解释原有安排，候选分类和复核优先级仅供内部使用。"
    "证据缺失或不一致时，复述待核主张及用户已补充的范围，再明确尚未证实的环节。"
    "只问能够推进核验的关键线索，或给出一个与这条消息相关的下一步。"
)
SUPPORT_FALLBACK = (
    "听起来你现在承受了很多。我想先确认，你现在安全吗？"
    "如果你担心自己马上会受伤，请联系当地急救，也请一位信任的人现在过来陪你。"
)
CAMPUS_THREAT_FALLBACK = (
    "你提到的校园安全威胁尚未证实。如果你在相关地点附近，先远离现场，到安全处拨打 110，并通知学校保卫处。"
    "如果你收到过原消息，把它和收到的时间交给警方。"
)
CAMPUS_THREAT_RESPONSE = (
    "用户报告了现实中的具体校园安全威胁，当前没有可靠证据判断消息真伪。"
    "根据本轮和前文，先回应用户刚提供的时间、地点、消息来源或安全状态。"
    "若尚未确认安全，建议远离相关地点，到安全处拨打 110 并通知学校保卫处。"
    "如果收到过原消息，可以把原消息和收到的时间交给警方。"
    "用户已经到安全处或已经报告时，接着说明当前应做什么，避免重复建议或询问已知信息。"
    "不要让用户前往现场核实、接触可疑物品或转发未经证实的消息。"
    "不要声称威胁已证实、已经代为报警或已通知校方。"
    "只有用户的当前安全还不明确时，才问一个简短的安全问题。"
)
INTERNAL_SEARCH_REPORT = re.compile(
    r"search_rag|RAG\s*(?:检索|搜索|工具|返回|命中)|检索(?:关键词|次数|命中数)|相似度分数|"
    r"(?:检索|搜索)[^。！？\n]{0,12}结果\s*(?:均|都|全部)?\s*(?:为空|是空的)|"
    r"(?:资料库|知识库)(?:中|里)?\s*(?:没有查到|未查到|没有收录|未收录|仍无收录|暂时无法检索)|"
    r"(?:按|用)[^。！？\n]{0,120}关键词[^。！？\n]{0,30}(?:检索|搜索)",
    re.IGNORECASE,
)

@dataclass(frozen=True)
class ExecutionContext:
    user_id: UUID
    instance_id: UUID
    conversation_id: UUID
    run_id: UUID
    query: str
    targets: tuple[str, ...]
    history: tuple[dict, ...]
    worker_id: UUID | None = None
    attempt: int = 1
    previous_answer: str | None = None
    previous_result: str | None = None
    previous_campus_effect: bool = False
    step_activity: StepActivity | None = None


@dataclass(frozen=True)
class RunOutput:
    content: str
    result_status: str
    steps: tuple[dict, ...] = ()
    artifacts: tuple[dict, ...] = ()

    def validate(self):
        if not isinstance(self.content, str) or len(self.content) > 100000:
            raise ValueError("运行输出超过限制")
        if self.result_status not in {status.value for status in Status}:
            raise ValueError("运行结果状态无效")
        encoded = json.dumps(asdict(self), ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 4 * 1024 * 1024:
            raise ValueError("运行产物超过限制")


class Runner(Protocol):
    async def run(self, context: ExecutionContext) -> RunOutput:
        ...


class CampusPublicAnswerCheck:
    def check(self, context: ReviewContext) -> tuple[Finding, ...]:
        answer = context.answer.strip().rstrip("。！.!").strip()
        if answer in GENERIC_EVIDENCE_ANSWERS:
            return (Finding("missing_conversation_context", PUBLIC_ANSWER_RULE.instruction, PUBLIC_ANSWER_RULE.id),)
        if any(item.source == "search_rag" for item in context.evidence) and INTERNAL_SEARCH_REPORT.search(context.answer):
            return (Finding("internal_search_report", PUBLIC_ANSWER_RULE.instruction, PUBLIC_ANSWER_RULE.id),)
        return ()


class CampusRunner:
    """Each run receives a fresh cluster; user context is a server-owned value."""

    def __init__(
        self, cluster_factory=None, *, routing_feedback: RoutingFeedback | None = None,
        answerer: ReliableAgent | None = None, campus_chat=None,
    ):
        self.cluster_factory = cluster_factory or (lambda context: build_campus_cluster())
        self.routing_feedback = routing_feedback
        self.answerer = answerer
        self.campus_chat = campus_chat
        self.action_reviewer = (LLMReviewer(answerer.llm, max_format_retries=0)
                                if answerer is not None and campus_chat is not None else None)

    @classmethod
    def with_model(
        cls, llm: LLMClient, *,
        skills_factory: Callable[[ExecutionContext], SkillRegistry] | None = None,
        router_factory: Callable[[ExecutionContext], Router] | None = None,
        routing_feedback: RoutingFeedback | None = None,
        campus_chat=None,
    ) -> "CampusRunner":
        default_registries = default_skill_registries()

        def cluster_factory(context: ExecutionContext):
            if skills_factory is None:
                registries = dict(default_registries)
            else:
                registry = skills_factory(context)
                if not isinstance(registry, SkillRegistry):
                    raise ValueError("skills_factory 必须返回 SkillRegistry")
                registries = {role: registry.for_role(role) for role in AgentId}
            if campus_chat is not None:
                handlers = campus_chat.read_handlers(context)
                registries = {role: registry.with_handlers(handlers) for role, registry in registries.items()}
            campus_tools = {
                skill["name"] for skill in registries[AgentId.CAMPUS_SERVICES].describe(CAMPUS_SERVICES_PROFILE)
                if skill["enabled"] and skill["read_only"]
            }
            reviewer = LLMReviewer(llm)
            backends = {
                role: ReliableAgentBackend.from_skills(
                    llm, reviewer, skills=registries[role], rules=(CAMPUS_EVIDENCE_RULE,),
                    allowed_tools=campus_tools if role == AgentId.CAMPUS_SERVICES else None,
                )
                for role in AgentId if role != AgentId.RUMOR_ASSESSMENT
            }
            rumor_skills = registries[AgentId.RUMOR_ASSESSMENT]
            has_assessment = any(
                skill["name"] == CAMPUS_RUMOR.name and skill["enabled"]
                for skill in rumor_skills.describe(RUMOR_ASSESSMENT_PROFILE)
            )
            backends[AgentId.RUMOR_ASSESSMENT] = ReliableAgentBackend.from_skills(
                llm, reviewer, skills=rumor_skills,
                rules=(CAMPUS_EVIDENCE_RULE, RUMOR_VERDICT_RULE),
                output_checks=(RumorVerdictCheck(require_assessment=has_assessment),),
                rumor_verdict_parser=parse_rumor_verdict,
            )

            catalog = None

            async def capabilities(profiles: tuple[AgentProfile, ...]) -> list[dict]:
                nonlocal catalog
                if catalog is None:
                    registered = await load_subagent_capabilities(describe_capabilities(profiles, registries))
                    catalog = (await campus_chat.discover(context, registered) if campus_chat is not None
                               else scope_actions(registered, None, {}))
                return catalog

            routing = router_factory(context) if router_factory is not None else LLMRouter(
                llm, feedback=routing_feedback, allow_direct=True,
                capabilities=capabilities, allow_action_plans=campus_chat is not None,
            )
            cluster = build_campus_cluster(backends=backends, router=routing)
            cluster.registry.get(AgentId.CAMPUS_SERVICES).skill_registry = registries[AgentId.CAMPUS_SERVICES]
            return cluster

        answerer = ReliableAgent(
            llm, MainAgentRAGClient(), LLMReviewer(llm), rules=(CAMPUS_EVIDENCE_RULE, PUBLIC_ANSWER_RULE),
            output_checks=(CampusPublicAnswerCheck(),),
            config=ReliabilityConfig(max_tool_rounds=2, max_tool_calls=4, max_tool_calls_per_batch=2),
        )
        return cls(cluster_factory, routing_feedback=routing_feedback, answerer=answerer, campus_chat=campus_chat)

    async def run(self, context):
        if self.campus_chat is not None and context.previous_campus_effect:
            return self._previous_action_output(context)
        history, classroom_pending = [], False
        for item in context.history:
            content = item["content"]
            if self.campus_chat and item["role"] == "user":
                content = redact_classroom_contact(content, pending=classroom_pending)
                if item.get("campus_action_turn") or item.get("classroom_pending") or item.get("pending_campus_action"):
                    classroom_pending = bool(item.get("classroom_pending"))
            history.append(Message(item["role"], content))
        request = ClusterRequest(
            conversation_id=str(context.conversation_id),
            query=redact_classroom_contact(context.query, pending=classroom_pending) if self.campus_chat else context.query,
            targets=tuple(AgentId(target) for target in context.targets),
            history=tuple(history),
        )
        if reports_campus_threat(request.query):
            content, status = CAMPUS_THREAT_FALLBACK, Status.COMPLETED.value
            if self.answerer is not None:
                content, status = await self._answer_general(
                    request, support=True, campus_threat=True, fresh_threat=True,
                )
            output = RunOutput(content, status)
            output.validate()
            return output
        if self.answerer is not None and recent_explicit_risk(request.query, request.history):
            content, status = await self._answer_general(request, support=True)
            output = RunOutput(content, status)
            output.validate()
            return output

        if self.campus_chat is not None and (
            self.answerer is None or (priority_campus_role(request.query) is None and not asks_how_to_verify_notice(request.query))
        ):
            command = unicodedata.normalize("NFKC", context.query).strip().rstrip("。!").strip()
            model = self.answerer.llm if self.answerer is not None else None
            pending = pending_campus_action(context.history)
            draft = classroom_draft(context.history)
            confirmation_plan = draft.plan if draft is not None and draft.ready else pending
            confirmation = pending_confirmation_context(context.history, confirmation_plan)
            if command == "确认":
                if draft is not None and not draft.ready:
                    command = "确认预约"
                elif model is None and confirmation and confirmation["previous_reply_is_preview"]:
                    command = "确认预约" if draft is not None else "确认办理"
                elif confirmation is None and (last_answered_user(context.history) or {}).get("campus_action_turn"):
                    command = "确认办理"
            if command == "确认办理":
                if pending is None:
                    if draft is not None and draft.ready:
                        return RunOutput("教室预约仍待确认。请回复「确认预约」。", Status.NEEDS_INPUT.value)
                    return RunOutput("当前会话没有待确认的校务办理。请先发起办理。", Status.NEEDS_INPUT.value)
                return await self._run_action(context, self.cluster_factory(context), pending)
            if command == "确认预约":
                if draft is not None and draft.ready:
                    return await self._run_action(context, self.cluster_factory(context), draft.plan)
                if draft is not None:
                    return RunOutput("教室预约仍在补资料，请接着填写刚才提示的信息。", Status.NEEDS_INPUT.value)
                if pending is not None and pending.action == "gym.start":
                    return await self._run_action(context, self.cluster_factory(context), pending)
                return RunOutput("当前会话没有待确认的教室预约。", Status.NEEDS_INPUT.value)
            reservation_pending = draft is not None or (pending is not None and pending.action == "gym.start")
            if command in ABANDON_CAMPUS_ACTION or (reservation_pending and command in ABANDON_RESERVATION):
                if draft is None and pending is None:
                    return RunOutput("当前会话没有待放弃的校务办理。", Status.NEEDS_INPUT.value)
                return self._action_output("已放弃这项待办理事项。", Status.COMPLETED.value)
            intent = campus_intent(context.query)
            turn = await plan_campus_turn(context.query, context.history, model, draft, confirmation, intent)
            if turn == CONFIRM_PENDING and confirmation_plan is not None and confirmation is not None:
                if self.action_reviewer is not None and await review_campus_action(
                    self.action_reviewer, context, confirmation_plan, confirmation=confirmation,
                ):
                    return await self._run_action(context, self.cluster_factory(context), confirmation_plan)
                return self._confirmation_hint(confirmation_plan)
            if turn == CLARIFY_CAMPUS_TURN:
                if confirmation_plan is not None:
                    return self._confirmation_hint(confirmation_plan, clarify=True)
                return RunOutput("你是在补充刚才的教室预约，还是想问一件新事？", Status.NEEDS_INPUT.value)
            if isinstance(turn, ClassroomTurn):
                cluster = self.cluster_factory(context)
                if isinstance(cluster.router, LLMRouter):
                    try:
                        async with asyncio.timeout(cluster.config.step_timeout):
                            plan = await cluster.router.check_compound(request, cluster.registry.profiles)
                    except (AgentError, TimeoutError):
                        plan = None
                    if plan is not None:
                        result = await cluster.run(request, prepared_plan=plan)
                        if result.plan.parts:
                            return await self._run_compound(context, request, cluster, result.plan.parts)
                        return await self._finish_plan(context, request, cluster, result)
                return await self._run_classroom(context, cluster, turn)
            if command == "确认" and confirmation_plan is not None:
                return self._confirmation_hint(confirmation_plan)
            if intent is not None and model is None:
                return await self._run_action(context, self.cluster_factory(context), intent.to_plan())
        if self.answerer is not None:
            prompt = request.query.strip().rstrip("。！？!? .").casefold()
            greeting = prompt in GREETING_QUERIES
            if greeting or prompt in IDENTITY_QUERIES:
                content, status = await self._answer_general(request, opening=True)
                if status != Status.COMPLETED.value or CAMPUS_CONTEXT_REQUIRED in content:
                    content = ("你好！" if greeting else "") + ASSISTANT_INTRODUCTION
                output = RunOutput(content, Status.COMPLETED.value)
                output.validate()
                return output

        cluster = self.cluster_factory(context)
        feedback = self.routing_feedback
        epoch = await feedback.recording_epoch() if feedback is not None else None
        calls: list[AgentId] = []
        on_dispatch = calls.append if epoch is not None else None
        result, general_reply = None, None
        try:
            result = await cluster.run(request, on_dispatch=on_dispatch, step_activity=context.step_activity)
            if result.plan.parts:
                return await self._run_compound(context, request, cluster, result.plan.parts, on_dispatch=on_dispatch)
            if self.answerer is not None and result.plan.general:
                general_reply = await self._answer_general(
                    request, support=result.plan.support, campus_threat=result.plan.campus_threat,
                )
                if (
                    CAMPUS_CONTEXT_REQUIRED in general_reply[0] and isinstance(cluster.router, LLMRouter)
                    and cluster.router.capabilities is not None
                ):
                    result = await cluster.run(
                        replace(request, campus_required=True), on_dispatch=on_dispatch, step_activity=context.step_activity,
                    )
                    if result.plan.parts:
                        return await self._run_compound(context, request, cluster, result.plan.parts, on_dispatch=on_dispatch)
                    general_reply = None
        except (AgentError, TimeoutError):
            if self.answerer is None:
                raise
            artifacts = []
            content, status = await self._answer(request, (), Status.FAILED.value, artifacts)
            output = RunOutput(content, status, artifacts=tuple(artifacts))
            output.validate()
            return output
        finally:
            if epoch is not None and (calls or (result is not None and result.plan.campus_action is None
                                               and not result.plan.support)):
                answered_direct = result is not None and (
                    result.plan.general or (bool(result.plan.parts) and all(part.plan.general for part in result.plan.parts))
                )
                await feedback.record(epoch, request.query, tuple(dict.fromkeys(calls)), explicit=bool(request.targets),
                                      answered_direct=answered_direct)
        return await self._finish_plan(context, request, cluster, result, general_reply=general_reply)

    async def _finish_plan(self, context, request, cluster, result, *, general_reply=None, timetable_step="campus-timetable"):
        if timetable := result.plan.timetable_query:
            return await self._run_timetable(context, cluster, timetable, step_id=timetable_step)
        if proposal := result.plan.campus_action:
            if self.campus_chat is None:
                return RunOutput("校园服务尚未启用这项办理能力。", Status.NEEDS_CONFIGURATION.value)
            if proposal.action == "classroom.start":
                classroom = classroom_route_turn(context.query, proposal)
                if classroom is not None:
                    return await self._run_classroom(context, cluster, classroom)
            elif plan_matches_message(proposal, context.query, context.history):
                return await self._run_action(context, cluster, proposal, confirm_first=True, review=True)
            return RunOutput("请补充要办理的具体对象和所需信息。", Status.NEEDS_INPUT.value)
        profiles = {profile.id: profile.name for profile in cluster.registry.profiles}
        sections, references, steps, artifacts = [], list(request.evidence), [], []
        for item in result.results:
            if self.answerer is None and item.reply.content:
                sections.append(profiles[item.agent_id] + "\n" + item.reply.content)
            if item.reply.status == Status.COMPLETED and item.reply.rumor_verdict != RumorVerdict.INSUFFICIENT_EVIDENCE:
                expiries = [value.expires_at for value in item.reply.evidence if value.expires_at is not None]
                references.append(Evidence(
                    f"specialist:{item.step_id}", item.reply.content, profiles[item.agent_id],
                    expires_at=min(expiries, default=None),
                ))
            elif item.reply.reason == "tool_limit_reached":
                references.extend(
                    Evidence(f"specialist:{item.step_id}:{index}", evidence.content, evidence.source,
                             expires_at=evidence.expires_at)
                    for index, evidence in enumerate(item.reply.evidence, 1)
                )
            if item.agent_id == AgentId.RUMOR_ASSESSMENT and item.reply.rumor_verdict in {
                RumorVerdict.SUPPORTED, RumorVerdict.REFUTED, RumorVerdict.MIXED,
            }:
                references.extend(
                    replace(value, id=f"specialist:{item.step_id}:source:{index}")
                    for index, value in enumerate(item.reply.evidence, 1)
                    if value.source in {CAMPUS_RUMOR.name, CAMPUS_DIGEST.name, WECHAT_ARTICLES.name} and value.status == "success"
                )
            if item.agent_id == AgentId.CAMPUS_INTRODUCTION and item.reply.reason != "tool_limit_reached":
                snapshot = item.snapshot or item.reply.snapshot
                evidence_items = item.reply.evidence or (snapshot.evidence if snapshot else ())
                walking_receipts = {
                    evidence.content: evidence for evidence in evidence_items
                    if evidence.kind == "tool" and evidence.source == "campus_street_view" and evidence.status == "success"
                    and isinstance(receipt := structured_receipt(evidence.content), dict)
                    and receipt.get("status") == "walking_route"
                }
                references.extend(
                    Evidence(f"specialist:{item.step_id}:tool:{index}", evidence.content, evidence.source,
                             expires_at=evidence.expires_at)
                    for index, evidence in enumerate(walking_receipts.values(), 1)
                )
            elif item.agent_id == AgentId.INFORMATION_SEARCH:
                snapshot = item.snapshot or item.reply.snapshot
                evidence_items = item.reply.evidence or (snapshot.evidence if snapshot else ())
                seen = set()
                for index, evidence in enumerate(evidence_items, 1):
                    key = evidence.source, evidence.content
                    if evidence.kind != "tool" or evidence.status != "success" or key in seen:
                        continue
                    evidence_id = f"specialist:{item.step_id}:{index}"
                    if evidence.source == WECHAT_ARTICLES.name:
                        receipt = structured_receipt(evidence.content)
                        if isinstance(receipt, dict) and (
                            receipt.get("items") or receipt.get("content") or "accounts" in receipt or "body_coverage" in receipt
                        ):
                            references.append(replace(evidence, id=evidence_id))
                    elif evidence.source == CAMPUS_DIGEST.name and item.reply.status != Status.COMPLETED:
                        references.append(Evidence(evidence_id, evidence.content, evidence.source, expires_at=evidence.expires_at))
                    seen.add(key)
            elif item.agent_id == AgentId.CAMPUS_SERVICES:
                snapshot = item.snapshot or item.reply.snapshot
                evidence_items = item.reply.evidence or (snapshot.evidence if snapshot else ())
                references.extend(replace(evidence, id=f"specialist:{item.step_id}:timetable:{index}")
                                  for index, evidence in enumerate(evidence_items, 1)
                                  if evidence.source == CAMPUS_TIMETABLE.name and evidence.status == "success")
            steps.append({"step_id": item.step_id, "agent_id": item.agent_id.value,
                          "status": item.reply.status.value,
                          "result": {"content": item.reply.content, "reason": item.reply.reason,
                                     "rumor_verdict": item.reply.rumor_verdict.value if item.reply.rumor_verdict else None}})
            if item.reply.evidence:
                artifacts.append({"kind": "evidence", "payload": {"step_id": item.step_id,
                                  "items": [asdict(value) for value in item.reply.evidence]}})
            snapshot = item.snapshot or item.reply.snapshot
            if snapshot:
                artifacts.append({"kind": "snapshot", "payload": {"step_id": item.step_id, "snapshot": asdict(snapshot)}})
        route = asdict(result.plan)
        route.pop("campus_action")
        route.pop("support")
        route.pop("campus_threat")
        route.pop("service_guide")
        route.pop("resolved_query")
        route.pop("timetable_query")
        route.pop("parts")
        artifacts.append({"kind": "route_plan", "payload": route})
        status = result.status.value
        if status == Status.FAILED.value and any(
            item.source in {CAMPUS_DIGEST.name, WECHAT_ARTICLES.name, "campus_street_view", CAMPUS_TIMETABLE.name}
            for item in references
        ):
            status = Status.INSUFFICIENT_EVIDENCE.value
        change_review = any(item.agent_id == AgentId.RUMOR_ASSESSMENT for item in result.results)
        change_unconfirmed = change_review and len(result.results) == 1 and (
            result.results[0].reply.rumor_verdict in {None, RumorVerdict.INSUFFICIENT_EVIDENCE}
        )
        if general_reply is not None:
            content, status = general_reply
            if CAMPUS_CONTEXT_REQUIRED in content:
                content, status = await self._answer(request, (), status, artifacts, cluster)
        elif self.answerer is not None and (
            change_review or result.plan.direct or references or result.plan.service_guide is not None
            or result.status in {Status.INSUFFICIENT_EVIDENCE, Status.FAILED}
        ):
            if change_unconfirmed and status == Status.COMPLETED.value:
                status = Status.INSUFFICIENT_EVIDENCE.value
            content, status = await self._answer(
                request, tuple(references), status, artifacts, cluster,
                change_review=change_review, service_guide=result.plan.service_guide,
                resolved_query=result.plan.resolved_query,
            )
        else:
            content = "\n\n".join(sections) or result.clarification or self._fallback(result)
        output = RunOutput(content, status, tuple(steps), tuple(artifacts))
        output.validate()
        return output

    async def _run_compound(self, context, request, cluster, parts, *, on_dispatch=None):
        tasks = {}
        capacity = asyncio.Semaphore(cluster.config.max_parallel)
        actions = {index for index, part in enumerate(parts, 1) if part.plan.campus_action is not None}
        pending_selection = actions if len(actions) > 1 else set()
        try:
            async with asyncio.timeout(cluster.config.total_timeout), asyncio.TaskGroup() as group:
                for index, part in enumerate(parts, 1):
                    tasks[index] = group.create_task(self._run_question_part(
                        context, request, cluster, index, part, tasks, capacity,
                        on_dispatch=on_dispatch, pending_selection=index in pending_selection,
                    ))
        except TimeoutError:
            pass
        outputs = {
            index: (
                RunOutput("这部分查询超时，请稍后重试。", Status.FAILED.value) if task.cancelled() else task.result()
            )
            for index, task in tasks.items()
        }
        return self._combine_questions(parts, outputs, pending_selection)

    async def _run_question_part(
        self, context, request, cluster, index, part, tasks, capacity, *, on_dispatch, pending_selection,
    ):
        if pending_selection:
            return RunOutput("", Status.NEEDS_INPUT.value)
        dependencies = {number: await tasks[number] for number in part.depends_on}
        incomplete = [number for number, output in dependencies.items() if output.result_status != Status.COMPLETED.value]
        if incomplete:
            numbers = "、".join(str(number) for number in incomplete)
            return RunOutput(f"这项需要等第 {numbers} 项核实后才能继续。", Status.BLOCKED.value)
        upstream = tuple(Evidence(f"question:{number}", output.content, f"第 {number} 项的回答")
                         for number, output in dependencies.items())
        question = redact_classroom_contact(part.query) if self.campus_chat else part.query
        scoped_request = replace(request, query=question, evidence=(*request.evidence, *upstream))
        plan = part.plan
        step_ids = {step.id: f"part-{index}-step-{number}" for number, step in enumerate(plan.steps, 1)}
        plan = replace(plan, steps=tuple(
            replace(step, id=step_ids[step.id], depends_on=tuple(step_ids[target] for target in step.depends_on))
            for step in plan.steps
        ))
        try:
            result = await cluster.run(scoped_request, on_dispatch=on_dispatch, step_activity=context.step_activity,
                                       prepared_plan=plan, step_capacity=capacity)
            async with capacity, asyncio.timeout(cluster.config.step_timeout):
                general = (await self._answer_general(scoped_request)
                           if self.answerer is not None and plan.general and not upstream else None)
                return await self._finish_plan(context, scoped_request, cluster, result, general_reply=general,
                                               timetable_step=f"part-{index}-timetable")
        except TimeoutError:
            return RunOutput("这部分查询超时，请稍后重试。", Status.FAILED.value)
        except Exception as exc:
            logging.getLogger(__name__).warning("Compound question part %s failed (%s)", index, type(exc).__name__)
            return RunOutput("这部分暂时无法完成，请稍后重试。", Status.FAILED.value)

    @staticmethod
    def _combine_questions(parts, outputs, pending_selection):
        labels = {}
        for index, part in enumerate(parts, 1):
            label = " ".join(redact_classroom_contact(part.query).split())
            label = label[:179] + "…" if len(label) > 180 else label
            labels[index] = re.sub(r"([\\`*_\[\]<>])", r"\\\1", label)
        truncated = sum(len(output.content) for output in outputs.values()) > 90_000
        section_limit = 90_000 // len(parts) if truncated else 90_000
        sections, steps = [], []
        artifacts = [{"kind": "route_plan", "payload": {
            "parts": [
                {"query": redact_classroom_contact(part.query), "depends_on": part.depends_on,
                 "status": outputs[index].result_status}
                for index, part in enumerate(parts, 1)
            ],
        }}]
        for index in labels:
            output = outputs[index]
            if index not in pending_selection:
                body = output.content.strip() or "这部分暂时无法确认。"
                if len(body) > section_limit:
                    body = body[:section_limit - 1] + "…"
                sections.append(f"**{index}. {labels[index]}**\n\n{body}")
            steps.extend(output.steps)
            artifacts.extend(
                {**artifact, "payload": {**artifact["payload"], "question": index}}
                for artifact in output.artifacts if artifact["kind"] != "route_plan"
            )
        if pending_selection:
            choices = "、".join(f"第 {index} 项「{labels[index]}」" for index in sorted(pending_selection))
            sections.append(f"请先选择一项办理：{choices}。")
        content = "\n\n".join(sections)
        for index, step in enumerate(steps):
            if step["step_id"] == "minta-action" and step["status"] == Status.NEEDS_INPUT.value:
                steps[index] = {**step, "result": {**step["result"], "compound_reply": content}}
        status = aggregate_status({Status(output.result_status) for output in outputs.values()})
        if truncated and status == Status.COMPLETED:
            status = Status.PARTIAL
        result = RunOutput(content, status.value, tuple(steps), tuple(artifacts))
        try:
            result.validate()
        except ValueError:
            # Multiple independent runs can exceed the shared audit budget with full model snapshots.
            result = replace(result, artifacts=tuple(
                artifact for artifact in artifacts if not artifact["kind"].endswith("snapshot")
            ))
            try:
                result.validate()
            except ValueError:
                result = replace(result, artifacts=(artifacts[0],))
                result.validate()
        return result

    async def _run_timetable(self, context, cluster, query, *, step_id="campus-timetable"):
        agent = cluster.registry.get(AgentId.CAMPUS_SERVICES)
        activity = context.step_activity(step_id, agent.profile.id) if context.step_activity else nullcontext()
        receipt = None
        async with activity:
            try:
                async with asyncio.timeout(30):
                    reply = await agent.read_timetable(str(context.conversation_id), query)
                if getattr(reply, "status", None) == "success":
                    table = json.loads(reply.content)
                    content = format_timetable(table, available_only=query.available_only)
                    receipt = table
                else:
                    content = getattr(reply, "content", "暂时无法读取当前的校务时段。")
            except (ValueError, KeyError, TypeError, TimeoutError):
                content = "暂时无法读取当前的校务时段，请稍后再试。"
        status = Status.INSUFFICIENT_EVIDENCE if receipt is None else (
            Status.COMPLETED if receipt["verified"] else Status.PARTIAL
        )
        step = {"step_id": step_id, "agent_id": agent.profile.id.value, "status": status.value,
                "result": {"content": content, "tool": CAMPUS_TIMETABLE.name}}
        artifacts = ({"kind": "timetable", "payload": receipt},) if receipt is not None else ()
        output = RunOutput(content, status.value, (step,), artifacts)
        output.validate()
        return output

    async def _run_classroom(self, context, cluster, classroom):
        # Preparing a draft only collects fields or previews the reservation. Submission is a separate turn.
        return await self._run_action(
            context, cluster, classroom.plan, confirm_first=classroom.confirm_first,
            classroom_source=classroom.source,
        )

    async def _run_action(self, context, cluster, plan, *, confirm_first=False, review=False, classroom_source=None):
        if context.attempt > 1 and not confirm_first and plan.target and plan.action != "classroom.start":
            return self._previous_action_output(context)
        if review and (self.action_reviewer is None or not await review_campus_action(self.action_reviewer, context, plan)):
            return RunOutput("请确认你想办理的事项，并写明具体对象。", Status.NEEDS_INPUT.value)
        agent = cluster.registry.get(AgentId.CAMPUS_SERVICES)
        activity = context.step_activity("minta-action", agent.profile.id) if context.step_activity else nullcontext()
        async with activity:
            reply, pending = await self.campus_chat.run_action(context, plan, agent, confirm_first=confirm_first)
        result = {"content": reply.content}
        if reply.reason in {CLASSROOM_PENDING, CLASSROOM_CONFIRMATION}:
            result[CLASSROOM_PENDING] = True
            if reply.reason == CLASSROOM_CONFIRMATION:
                result[CLASSROOM_CONFIRMATION] = True
                if pending is not None:
                    result["classroom_target"] = pending.target
            if classroom_source is not None:
                result["classroom_source"] = classroom_source
        elif reply.reason == CAMPUS_ACTION_CONFIRMATION and pending is not None:
            result["pending_campus_action"] = pending.model_dump()
        return self._action_output(reply.content, reply.status.value, result)

    @staticmethod
    def _confirmation_hint(plan, *, clarify=False):
        classroom = plan.action == "classroom.start"
        command = "确认预约" if classroom else "确认办理"
        subject = "教室预约" if classroom else "校务办理"
        content = f"要提交刚才的{subject}，请回复「{command}」。"
        if clarify:
            content = f"你是在补充刚才的{subject}，还是想问一件新事？{content}"
        return RunOutput(content, Status.NEEDS_INPUT.value)

    def _previous_action_output(self, context):
        if not context.previous_answer:
            return self._action_output("这轮的校务回执暂未确认。可在校园应用核对当前状态。", Status.NEEDS_INPUT.value)
        result = context.previous_result if context.previous_result in {value.value for value in Status} else Status.PARTIAL.value
        return self._action_output(context.previous_answer, result)

    @staticmethod
    def _action_output(content, status, result=None):
        step = {"step_id": "minta-action", "agent_id": AgentId.CAMPUS_SERVICES.value, "status": status,
                "result": result if result is not None else {"content": content}}
        output = RunOutput(content, status, (step,))
        output.validate()
        return output

    async def _answer_general(
        self, request, *, opening: bool = False, support: bool = False, campus_threat: bool = False,
        fresh_threat: bool = False,
    ):
        messages = [Message("system", GENERAL_ANSWER_INSTRUCTIONS), Message("system", MAIN_AGENT_PERSONA)]
        timeout = self.answerer.config.llm_timeout
        fallback = (SUPPORT_FALLBACK, Status.COMPLETED.value) if support else (ANSWER_UNAVAILABLE, Status.FAILED.value)
        if support:
            messages.append(Message("system", CAMPUS_THREAT_RESPONSE if campus_threat else SUPPORT_RESPONSE))
            timeout = min(timeout, 10.0)
        if campus_threat:
            fallback = CAMPUS_THREAT_FALLBACK, Status.COMPLETED.value
        if opening:
            messages.append(Message("system", OPENING_ANSWER_INSTRUCTIONS))
            timeout = min(timeout, 10.0)
        messages.extend(item for item in request.history if item.role in {"user", "assistant"})
        messages.append(Message("user", request.query))
        try:
            async with asyncio.timeout(timeout):
                reply = await self.answerer.llm.complete(messages, [])
        except (AgentError, TimeoutError):
            return fallback
        if (
            not isinstance(reply, Message) or reply.role != "assistant" or reply.tool_calls
            or not isinstance(reply.content, str) or not reply.content.strip()
        ):
            return fallback
        if support and CAMPUS_CONTEXT_REQUIRED in reply.content:
            return fallback
        if fresh_threat and not all(term in reply.content for term in ("110", "保卫处")):
            return fallback
        return reply.content, Status.COMPLETED.value

    async def _answer(
        self, request, references, status, artifacts, cluster=None, *, change_review=False, service_guide=None, resolved_query=None,
    ):
        messages = [Message("system", MAIN_ANSWER_INSTRUCTIONS), Message("system", MAIN_AGENT_PERSONA)]
        if change_review:
            messages.append(Message("system", CHANGE_REVIEW_INSTRUCTIONS))
        if service_guide is not None:
            references += (service_guide,)
            messages.append(Message("system", (
                "用校园服务的协作结果及本轮提供的动作资料说明现有校园服务的操作流程，"
                "说明校园应用入口、操作顺序及当前账号所需的信息。"
                "可预约时段以服务数据为准，缺少时段资料时仍可说明如何在页面查看。"
                "有时段表回执时，同时列出用户询问的日期和具体起止。代办提示由系统附在回复末尾。"
            )))
        elif (
            self.campus_chat is not None and cluster is not None
            and isinstance(cluster.router, LLMRouter) and cluster.router.capabilities
        ):
            agents = await cluster.router.capabilities(cluster.registry.profiles)
            actions = [{"agent": agent["name"], "skill": skill["name"], **action}
                       for agent in agents for skill in agent["skills"] if skill["enabled"]
                       for action in skill.get("actions", []) if "required_fields" in action]
            if actions:
                references += (Evidence("campus-actions", json.dumps(actions, ensure_ascii=False), "当前账号可办理事项"),)
        evidence_gap = status == Status.INSUFFICIENT_EVIDENCE.value
        if evidence_gap or status == Status.FAILED.value:
            messages.append(Message("system", (
                "从本轮问题和会话历史确认用户要解决的具体事项。有原始资料就先核对，再按该事项补查 search_rag。"
                "给出可确认的部分和具体缺口。"
                "若用户想办理清单中的业务，先依据当前账号的办理条件说明如何补齐信息。"
            )))
        messages.extend(item for item in request.history if item.role in {"user", "assistant"})
        if resolved_query:
            messages.append(resolved_query_message(resolved_query))
        messages.append(Message("user", request.query))
        try:
            result = await self.answerer.run(messages, references=references)
        except AgentError as exc:
            if snapshot := getattr(exc, "snapshot", None):
                artifacts.append({"kind": "main_answer_snapshot", "payload": asdict(snapshot)})
            if evidence_gap or change_review or isinstance(exc, (InvalidReviewError, VerificationFailed)):
                topic = " ".join((resolved_query or request.query).split())
                topic = topic[:160] + ("…" if len(topic) > 160 else "")
                # A literal quotation remains inert in the chat's Markdown renderer.
                fence = "`" * (1 + max((len(group) for group in re.findall(r"`+", topic)), default=0))
                return f"关于 {fence} {topic} {fence}，我暂时无法确认。", Status.INSUFFICIENT_EVIDENCE.value
            return ANSWER_UNAVAILABLE, Status.FAILED.value
        if result.snapshot is not None:
            artifacts.append({"kind": "main_answer_snapshot", "payload": asdict(result.snapshot)})
        if status == Status.FAILED.value:
            status = Status.PARTIAL.value
        if evidence_gap and not change_review and (
            any(item.source in {CAMPUS_DIGEST.name, WECHAT_ARTICLES.name}
                and isinstance(receipt := structured_receipt(item.content), dict)
                and (receipt.get("items") or receipt.get("content") or receipt.get("accounts")) for item in references)
            or any(item.source == "campus_street_view" and isinstance(receipt := structured_receipt(item.content), dict)
                   and receipt.get("status") == "walking_route" for item in references)
            or any(item.source == "search_rag" and item.status == "success" and json.loads(item.content)["hits"]
                   for item in result.evidence)
        ):
            status = Status.PARTIAL.value
        answer = result.answer
        if service_guide is not None:
            answer = answer.rstrip() + "\n\n需要的话，我也可以在这里帮你办理。"
            if status != Status.COMPLETED.value:
                status = Status.PARTIAL.value
        return answer, status

    @staticmethod
    def _fallback(result):
        if any(item.reply.reason == "review_timeout" for item in result.results):
            return "这次回答的资料核验超时，请稍后重试。"
        return {
            "needs_configuration": "所选智能体尚未配置知识和执行后端。",
            "insufficient_evidence": "现有证据不足，暂时无法给出可靠结论。",
            "failed": "任务执行失败，请稍后重试。",
        }.get(result.status.value, "本次任务未产生可交付的回答。")
