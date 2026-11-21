"""Review proposed campus actions before handing them to a specialist."""

import asyncio
import json
from datetime import datetime
from enum import Enum

from ..campus_cluster.action_plan import CAMPUS_TIME_ZONE, CampusActionPlan
from ..campus_cluster.activity_time import campus_datetime
from ..campus_cluster.contracts import Status
from ..campus_cluster.subskills.minta_campus import name_key, redact_classroom_contact
from ..core import AgentError, Message
from ..reliability.checks import review_findings
from ..reliability.models import Evidence, ReviewContext, Rule


ACTION_INTENT_RULE = Rule(
    "campus.action_plan",
    "核对拟办动作、对象和参数是否符合用户本轮的明确委托或同一事项的补充。"
    "把礼貌地请你帮忙办理的问句理解为委托，结合上下文区分发起办理与询问规则。"
    "这是已经通过业务校验、准备展示给用户确认的计划。参数必须对应用户表达，不能采用助手举的例子。"
    "服务预览可证明目录中的编号、完整名称和简称指向同一对象。业务默认值以服务清单为准。"
    "如果本轮明确选择了前文场馆时段表中的一项，允许用随附的服务记录补齐该行的场馆、日期及起止时间。"
    "教室预约的时段以及体育馆预约的日期、场地号和时间可按北京时间规范化。核对与用户所选一致。"
    "复用前文参数时，本轮必须明确接续同一事项。历史中的委托或确认不能代替本轮的意思。"
    "用户只是咨询、转述、否认或说算了，未选择多项中的哪一项，或计划忽略了不支持的附加条件时，不通过。"
    "只在 rule_checks 中说明结论，claims 和 issues 都填空数组。",
)
ACTION_CONFIRMATION_RULE = Rule(
    "campus.action_confirmation",
    "只判断本轮用户是否明确同意按 pending 中已展示的对象和条件提交。"
    "pending.previous_reply_is_preview 为真表示上一条回复就是这笔待办的确认提示。"
    "简短肯定只有在直接回应确认提示时才足以同意。中间切过话题时，用户必须明确接续这笔待办。"
    "问句、闲聊、同意另一件事、否定、修改对象或附加条件都不能通过。此前的授权或确认也不能代替本轮。"
    "只在 rule_checks 中说明结论，claims 和 issues 都填空数组。",
)
CAMPUS_INTENT_TIMEOUT = 20.0


class ActionReview(Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


def campus_action_turns(history):
    """Yield the recorded campus workflow turns, newest first, skipping ordinary chat."""
    for item in reversed(history):
        if item["role"] == "user" and (
            item.get("campus_action_turn") or item.get("classroom_pending") or item.get("pending_campus_action") is not None
        ):
            yield item


def pending_campus_action(history) -> CampusActionPlan | None:
    latest = next(campus_action_turns(history), None)
    if latest is None or latest.get("pending_campus_action") is None:
        return None
    try:
        return CampusActionPlan.model_validate(latest["pending_campus_action"])
    except ValueError:
        return None


def last_answered_user(history):
    if len(history) >= 2 and history[-2]["role"] == "user" and history[-1]["role"] == "assistant":
        return history[-2]
    return None


def campus_action_draft(history) -> CampusActionPlan | None:
    turn = last_answered_user(history)
    if turn is None:
        return None
    value = turn.get("campus_action_draft") or turn.get("pending_campus_action")
    if value is None:
        return None
    try:
        return CampusActionPlan.model_validate(value)
    except ValueError:
        return None


def model_dialog_history(history) -> tuple[Message, ...]:
    return tuple(Message(item["role"], redact_classroom_contact(item["content"], pending=True))
                 for item in history if item["role"] in {"user", "assistant"})


def _model_plan(plan: CampusActionPlan) -> dict:
    proposal = plan.model_dump()
    if plan.action == "classroom.start" and "phone" in proposal["arguments"]:
        proposal["arguments"]["phone"] = "[电话已隐藏]"
    return proposal


def pending_confirmation_context(history, plan: CampusActionPlan | None) -> dict | None:
    if plan is None:
        return None
    turn = next(campus_action_turns(history), None)
    if turn is None:
        return None
    preview = turn.get("campus_action_reply")
    direct_reply = last_answered_user(history) is turn and (preview is None or history[-1]["content"] == preview)
    return {
        "plan": _model_plan(plan),
        "preview": redact_classroom_contact(preview, pending=True) if preview else None,
        "previous_reply_is_preview": direct_reply,
    }


def draft_target_is_grounded(context, plan, draft) -> bool:
    if not draft.target:
        return True
    names = {name for value in (plan.target, draft.target) if (name := name_key(value))}
    previous = campus_action_draft(context.history)
    if previous is not None and previous.action == draft.action and name_key(previous.target) in names:
        return True
    if draft.action == "gym.start" and matching_gym_slots(plan, recent_gym_slots(context.history)):
        return True
    messages = [context.query, *(item["content"] for item in context.history if item["role"] == "user")]
    return any(name in name_key(message) for message in messages for name in names)


def recent_gym_slots(history) -> tuple[dict[str, str], ...]:
    """Read the last displayed timetable, also allowing a follow-up to a clarification."""
    for item in reversed(history):
        if item["role"] != "assistant":
            continue
        tables = item.get("gym_timetables")
        if tables:
            slots = {}
            for rows in tables:
                for row in rows or ():
                    if row.get("kind") != "gym":
                        continue
                    start, end = (campus_datetime(row[key]).astimezone(CAMPUS_TIME_ZONE)
                                  for key in ("starts_at", "ends_at"))
                    slot = {
                        "venue_id": row["resource_id"], "venue_name": row["name"],
                        "court_id": str(row["court_id"]), "court_name": row["location"],
                        "date": start.date().isoformat(), "start_time": f"{start:%H:%M}",
                        "end_time": "24:00" if start.date() < end.date() and end.hour == end.minute == 0 else f"{end:%H:%M}",
                    }
                    slots[tuple(slot[key] for key in ("venue_id", "date", "court_id", "start_time", "end_time"))] = slot
            return tuple(slots.values())
        if item.get("reply_status") != Status.NEEDS_INPUT.value:
            break
    return ()


def matching_gym_slots(plan: CampusActionPlan, slots: tuple[dict[str, str], ...]) -> tuple[dict[str, str], ...]:
    if not plan.arguments.keys() & {"court_id", "start_time", "end_time"}:
        return ()
    target = name_key(plan.target).replace(" ", "")
    return tuple(slot for slot in slots if all(slot.get(key) == value for key, value in plan.arguments.items()) and
                 (not target or target == name_key(slot["venue_id"]) or
                  target == name_key(slot["court_name"]).replace(" ", "") or
                  target in name_key(slot["venue_name"]).replace(" ", "")))


async def review_campus_action(reviewer, context, plan: CampusActionPlan, *, confirmation=None, prepared=None,
                               references=(), gym_slot=None) -> ActionReview:
    classroom = plan.action == "classroom.start"
    if confirmation is None:
        rule, proposal = ACTION_INTENT_RULE, _model_plan(plan)
    else:
        rule, proposal = ACTION_CONFIRMATION_RULE, {"decision": "confirm", "pending": confirmation}
    instruction = "请根据会话和最后一条用户消息，按所给规则审核这次校园办理意图。"
    if confirmation is None and plan.action in {"gym.start", "gym.close", "classroom.start", "classroom.close"}:
        instruction += f"今天是北京时间 {datetime.now(CAMPUS_TIME_ZONE):%Y-%m-%d}，按此日期理解本轮的相对时间。"
    evidence = []
    if gym_slot is not None:
        evidence.append(Evidence("gym-slot", json.dumps(gym_slot, ensure_ascii=False), "前文已展示的场馆时段表",
                                 status="success"))
    if prepared is not None:
        evidence.append(Evidence("campus-preparation", json.dumps(_model_plan(prepared), ensure_ascii=False),
                                 "校务服务校验后的办理参数", status="success"))
    evidence.extend(references)
    check = ReviewContext(
        messages=(Message("system", instruction),
                  *model_dialog_history(context.history),
                  Message("user", redact_classroom_contact(context.query, pending=classroom))),
        rules=(rule,), evidence=tuple(evidence),
        answer=json.dumps(proposal, ensure_ascii=False),
    )
    try:
        async with asyncio.timeout(CAMPUS_INTENT_TIMEOUT):
            findings = review_findings(check, await reviewer.review(check))
            return ActionReview.REJECTED if findings else ActionReview.APPROVED
    except (AgentError, TimeoutError, ValueError, RecursionError):
        return ActionReview.UNAVAILABLE
