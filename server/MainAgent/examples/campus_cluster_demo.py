"""Inspect the empty four-agent framework without a model or campus data."""

import asyncio
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main_agent.campus_cluster import AgentId, ClusterRequest, build_campus_cluster


async def main():
    cluster = build_campus_cluster()
    print("四个子智能体：")
    for profile in cluster.registry.profiles:
        print(f"  {profile.id.value}: {profile.name}")
    result = await cluster.run(ClusterRequest(
        conversation_id="framework_demo", query="框架检查", targets=tuple(AgentId),
    ))
    print("\n路由：独立的 ExplicitRouter")
    print("自学习请求算法：留空，未接入主编排")
    print("集群状态：", result.status.value)
    for step in result.results:
        print(f"  {step.agent_id.value}: {step.reply.status.value} ({step.reply.reason})")


if __name__ == "__main__":
    asyncio.run(main())
