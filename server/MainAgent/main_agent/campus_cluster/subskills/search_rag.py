"""Let campus specialists query the MainAgent's scoped campus RAG tool."""

from typing import Annotated

from pydantic import StringConstraints

from ...mcp_tools import MainAgentRAGClient
from ...reliability.models import ToolResult
from ..skills import Skill, SkillContext, SkillInput


class SearchRAGInput(SkillInput):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


async def search_rag(_context: SkillContext, args: SearchRAGInput) -> ToolResult:
    return await MainAgentRAGClient().call_tool("search_rag", {"query": args.query})


SEARCH_RAG = Skill(
    "search_rag",
    "检索校园资料库，查找核实校园事实、校内公告和规章所需的原文片段和来源。",
    SearchRAGInput, search_rag, read_only=True,
)
