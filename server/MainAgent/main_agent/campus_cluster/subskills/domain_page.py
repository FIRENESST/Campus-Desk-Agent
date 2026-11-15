"""Expose the public homepage reader as a campus subagent skill."""

import json

from ...reliability.models import ToolResult
from ..skills import Skill, SkillContext, SkillInput
from .domain_page_reader import DomainPageError, PublicDomain, fetch_domain_page


class DomainPageInput(SkillInput):
    domain: PublicDomain


async def read_domain_page(_context: SkillContext, args: DomainPageInput) -> ToolResult:
    try:
        page = await fetch_domain_page(args.domain)
    except DomainPageError as error:
        return ToolResult(json.dumps({
            "domain": args.domain, "error": {"code": error.code, "message": str(error)},
        }, ensure_ascii=False), status="error")
    return ToolResult(page.model_dump_json(), status="success")


READ_DOMAIN_PAGE = Skill(
    "read_domain_page", "给定公开域名，读取 HTTPS 首页并返回标题、来源 URL 和静态正文。",
    DomainPageInput, read_domain_page, read_only=True,
)
