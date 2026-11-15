"""Let campus specialists query the MainAgent's scoped campus RAG tool."""

from typing import Annotated

from pydantic import Field, StringConstraints

from ...mcp_tools import MainAgentRAGClient, RAG_QUERY_DESCRIPTION, RAG_SEARCH_DESCRIPTION
from ...reliability.models import ToolResult
from ..skills import Skill, SkillContext, SkillInput


class SearchRAGInput(SkillInput):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)] = Field(
        description=RAG_QUERY_DESCRIPTION,
    )


async def search_rag(_context: SkillContext, args: SearchRAGInput) -> ToolResult:
    return await MainAgentRAGClient().call_tool("search_rag", {"query": args.query})


SEARCH_RAG = Skill(
    "search_rag",
    RAG_SEARCH_DESCRIPTION,
    SearchRAGInput, search_rag, read_only=True,
)
