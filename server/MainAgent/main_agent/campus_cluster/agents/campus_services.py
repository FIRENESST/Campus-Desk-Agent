import json

from ...reliability.models import Evidence, ToolResult
from ..action_plan import CampusActionPlan
from ..contracts import AgentBackend, AgentId, AgentProfile, AgentReply, AgentTask, Status
from ..subskills.campus_timetable import CAMPUS_TIMETABLE
from ..subskills.minta_campus import CAMPUS_ACTION
from ..subskills.registry import SUBSKILL_REGISTRY
from .base import SpecialistAgent


PROFILE = AgentProfile(
    AgentId.CAMPUS_SERVICES, "校园服务",
    "负责校园服务咨询及办理，可查询教室、体育馆和社团，也可按分类、书名或作者查找馆藏和匿名借阅进度。"
    "用户咨询已接入业务怎么操作时，依据提供的可办理事项说明校园应用入口、操作顺序及账号需要准备的信息。"
    "查询精确的空闲时段、预约占用、场地余量、活动起止、课表、约好的个人日程或借书到期时间时，"
    "先调用 campus_timetable。相对日期按北京时间结合会话换算；空闲日期未指定时可先查今天并说明日期。"
    "按回执核对起止日期与时间、状态和余量，数据读取失败时说明无法确认。只查询时不提交任何预约或报名。"
    "借还、到期及历史记录仅限当前账号。"
    "明确办理指令由主智能体委派。回执未确认时请说明状态，不自行声称已完成。",
)


class CampusServicesAgent(SpecialistAgent):
    skill_registry = SUBSKILL_REGISTRY.for_role(PROFILE.id)

    def __init__(self, backend: AgentBackend | None = None, *, profile: AgentProfile = PROFILE):
        if profile.id != AgentId.CAMPUS_SERVICES:
            raise ValueError("校园服务配置的 ID 不匹配")
        super().__init__(profile, backend)

    async def read_timetable(self, conversation_id, query):
        task = AgentTask(conversation_id, "campus-timetable", self.profile, "读取校务时段表")
        client = self.skill_registry.bind(task, allowed_tools={CAMPUS_TIMETABLE.name}, read_only=True)
        if CAMPUS_TIMETABLE.name not in client.names:
            return ToolResult("当前校园服务暂时无法读取时段。", "error")
        return await client.call_tool(CAMPUS_TIMETABLE.name, query.model_dump())

    async def execute_action(
        self, conversation_id: str, plan: CampusActionPlan, handler,
    ) -> tuple[AgentReply, CampusActionPlan | None]:
        registry = self.skill_registry.with_handlers({CAMPUS_ACTION.name: handler})
        task = AgentTask(conversation_id, "minta-action", self.profile, "办理本轮校务")
        client = registry.bind(task, allowed_tools={CAMPUS_ACTION.name})
        if CAMPUS_ACTION.name not in client.names:
            return AgentReply(Status.NEEDS_CONFIGURATION, "校园服务尚未启用这项办理能力。"), None
        receipt = json.loads(await client.call_tool(CAMPUS_ACTION.name, {"plan": plan.model_dump()}))
        payload = receipt.get("pending_plan")
        pending = CampusActionPlan.model_validate(payload) if payload is not None else None
        evidence = ()
        if pending is not None and (name := receipt.get("target_name")):
            target = {"id": pending.target, "name": name}
            evidence = (Evidence("campus-action-target", json.dumps(target, ensure_ascii=False), "校务服务目录",
                                 status="success"),)
        reply = AgentReply(Status(receipt["status"]), receipt["message"], reason=receipt.get("reason"), evidence=evidence)
        return reply, pending
