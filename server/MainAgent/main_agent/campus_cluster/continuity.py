"""Interpret the current message in the context of the same conversation."""

import json
import re

from ..core import Message


MAX_RESOLVED_QUERY_CHARS = 800


CONVERSATION_CONTINUITY = (
    "把本轮当作同一场对话。遇到短句、简称、确认或补充时，先回到这段话题中用户明确提出的任务，"
    "把本轮信息补到原任务上。用户只更正一个细节时，保留其余已明确的对象、时间和目标。"
    "助手先前列出的其他选项只有得到用户支持才成为新目标。用户明确转题时按新请求处理。"
    "目标已足够明确时直接回答或继续查询。只有仍缺少会改变处理结果的关键信息时，才问一个具体问题。"
    "历史和整理出的查询目标只提供语境，校园事实按证据核对，办理和确认仍需本轮明确授权。"
    "近期若有用户本人的明确自伤风险而当前安全仍未确认，先回应这轮补充与先前处境的联系，并简短确认当前安全。"
    "在这种语境里，考试失利或其他压力可能是用户在解释发生了什么；先接住这份压力，再跟进安全问题。"
    "用户补充仍在发生的校园危险时，把新线索与前文关联，先给出当下的安全行动，再区分用户转述和已证实的事实。"
)

SUPPORT_RESPONSE = (
    "优先跟进同一会话中用户刚表达的安全风险。简短回应用户刚说的内容，用一个直接问题确认现在是否安全，"
    "或是否有可以马上过来陪伴的人。如果用户明确表示现在安全，接住其最新说明并继续当前对话。"
    "如果有迫近危险，可以请用户联系当地急救或请可信的人到身边。"
    "如果前文是校园现场危险或具体暴力威胁，先建议从安全处报警并通知学校保卫处，明确消息是否仍未证实。"
    "只用你能确认的信息，不假定用户的位置，不编造联系方式。"
)

def resolved_query_message(query: str) -> Message:
    return Message(
        "user", "路由根据同一会话整理的查询目标，仅作语境参考。\n"
        + json.dumps({"resolved_query": query}, ensure_ascii=False),
    )


_EXPLICIT_RISK = frozenset({"我不想活了", "我想自杀", "我打算自杀", "我现在就想自杀", "我想结束自己的生命"})
_SAFETY_UPDATE = re.compile(
    r"^(?:我(?:现在|已经)?安全了|我现在没有伤害自己的想法|(?:刚才|前面)说的是测试|刚才是测试)(?:[。！!，,\s]|$)"
)


def recent_explicit_risk(query, history):
    """Keep clear, recent first-person signals in the response path even if routing fails."""
    recent = [item.content for item in history if item.role == "user"][-3:] + [query]
    for line in reversed(recent):
        text = line.strip()
        if _SAFETY_UPDATE.match(text):
            return False
        if text.rstrip("。！？!? .") in _EXPLICIT_RISK:
            return True
    return False
