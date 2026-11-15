"""Offline, scripted demonstration; no real model accuracy is measured."""

import asyncio
import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main_agent import Message, ToolSpec
from main_agent.reliability import LLMReviewer, ReliableAgent, ToolEvidenceRetriever


class DemoTools:
    async def list_tools(self):
        return [ToolSpec("get_price", "查询票价", {"type": "object", "properties": {}})]

    async def call_tool(self, name, arguments):
        return "票价为 20 元"


class DraftModel:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        return Message("assistant", "票价为 99 元" if self.calls == 1 else "票价为 20 元")


class LookupModel:
    async def complete(self, messages, tools):
        if messages[-1].role == "tool":
            return Message("assistant", "查询结束")
        from main_agent import ToolCall
        return Message("assistant", tool_calls=[ToolCall("price", "get_price", {})])


class ReviewModel:
    async def complete(self, messages, tools):
        payload = json.loads(messages[-1].content)
        grounded = payload["answer"] == "票价为 20 元" and bool(payload["evidence"])
        citations = []
        if grounded:
            citations = [{"evidence_id": payload["evidence"][0]["id"], "quote": "票价为 20 元"}]
        return Message("assistant", json.dumps({
            "rule_checks": [{"rule_id": rule["id"], "status": "pass", "reason": "脚本用例中的规则检查"} for rule in payload["rules"]],
            "claims": [{"text": payload["answer"], "kind": "fact", "status": "supported" if grounded else "unsupported", "citations": citations}],
            "issues": [],
        }, ensure_ascii=False))


async def main():
    tools = DemoTools()
    agent = ReliableAgent(
        DraftModel(), tools, LLMReviewer(ReviewModel()),
        retriever=ToolEvidenceRetriever(LookupModel(), tools, allowed_tools={"get_price"}),
    )
    result = await agent.run([Message("system", "用中文回答票价问题。"), Message("user", "票价多少？")])
    print("脚本化流程演示，不代表真实模型准确率提升。")
    print("初始草稿：", result.attempts[0].answer)
    print("拦截原因：", ", ".join(finding.code for finding in result.attempts[0].findings))
    print("补充证据：", result.evidence[0].content)
    print("最终答案：", result.answer)
    print("修复轮数：", result.stats.repairs)
    print("模型接口调用数：", result.stats.model_calls)
    print("全部工具执行数：", result.stats.total_tool_executions)
    print("阶段轨迹：", " -> ".join(
        event.phase for event in result.snapshot.events if event.kind == "phase"
    ))


if __name__ == "__main__":
    asyncio.run(main())
